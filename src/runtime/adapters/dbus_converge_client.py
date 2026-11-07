"""CLI side of the daemon converge job queue (v3 ``generations.md`` §3.1).

The CLI preflights the well-known name **once**, subscribes to
``JobFinished``/``NameOwnerChanged`` **before** submitting, then calls the
existing ``BeginJob`` method with a ``converge:<intentHash>`` kind. The daemon
allocates the job id and emits ``JobStarted``/``JobFinished`` on the existing
event surface; there is no polling of state files. The committed result file is
read once after the matching event.

If the daemon does not own the name, ``submit`` returns ``None`` and the caller
runs the same convergence function under ``converge.lock``. Once a job was
submitted, a daemon disappearance is a typed failure — the CLI never races a
possibly-live job with the fallback executor.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection

from runtime.adapters.dbus_event_bus import INTERFACE, OBJECT_PATH
from runtime.ports.bus_name_owner import BUS_NAME

logger = logging.getLogger(__name__)

__all__ = [
    "CONVERGE_JOB_PREFIX",
    "ConvergeDaemonLostError",
    "ConvergeJobFailedError",
    "ConvergeJobResult",
    "ConvergeTimeoutError",
    "DbusConvergeClient",
]

CONVERGE_JOB_PREFIX = "converge:"
_DBUS_INTERFACE = "org.freedesktop.DBus"
RESULT_DIR = "converge-results"
REQUEST_DIR = "converge-requests"


class ConvergeDaemonLostError(RuntimeError):
    """The daemon disappeared after a job was submitted."""


class ConvergeJobFailedError(RuntimeError):
    """The daemon finished the job without a committed generation."""

    def __init__(self, message: str, *, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class ConvergeTimeoutError(TimeoutError):
    """The job did not finish within the caller's abort bound."""


@dataclass(frozen=True, slots=True)
class ConvergeJobResult:
    """The daemon's committed result for one CLI job."""

    job_id: str
    generation_hash: str | None
    exit_code: int
    degraded: bool = False
    superseded_by: str | None = None
    payload: dict[str, Any] | None = None


def _connect_session() -> DBusConnection:
    try:
        return open_dbus_connection(bus="SESSION")
    except Exception as exc:  # adapter boundary
        raise RuntimeError(f"converge client cannot connect to the session bus: {exc}") from exc


_JOB_FINISHED_MATCH = (
    "type='signal',"
    f"sender='{BUS_NAME}',"
    f"path='{OBJECT_PATH}',"
    f"interface='{INTERFACE}',"
    "member='JobFinished'"
)
_NAME_OWNER_MATCH = (
    "type='signal',"
    f"sender='{_DBUS_INTERFACE}',"
    f"interface='{_DBUS_INTERFACE}',"
    "member='NameOwnerChanged',"
    f"arg0='{BUS_NAME}'"
)


class DbusConvergeClient:
    """One-shot bus preflight + event-driven converge job wait."""

    def __init__(
        self,
        *,
        state_root: Path,
        connect: Callable[[], DBusConnection] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._state_root = state_root
        self._connect = connect or _connect_session
        self._timeout = timeout

    def result_path(self, job_id: str) -> Path:
        return self._state_root / RESULT_DIR / f"{job_id}.json"

    def _call(
        self, conn: DBusConnection, message: Any, *, label: str, timeout: float
    ) -> tuple[Any, ...]:
        reply = conn.send_and_get_reply(message, timeout=timeout)
        if reply.header.message_type == MessageType.error:
            name = reply.header.fields.get(HeaderFields.error_name, "unknown error")
            raise RuntimeError(f"{label} failed: {name}")
        return tuple(reply.body)

    def preflight(self, conn: DBusConnection) -> bool:
        """Exactly one ``NameHasOwner`` call; no retries and no sleeps."""
        body = self._call(
            conn,
            message_bus.NameHasOwner(BUS_NAME),
            label="NameHasOwner",
            timeout=min(5.0, self._timeout),
        )
        return bool(body and body[0])

    def _write_request(self, intent_hash: str, request: dict[str, Any] | None) -> None:
        if request is None:
            return
        directory = self._state_root / REQUEST_DIR
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{intent_hash}.json"
        tmp = directory / f".{intent_hash}.tmp.{os.getpid()}"
        try:
            tmp.write_text(
                json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            os.replace(str(tmp), str(target))
        finally:
            tmp.unlink(missing_ok=True)

    def submit(
        self,
        intent_hash: str,
        request: dict[str, Any] | None = None,
    ) -> ConvergeJobResult | None:
        """Return the committed result, or ``None`` when no daemon owns the name."""
        conn = self._connect()
        try:
            if not self.preflight(conn):
                return None
            self._write_request(intent_hash, request)
            conn.send_and_get_reply(message_bus.AddMatch(_JOB_FINISHED_MATCH), timeout=5)
            conn.send_and_get_reply(message_bus.AddMatch(_NAME_OWNER_MATCH), timeout=5)
            address = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)
            message = new_method_call(
                address,
                "BeginJob",
                "su",
                (f"{CONVERGE_JOB_PREFIX}{intent_hash}", int(self._timeout)),
            )
            (job_id,) = self._call(conn, message, label="BeginJob", timeout=5)
            job_id = str(job_id)
            deadline = time.monotonic() + self._timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ConvergeTimeoutError(
                        f"converge job {job_id} did not finish within {self._timeout:g}s"
                    )
                try:
                    msg = conn.receive(timeout=remaining)
                except TimeoutError:
                    raise ConvergeTimeoutError(
                        f"converge job {job_id} did not finish within {self._timeout:g}s"
                    ) from None
                if msg.header.message_type != MessageType.signal:
                    continue
                fields = msg.header.fields
                member = fields.get(HeaderFields.member, "")
                body = tuple(msg.body)
                if member == "NameOwnerChanged":
                    if len(body) == 3 and body[0] == BUS_NAME and not body[2]:
                        raise ConvergeDaemonLostError(
                            f"{BUS_NAME} disappeared after job {job_id} was submitted; "
                            "the committed generation is the source of truth"
                        )
                    continue
                if member != "JobFinished":
                    continue
                if len(body) != 3 or str(body[0]) != job_id:
                    continue
                exit_code = int(body[1])
                payload = self._read_result(job_id)
                if payload is None:
                    raise ConvergeJobFailedError(
                        f"converge job {job_id} finished without a result record",
                        exit_code=exit_code,
                    )
                generation_hash = payload.get("generationHash")
                if exit_code != 0:
                    reason = payload.get("error") or f"exit code {exit_code}"
                    raise ConvergeJobFailedError(
                        f"converge job {job_id} failed: {reason}",
                        exit_code=exit_code,
                    )
                if not isinstance(generation_hash, str):
                    raise ConvergeJobFailedError(
                        f"converge job {job_id} returned no generation hash",
                        exit_code=exit_code,
                    )
                return ConvergeJobResult(
                    job_id=job_id,
                    generation_hash=generation_hash,
                    exit_code=exit_code,
                    degraded=bool(payload.get("degraded", False)),
                    superseded_by=payload.get("supersededBy"),
                    payload=payload,
                )
        finally:
            try:
                conn.close()
            except Exception:
                logger.exception("converge client: closing bus connection failed")

    def _read_result(self, job_id: str) -> dict[str, Any] | None:
        try:
            data = json.loads(self.result_path(job_id).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None
