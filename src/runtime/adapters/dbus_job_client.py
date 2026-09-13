"""Session-bus job client — tools report through the hub AND serve control (5-4).

The production :class:`IJobClient` for an out-of-process lifetime job: it
calls ``BeginJob``/``RenewJob``/``ReportProgress``/``EndJob`` and publishes
domain events via the hub's validated ``Emit`` — a job NEVER constructs a
signal (AD-38).

It is also the **job side of the control channel**: a job process serves
``/org/dotfiles/Job`` / ``org.dotfiles.Job1.Control(action)`` on the SAME
connection it used for ``BeginJob`` (so the hub-recorded bus-attested unique
name is this connection), routes the action to a registered handler (the
capture controller's pause/resume/stop), and answers with a typed error on
failure. ``serve`` drives the receive loop and, optionally, a cadence tick on
the same single (blocking) connection — no concurrent writers.

Transport is jeepney (adapters-only, per AD-34); the connection opener is
injectable so the whole client is unit-testable without a live bus.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from jeepney import (
    DBusAddress,
    HeaderFields,
    MessageType,
    new_error,
    new_method_call,
    new_method_return,
)
from jeepney.io.blocking import DBusConnection, open_dbus_connection

from runtime.adapters.dbus_event_bus import (
    INTERFACE,
    JOB_INTERFACE,
    JOB_OBJECT_PATH,
    OBJECT_PATH,
    WireError,
    _wrap_value,
    job_introspect_xml,
)
from runtime.domain.models import JobEnded, NotControllable, UnknownJob
from runtime.ports.bus_name_owner import BUS_NAME
from runtime.ports.jobs import IControllableJobClient

__all__ = ["DbusJobClient"]

logger = logging.getLogger(__name__)

_PEER_INTERFACE = "org.freedesktop.DBus.Peer"
_INTROSPECT_INTERFACE = "org.freedesktop.DBus.Introspectable"
_UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"
_INVALID_ARGS = "org.freedesktop.DBus.Error.InvalidArgs"
_FAILED = "org.freedesktop.DBus.Error.Failed"


def _connect_session() -> DBusConnection:
    """Open the session bus (production default; tests inject a fake)."""
    try:
        return open_dbus_connection(bus="SESSION")
    except Exception as exc:
        raise RuntimeError(f"job client cannot connect to the session bus: {exc}") from exc


class DbusJobClient(IControllableJobClient):
    """``IJobClient`` over the hub's ``org.dotfiles.Events1`` methods.

    Also serves ``org.dotfiles.Job1`` for the hub's delegated control calls.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], DBusConnection] | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._connect = connect if connect is not None else _connect_session
        self._conn: DBusConnection | None = None
        self._timeout = timeout
        self._job_id: str | None = None
        self._control_handler: Callable[[str, str], None] | None = None
        self._pending: list[Any] = []

    def set_control_handler(self, handler: Callable[[str, str], None]) -> None:
        """Bind the job-side handler ``handler(job_id, action)`` for Control.

        Called by the job host (e.g. the capture controller's ``control``);
        the hub validates the action against the kind allowlist first, so the
        handler only enforces identity and transition validity.
        """
        if not callable(handler):
            raise ValueError(f"control handler must be callable, got {type(handler).__name__}")
        self._control_handler = handler

    @property
    def job_id(self) -> str | None:
        """The hub-allocated job id currently held (None once ended)."""
        return self._job_id

    def _ensure(self) -> DBusConnection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def _call(self, member: str, signature: str, body: tuple[Any, ...]) -> tuple[Any, ...]:
        conn = self._ensure()
        address = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)
        message = new_method_call(address, member, signature, body)
        serial = next(conn.outgoing_serial)
        conn.send_message(message, serial=serial)
        deadline = time.monotonic() + self._timeout
        while True:
            # A nested ``_call`` (a Control handler publishing/ending) may have
            # already read our reply while it pumped; take it back rather than
            # waiting for a message that was consumed.
            queued = self._take_pending(serial)
            if queued is not None:
                return self._reply_body(member, queued)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{member} reply timed out")
            reply = conn.receive(timeout=remaining)
            reply_to = reply.header.fields.get(HeaderFields.reply_serial, None)
            if reply_to == serial:
                return self._reply_body(member, reply)
            if reply.header.message_type in (MessageType.method_return, MessageType.error):
                # A reply for an outer ``_call``; stash it for its owner.
                self._pending.append(reply)
                continue
            # An inbound Control (or Peer/Introspect) while we wait for our
            # own reply: serve it on this connection rather than swallowing it
            # (the hub may be calling us mid-tick). Never dropped.
            try:
                self._answer(reply)
            except OSError:
                raise
            except Exception:
                logger.exception("job client: serving an interleaved message failed")

    def _take_pending(self, serial: int) -> Any | None:
        """Remove and return a stashed reply for ``serial`` (FIFO scan)."""
        for index, reply in enumerate(self._pending):
            if reply.header.fields.get(HeaderFields.reply_serial, None) == serial:
                return self._pending.pop(index)
        return None

    @staticmethod
    def _reply_body(member: str, reply: Any) -> tuple[Any, ...]:
        """Validate a matched reply and return its body (typed error passthrough)."""
        if reply.header.message_type == MessageType.error:
            error_name = reply.header.fields.get(HeaderFields.error_name, "unknown error")
            raise RuntimeError(f"{member} failed: {error_name}")
        return tuple(reply.body)

    def begin(self, kind: str, ttl: float) -> str:
        (job_id,) = self._call("BeginJob", "su", (kind, int(ttl)))
        self._job_id = str(job_id)
        return self._job_id

    def renew(self, job_id: str) -> None:
        self._call("RenewJob", "s", (job_id,))

    def report_progress(self, job_id: str, fraction: float) -> None:
        self._call("ReportProgress", "sd", (job_id, float(fraction)))

    def end(self, job_id: str, exit_code: int) -> None:
        self._call("EndJob", "si", (job_id, int(exit_code)))
        if self._job_id == job_id:
            self._job_id = None

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        wrapped = {key: _wrap_value(value) for key, value in payload.items()}
        self._call("Emit", "sa{sv}", (topic, wrapped))

    # ── Job side: serve org.dotfiles.Job1 (the hub's delegated Control) ──

    def serve(
        self,
        stop_requested: Callable[[], bool],
        *,
        tick: Callable[[], None] | None = None,
        timeout: float = 0.2,
    ) -> None:
        """Blocking loop: drive ``tick`` and answer ``Job1.Control`` calls.

        ``tick`` (optional) is the job's cadence work (renew/publish); it runs
        on this thread between receives, so a single blocking jeepney
        connection is never written concurrently. ``stop_requested`` ends the
        loop. The handler's failure is a typed error reply, never a crash of
        the loop.
        """
        conn = self._ensure()
        while not stop_requested():
            if tick is not None:
                tick()
            try:
                msg = conn.receive(timeout=timeout)
            except TimeoutError:
                continue
            except OSError:
                if stop_requested():
                    break
                raise
            if msg.header.message_type != MessageType.method_call:
                continue
            try:
                self._answer(msg)
            except OSError:
                raise
            except Exception:
                logger.exception("job client: answering a control call failed; continuing")

    def _answer(self, msg: Any) -> None:
        """Route one inbound call on the job's connection (Control/Peer/Introspect)."""
        conn = self._ensure()
        fields = msg.header.fields
        path = fields.get(HeaderFields.path, "")
        interface = fields.get(HeaderFields.interface, "")
        member = fields.get(HeaderFields.member, "")
        signature: str
        body: tuple[Any, ...]
        try:
            if path != JOB_OBJECT_PATH:
                return  # not ours; ignore (never claim foreign objects)
            if interface == _INTROSPECT_INTERFACE and member == "Introspect":
                signature, body = "s", (job_introspect_xml(),)
            elif interface == _PEER_INTERFACE and member == "Ping":
                signature, body = "", ()
            elif interface == JOB_INTERFACE and member == "Control":
                # The handler may call back into the hub (publish/end); the
                # hub is pumping while it waits for this ack, so those calls
                # are answered rather than stalled (see ``_call``).
                self._handle_control_call(tuple(msg.body))
                signature, body = "", ()
            else:
                raise WireError(
                    _UNKNOWN_METHOD,
                    f"no such method on {JOB_OBJECT_PATH}: {interface}.{member}",
                )
        except Exception as exc:  # a call is never dropped silently
            name, text = self._control_error(exc)
            conn.send_message(new_error(msg, name, "s", (text,)))
            return
        conn.send_message(new_method_return(msg, signature or None, body))

    def _handle_control_call(self, body: tuple[Any, ...]) -> None:
        """Dispatch ``Job1.Control(action)`` to the registered handler."""
        if len(body) != 1 or not isinstance(body[0], str):
            raise WireError(_INVALID_ARGS, "Control takes exactly one string action")
        action = body[0]
        handler = self._control_handler
        job_id = self._job_id
        if handler is None or job_id is None:
            raise UnknownJob(job_id)
        handler(job_id, action)

    @staticmethod
    def _control_error(exc: BaseException) -> tuple[str, str]:
        """Map a job-side failure to ``(dbus_error_name, message)``.

        Typed hub errors cross the wire as ``org.dotfiles.Job1.<Type>`` so the
        hub can re-raise the exact domain error; anything else is ``Failed``.
        """
        if isinstance(exc, NotControllable):
            return f"{JOB_INTERFACE}.NotControllable", str(exc)
        if isinstance(exc, JobEnded):
            return f"{JOB_INTERFACE}.JobEnded", str(exc)
        if isinstance(exc, UnknownJob):
            return f"{JOB_INTERFACE}.UnknownJob", str(exc)
        if isinstance(exc, WireError):
            return exc.dbus_name, str(exc)
        return _FAILED, str(exc)

    def close(self) -> None:
        """Close the connection (idempotent, never raises)."""
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception("job client: error closing bus connection")
