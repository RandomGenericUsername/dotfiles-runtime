"""D-Bus ``NameOwnerChanged`` waits for AGS registration/release (v3 §2.9).

AGS instances are Gtk.Application names ``io.Astal.<instance>``. Readiness is
the bus event, not a repeated ``ags list``: subscribe first, read ownership
once, then block on the signal. A deadline only aborts the wait; it never
means the name is ready.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from jeepney import HeaderFields, MessageType
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection

logger = logging.getLogger(__name__)

__all__ = ["DbusNameWatcher", "NameWaitTimeoutError"]


class NameWaitTimeoutError(TimeoutError):
    """The bus event did not arrive within the abort bound."""


def _connect_session() -> DBusConnection:
    return open_dbus_connection(bus="SESSION")


class DbusNameWatcher:
    """Wait for an AGS instance name to appear or disappear via the bus."""

    def __init__(
        self,
        *,
        connect: Callable[[], DBusConnection] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._connect = connect or _connect_session
        self._timeout = timeout

    @staticmethod
    def bus_name(instance: str) -> str:
        return f"io.Astal.{instance}"

    def _owned(self, conn: DBusConnection, name: str, timeout: float) -> bool:
        reply = conn.send_and_get_reply(message_bus.NameHasOwner(name), timeout=timeout)
        if reply.header.message_type == MessageType.error:
            return False
        return bool(reply.body and reply.body[0])

    def wait_for_owner(self, instance: str, *, timeout: float | None = None) -> bool:
        """True only after ``NameOwnerChanged`` gives the name a new owner."""
        name = self.bus_name(instance)
        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        conn = self._connect()
        try:
            # Subscribe before reading: an owner that appears between the read
            # and the receive is delivered by the queued match, not missed.
            conn.send_and_get_reply(
                message_bus.AddMatch(
                    "type='signal',"
                    "sender='org.freedesktop.DBus',"
                    "interface='org.freedesktop.DBus',"
                    "member='NameOwnerChanged',"
                    f"arg0='{name}'"
                ),
                timeout=5,
            )
            if self._owned(conn, name, timeout=5):
                return True
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    msg = conn.receive(timeout=remaining)
                except TimeoutError:
                    return False
                if msg.header.message_type != MessageType.signal:
                    continue
                fields = msg.header.fields
                if fields.get(HeaderFields.member, "") != "NameOwnerChanged":
                    continue
                body = tuple(msg.body)
                if len(body) == 3 and body[0] == name and body[2]:
                    return True
        finally:
            try:
                conn.close()
            except Exception:
                logger.exception("name watcher: closing bus connection failed")

    def wait_for_release(self, instance: str, *, timeout: float | None = None) -> bool:
        """True only after ``NameOwnerChanged`` reports the name owner left."""
        name = self.bus_name(instance)
        deadline = time.monotonic() + (timeout if timeout is not None else self._timeout)
        conn = self._connect()
        try:
            conn.send_and_get_reply(
                message_bus.AddMatch(
                    "type='signal',"
                    "sender='org.freedesktop.DBus',"
                    "interface='org.freedesktop.DBus',"
                    "member='NameOwnerChanged',"
                    f"arg0='{name}'"
                ),
                timeout=5,
            )
            if not self._owned(conn, name, timeout=5):
                return True
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    msg = conn.receive(timeout=remaining)
                except TimeoutError:
                    return False
                if msg.header.message_type != MessageType.signal:
                    continue
                fields = msg.header.fields
                if fields.get(HeaderFields.member, "") != "NameOwnerChanged":
                    continue
                body = tuple(msg.body)
                if len(body) == 3 and body[0] == name and not body[2]:
                    return True
        finally:
            try:
                conn.close()
            except Exception:
                logger.exception("name watcher: closing bus connection failed")
