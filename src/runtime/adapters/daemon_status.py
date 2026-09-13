"""Read-only daemon status probe + report assembly (AD-41, P5-1-4).

The status surface is deliberately **daemon-independent**: it answers
"is the reactive daemon there, and what has it persisted?" without ever
requiring the daemon to be running. Absence of the daemon or the session
bus degrades to an explicit "reduced functionality" report — never an
error, never a crash (AD-41: commands stay authoritative).

Two responsibilities:

- :func:`probe_session_bus` — the D-Bus adapter (``jeepney`` stays confined
  to ``adapters/`` per AD-34). It asks the bus daemon whether
  ``org.dotfiles.Events`` has an owner (AD-38); when owned it hydrates the
  hub epoch via ``GetTopicState`` (there is no ``GetEpoch`` method in the
  contract — the reserved ``_epoch`` member is the read path) and the live
  jobs via ``GetActiveJobs``. Every failure path is caught and folded into
  the snapshot's ``detail``.
- :func:`assemble_daemon_report` — pure assembly of the probe snapshot with
  the locally persisted facts that do not need the daemon: the
  last-converged backstop record (inputs hash + timestamp) and the resolved
  AD-39 watch-root set. No I/O, no bus.

The CLI composition root owns the wiring; tests inject a fake snapshot.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection

from runtime.adapters.converge_backstop import BackstopRecord
from runtime.adapters.dbus_event_bus import INTERFACE, OBJECT_PATH, _unwrap_value
from runtime.adapters.watch_health import WatchHealthRecord
from runtime.adapters.watch_roots import WatchRoot
from runtime.ports.bus_name_owner import BUS_NAME

__all__ = [
    "DaemonBackstopView",
    "DaemonReport",
    "DaemonStatusSnapshot",
    "DaemonWatchHealthView",
    "DaemonWatchRootView",
    "assemble_daemon_report",
    "probe_session_bus",
]

logger = logging.getLogger(__name__)

#: The known topic used only to read the hub epoch through ``GetTopicState``.
#: Any contract topic works; the reserved ``_epoch`` member is epoch-exact.
_STATUS_EPOCH_TOPIC = "icme.saved"


@dataclass(frozen=True, slots=True)
class DaemonStatusSnapshot:
    """One probe of the live daemon (or its absence)."""

    bus_available: bool
    name_owned: bool
    epoch: int | None = None
    active_jobs: Mapping[str, str] = field(default_factory=dict)
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DaemonBackstopView:
    """The persisted last-converged record as the status surface shows it."""

    path: str
    present: bool
    input_hash: str | None
    converged_at: str | None


@dataclass(frozen=True, slots=True)
class DaemonWatchRootView:
    """One resolved AD-39 watch root (directory tree or single file)."""

    path: str
    kind: str
    depth: int


@dataclass(frozen=True, slots=True)
class DaemonWatchHealthView:
    """The daemon's last reported watch-set health (AD-40)."""

    degraded: bool
    failed_roots: tuple[str, ...]
    last_error: str | None
    updated_at: str | None


@dataclass(frozen=True, slots=True)
class DaemonReport:
    """The assembled read-only daemon status report."""

    bus_available: bool
    name_owned: bool
    epoch: int | None
    active_jobs: Mapping[str, str]
    detail: str | None
    backstop: DaemonBackstopView
    watch_roots: tuple[DaemonWatchRootView, ...]
    watch_health: DaemonWatchHealthView | None = None

    @property
    def reduced_functionality(self) -> bool:
        """True when the daemon (or its bus) is unavailable (AD-41)."""
        return not self.name_owned


def _call(
    conn: DBusConnection,
    member: str,
    signature: str,
    body: tuple[Any, ...],
    timeout: float,
) -> tuple[Any, ...]:
    """Send one method call and return its body (raises ``RuntimeError``)."""
    address = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)
    reply = conn.send_and_get_reply(
        new_method_call(address, member, signature or None, body), timeout=timeout
    )
    if reply.header.message_type == MessageType.error:
        error_name = reply.header.fields.get(HeaderFields.error_name, "unknown error")
        raise RuntimeError(f"{member} failed: {error_name}")
    return tuple(reply.body)


