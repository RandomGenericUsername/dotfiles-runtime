"""Session-bus job client — tools report through the hub's methods (5-4).

The production :class:`IJobClient` for an out-of-process lifetime job: it
calls ``BeginJob``/``RenewJob``/``ReportProgress``/``EndJob`` and publishes
domain events via the hub's validated ``Emit`` — a job NEVER constructs a
signal (AD-38). Transport is jeepney (adapters-only, per AD-34); the
connection opener is injectable so the whole client is unit-testable
without a live bus.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.io.blocking import DBusConnection, open_dbus_connection

from runtime.adapters.dbus_event_bus import INTERFACE, OBJECT_PATH, _wrap_value
from runtime.ports.bus_name_owner import BUS_NAME
from runtime.ports.jobs import IJobClient

__all__ = ["DbusJobClient"]

logger = logging.getLogger(__name__)


def _connect_session() -> DBusConnection:
    """Open the session bus (production default; tests inject a fake)."""
    try:
        return open_dbus_connection(bus="SESSION")
    except Exception as exc:
        raise RuntimeError(f"job client cannot connect to the session bus: {exc}") from exc


class DbusJobClient(IJobClient):
    """``IJobClient`` over the hub's ``org.dotfiles.Events1`` methods."""

    def __init__(
        self,
        *,
        connect: Callable[[], DBusConnection] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._connect = connect if connect is not None else _connect_session
        self._conn: DBusConnection | None = None
        self._timeout = timeout

    def _ensure(self) -> DBusConnection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _call(self, member: str, signature: str, body: tuple[Any, ...]) -> tuple[Any, ...]:
        address = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)
        reply = self._ensure().send_and_get_reply(
            new_method_call(address, member, signature, body), timeout=self._timeout
        )
        if reply.header.message_type == MessageType.error:
            error_name = reply.header.fields.get(HeaderFields.error_name, "unknown error")
            raise RuntimeError(f"{member} failed: {error_name}")
        return tuple(reply.body)

    def begin(self, kind: str, ttl: float) -> str:
        (job_id,) = self._call("BeginJob", "su", (kind, int(ttl)))
        return str(job_id)

    def renew(self, job_id: str) -> None:
        self._call("RenewJob", "s", (job_id,))

    def report_progress(self, job_id: str, fraction: float) -> None:
        self._call("ReportProgress", "sd", (job_id, float(fraction)))

    def end(self, job_id: str, exit_code: int) -> None:
        self._call("EndJob", "si", (job_id, int(exit_code)))

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        wrapped = {key: _wrap_value(value) for key, value in payload.items()}
        self._call("Emit", "sa{sv}", (topic, wrapped))

    def close(self) -> None:
        """Close the connection (idempotent, never raises)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception("job client: error closing bus connection")