def _as_epoch(value: object) -> int | None:
    """Normalize an ``a{sv}`` ``_epoch`` member (variant or plain) to int."""
    unwrapped = _unwrap_value(value)
    if isinstance(unwrapped, bool) or not isinstance(unwrapped, int):
        return None
    return unwrapped


def probe_session_bus(timeout: float = 5.0) -> DaemonStatusSnapshot:
    """Probe the session bus for the daemon; never raises, never blocks long.

    Returns a snapshot whose ``detail`` explains any degraded path. No
    daemon attached and no bus at all are both *expected* outcomes, not
    errors (AD-41).
    """
    try:
        conn = open_dbus_connection(bus="SESSION")
    except Exception as exc:
        logger.info("daemon status: session bus unavailable (%s); reduced functionality", exc)
        return DaemonStatusSnapshot(
            bus_available=False,
            name_owned=False,
            detail=f"session bus unavailable: {exc}",
        )
    try:
        return _probe_connected(conn, timeout)
    finally:
        try:
            conn.close()
        except Exception:  # pragma: no cover - best-effort teardown
            logger.exception("daemon status: error closing bus connection")


def _probe_connected(conn: DBusConnection, timeout: float) -> DaemonStatusSnapshot:
    """Probe against an open connection (split out for hermetic testing)."""
    try:
        reply = conn.send_and_get_reply(message_bus.NameHasOwner(BUS_NAME), timeout=timeout)
    except Exception as exc:
        return DaemonStatusSnapshot(
            bus_available=True,
            name_owned=False,
            detail=f"bus query failed: {exc}",
        )
    if reply.header.message_type == MessageType.error:
        error_name = reply.header.fields.get(HeaderFields.error_name, "unknown error")
        return DaemonStatusSnapshot(
            bus_available=True,
            name_owned=False,
            detail=f"NameHasOwner failed: {error_name}",
        )
    owned = bool(reply.body) and bool(reply.body[0])
    if not owned:
        return DaemonStatusSnapshot(
            bus_available=True,
            name_owned=False,
            detail="daemon absent / reduced functionality",
        )

    detail_parts: list[str] = []
    epoch: int | None = None
    active_jobs: dict[str, str] = {}
    try:
        (state,) = _call(conn, "GetTopicState", "s", (_STATUS_EPOCH_TOPIC,), timeout)
        if isinstance(state, dict):
            epoch = _as_epoch(state.get("_epoch"))
    except Exception as exc:
        detail_parts.append(f"epoch unavailable: {exc}")
    try:
        (jobs,) = _call(conn, "GetActiveJobs", "", (), timeout)
        if isinstance(jobs, dict):
            active_jobs = {str(job_id): str(kind) for job_id, kind in jobs.items()}
    except Exception as exc:
        detail_parts.append(f"active jobs unavailable: {exc}")
    return DaemonStatusSnapshot(
        bus_available=True,
        name_owned=True,
        epoch=epoch,
        active_jobs=active_jobs,
        detail="; ".join(detail_parts) or None,
    )


def assemble_daemon_report(
    snapshot: DaemonStatusSnapshot,
    *,
    backstop_record: BackstopRecord | None,
    backstop_path: Path,
    watch_roots: Sequence[WatchRoot],
    watch_health_record: WatchHealthRecord | None = None,
) -> DaemonReport:
    """Assemble the full report (pure — no bus, no filesystem reads)."""
    backstop = DaemonBackstopView(
        path=str(backstop_path),
        present=backstop_record is not None,
        input_hash=None if backstop_record is None else backstop_record.input_hash,
        converged_at=None if backstop_record is None else backstop_record.converged_at,
    )
    roots = tuple(
        DaemonWatchRootView(
            path=str(root.path),
            kind="directory" if root.is_directory else "file",
            depth=root.depth,
        )
        for root in watch_roots
    )
    health = (
        None
        if watch_health_record is None
        else DaemonWatchHealthView(
            degraded=watch_health_record.degraded,
            failed_roots=watch_health_record.failed_roots,
            last_error=watch_health_record.last_error,
            updated_at=watch_health_record.updated_at,
        )
    )
    return DaemonReport(
        bus_available=snapshot.bus_available,
        name_owned=snapshot.name_owned,
        epoch=snapshot.epoch,
        active_jobs=dict(snapshot.active_jobs),
        detail=snapshot.detail,
        backstop=backstop,
        watch_roots=roots,
        watch_health=health,
    )
