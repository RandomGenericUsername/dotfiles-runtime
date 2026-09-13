"""Session-bus hub adapter — jeepney transport + job-method dispatch (Phase 5).

P5-1-2b-i: publishes the 2a-owned job surface (``BeginJob``/``RenewJob``/
``AdoptJob``/``ReportProgress``/``EndJob``/``Control``/``GetActiveJobs``)
on ``/org/dotfiles/Events`` / ``org.dotfiles.Events1`` per
``contracts/event-contract.xml``. ``Emit``/``GetTopicState`` are NOT
published (2b-ii owns them; calling them fails loud with the standard
``UnknownMethod`` error, never a silent no-op). No signals yet (2b-ii).

Structure (transport split for hermetic tests):

- :data:`METHODS` — the single source: method → in/out args
  (name/type/direction), byte-matching the contract XML. Dispatch,
  introspection XML, and the conformance tests all derive from it.
- Name mapping at the seam (2a Gate-2): domain sink tags stay snake_case
  while wire names are PascalCase — ``job_started``→``JobStarted``,
  ``job_progress``→``JobProgress``, ``job_finished``→``JobFinished``,
  ``jobs_cleared``→``JobsCleared``. ``job_adopted`` and the ``pid`` field
  have NO wire signal (no contract signal carries a pid) and are dropped
  on the wire — the ownership trail stays observable in-process only.
- :class:`HubService` — transport-free dispatch over an
  :class:`IJobRegistry`: ``dispatch(member, args)`` returns
  ``(out_signature, out_body)`` and raises domain errors /
  :class:`WireError` / ``ValueError``. Fully unit-testable with no bus.
- :class:`JeepneyNameOwner` — the :class:`IBusNameOwner` over a jeepney
  blocking connection: connect (jeepney Hellos inside its constructor),
  ``RequestName(DO_NOT_QUEUE)`` fail-fast, a
  serve thread running the receive loop, release-driven stop. jeepney was
  chosen over dasbus at build time: dasbus unconditionally imports ``gi``
  (PyGObject, a system dependency), which is absent from the hermetic uv
  venv — so dasbus is unwirable both in tests and in the deployed tool
  env, exactly the fallback case the 2b-i story names.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from jeepney import HeaderFields, MessageType, new_error, new_method_return
from jeepney.bus_messages import DBusNameFlags, message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection

from runtime.domain.models import (
    BusNameContentionError,
    BusUnavailableError,
    JobEnded,
    NotControllable,
    UnknownJob,
)
from runtime.ports.bus_name_owner import BUS_NAME, IBusNameOwner
from runtime.ports.event_bus import IJobRegistry

logger = logging.getLogger(__name__)

#: Object path serving the hub (mirrors the contract XML; pinned by
#: the conformance tests, not by prose).
OBJECT_PATH = "/org/dotfiles/Events"

#: Interface served (version in the name only, AD-34).
INTERFACE = "org.dotfiles.Events1"

#: Peer interface answered minimally (liveness only).
_PEER_INTERFACE = "org.freedesktop.DBus.Peer"

#: Introspection interface answered from the generated XML.
_INTROSPECT_INTERFACE = "org.freedesktop.DBus.Introspectable"

#: Standard error for unbound members (Emit/GetTopicState/anything else).
_UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"

#: Generic error for value violations (fraction range, widths) — the
#: contract's typed list has no BadFraction (2a Gate-1 rec 6).
_INVALID_ARGS = "org.freedesktop.DBus.Error.InvalidArgs"

#: Last-resort error: a call is never dropped silently.
_FAILED = "org.freedesktop.DBus.Error.Failed"

_U32_MAX = 2**32 - 1
_I32_MIN = -(2**31)
_I32_MAX = 2**31 - 1

#: The single source for the served surface: method → in/out args as
#: (name, signature) in wire order. Byte-matches
#: ``contracts/event-contract.xml`` (methods block); the conformance tests
#: enforce it executable. 2b-ii adds Emit/GetTopicState here.
METHODS: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {
    "BeginJob": {"in": (("kind", "s"), ("ttl", "u")), "out": (("job_id", "s"),)},
    "RenewJob": {"in": (("job_id", "s"),), "out": ()},
    "AdoptJob": {"in": (("job_id", "s"), ("pid", "u")), "out": ()},
    "ReportProgress": {"in": (("job_id", "s"), ("fraction", "d")), "out": ()},
    "EndJob": {"in": (("job_id", "s"), ("exit_code", "i")), "out": ()},
    "Control": {"in": (("job_id", "s"), ("action", "s")), "out": ()},
    "GetActiveJobs": {"in": (), "out": (("jobs", "a{ss}"),)},
}


def introspect_xml() -> str:
    """Introspection XML for the served object, generated from METHODS.

    Single-sourced (no hand-written XML to drift): the served
    ``Introspect()`` reply and the conformance tests read this.
    """
    lines = [
        f'<node name="{OBJECT_PATH}">',
        f'  <interface name="{INTERFACE}">',
    ]
    for name, spec in METHODS.items():
        lines.append(f'    <method name="{name}">')
        for arg_name, arg_type in spec["in"]:
            lines.append(f'      <arg name="{arg_name}" type="{arg_type}" direction="in"/>')
        for arg_name, arg_type in spec["out"]:
            lines.append(f'      <arg name="{arg_name}" type="{arg_type}" direction="out"/>')
        lines.append("    </method>")
    lines.append("  </interface>")
    lines.append("</node>")
    return "\n".join(lines) + "\n"


class WireError(Exception):
    """A wire-level failure with its D-Bus error name (e.g. UnknownMethod)."""

    def __init__(self, dbus_name: str, message: str) -> None:
        super().__init__(message)
        self.dbus_name = dbus_name


def wire_error_name(exc: BaseException) -> tuple[str, str]:
    """Map a dispatch failure to ``(dbus_error_name, message)``.

    Typed domain errors keep their contract names under the ``Events1``
    namespace with the offending detail preserved; value violations become
    ``InvalidArgs``; anything unexpected becomes ``Failed`` (a call is
    never dropped silently).
    """
    if isinstance(exc, WireError):
        return exc.dbus_name, str(exc)
    if isinstance(exc, (UnknownJob, JobEnded, NotControllable)):
        return f"{INTERFACE}.{type(exc).__name__}", str(exc)
    if isinstance(exc, ValueError):
        return _INVALID_ARGS, str(exc)
    return _FAILED, f"internal error: {exc}"


def _u32(value: object, name: str) -> int:
    """Validate a wire ``u`` (bool/float/str/negative/over-u32 all fail)."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _U32_MAX:
        raise ValueError(f"{name} must be a uint32, got {value!r}")
    return value


def _i32(value: object, name: str) -> int:
    """Validate a wire ``i`` before the domain ever sees it."""
    if isinstance(value, bool) or not isinstance(value, int) or not _I32_MIN <= value <= _I32_MAX:
        raise ValueError(f"{name} must be an int32, got {value!r}")
    return value


class HubService:
    """Transport-free job-method dispatch over an :class:`IJobRegistry`.

    ``dispatch(member, args)`` returns ``(out_signature, out_body)`` with
    ``""`` for void returns. All hub calls are serialized under one lock
    (the 2a single-threaded-caller precondition; domain stays lock-free).
    The sink stays log-only in 2b-i — dispatch performs NO bus emission
    inside a registry call, so the sink-must-not-raise precondition holds
    by construction (signals arrive in 2b-ii).
    """

    def __init__(self, registry: IJobRegistry) -> None:
        self._registry = registry
        self._lock = threading.Lock()

    def dispatch(self, member: str, args: tuple[Any, ...] | None) -> tuple[str, tuple[Any, ...]]:
        """Dispatch one call; raise domain errors / WireError / ValueError."""
        if args is None:
            raise WireError(_INVALID_ARGS, f"{member} called with no arguments")
        spec = METHODS.get(member)
        if spec is None:
            raise WireError(
                _UNKNOWN_METHOD,
                f"no such method on {INTERFACE}: {member!r}",
            )
        expected = spec["in"]
        if len(args) != len(expected):
            raise WireError(
                _INVALID_ARGS,
                f"{member} takes {len(expected)} args, got {len(args)}",
            )
        with self._lock:
            return self._dispatch_locked(member, args)

    def _dispatch_locked(self, member: str, args: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
        if member == "BeginJob":
            # kind stays pass-through: the domain validates (non-empty str)
            # and reports ValueError; job_id likewise reaches the domain,
            # where a non-str id is UnknownJob (never InvalidArgs).
            ttl = _u32(args[1], "ttl")
            return "s", (self._registry.begin(args[0], ttl),)
        if member == "RenewJob":
            self._registry.renew(args[0])
            return "", ()
        if member == "AdoptJob":
            # Deliberate order: wire widths are the transport contract and
            # are checked before liveness (domain documents identity-first,
            # but both failures are loud typed errors, so wire impact is nil).
            self._registry.adopt(args[0], _u32(args[1], "pid"))
            return "", ()
        if member == "ReportProgress":
            # Range stays a domain ValueError → generic D-Bus error
            # (no BadFraction in the typed list, 2a Gate-1 rec 6).
            self._registry.report_progress(args[0], args[1])
            return "", ()
        if member == "EndJob":
            # Same deliberate order as AdoptJob above (widths before liveness).
            self._registry.end(args[0], _i32(args[1], "exit_code"))
            return "", ()
        if member == "Control":
            # Unhashable actions reach the domain, which reports
            # NotControllable (2a Gate-2) — never a bare TypeError.
            self._registry.control(args[0], args[1])
            return "", ()
        if member == "GetActiveJobs":
            return "a{ss}", (self._registry.active_jobs(),)
        raise WireError(_UNKNOWN_METHOD, f"unbound method table entry: {member!r}")


class JeepneyNameOwner(IBusNameOwner):
    """Session-bus name owner + job-surface server over jeepney (2b-i).

    ``acquire()`` connects (the constructor Hellos), takes the
    well-known name with ``DO_NOT_QUEUE`` (fail-fast: anything but
    primary-owner/already-owner is contention, and a bus refusal is
    unavailability — never confused), then serves. A serve thread runs
    the blocking receive loop with a short timeout; ``release()`` is
    idempotent and stops it (stop flag checked each iteration, so the
    loop exits within one timeout even if closing the socket does not
    wake the blocked receive); ``wait_until_terminated()`` parks on the
    stopped event (no polling, no timers). Re-acquire after release
    fully re-arms (stopped flag cleared on ownership).
    """

    def __init__(self, service: HubService | None = None) -> None:
        self._service = service
        self._conn: DBusConnection | None = None
        self._owned = False
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> None:
        """Connect, RequestName(DO_NOT_QUEUE); spawn the serve loop.

        ``open_dbus_connection`` already says Hello inside its constructor
        (a separate Hello here would be a protocol duplicate), so reaching
        this point means the bus greeted us; only ``RequestName`` is sent.
        """
        if self._conn is not None:
            raise RuntimeError("bus name owner is already serving")
        try:
            conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:
            raise BusUnavailableError(f"cannot connect to the session bus: {exc}") from exc
        try:
            reply = conn.send_and_get_reply(
                message_bus.RequestName(BUS_NAME, flags=int(DBusNameFlags.do_not_queue)),
                timeout=5,
            )
        except Exception as exc:
            _close_quietly(conn)
            raise BusUnavailableError(f"bus request failed: {exc}") from exc
        if reply.header.message_type != MessageType.method_return or not reply.body:
            error = reply.header.fields.get(HeaderFields.error_name, "unknown error")
            _close_quietly(conn)
            raise BusUnavailableError(f"bus refused name {BUS_NAME}: {error}")
        code = reply.body[0]
        if code not in (1, 4):
            _close_quietly(conn)
            raise BusNameContentionError(f"{BUS_NAME} is already owned (RequestName reply {code})")
        self._conn = conn
        self._owned = True
        self._stopped.clear()
        if self._service is not None:
            self._thread = threading.Thread(target=self._serve_loop, name="dbus-serve", daemon=True)
            self._thread.start()

    def release(self) -> None:
        """Stop serving and free the name (idempotent, never raises)."""
        self._stopped.set()
        conn, self._conn = self._conn, None
        self._owned = False
        if conn is not None:
            _close_quietly(conn)
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def wait_until_terminated(self) -> None:
        """Block (no polling, no timers) until :meth:`release`."""
        self._stopped.wait()

    def _serve_loop(self) -> None:
        """Blocking receive loop: route calls, reply, never drop."""
        conn = self._conn
        service = self._service
        if conn is None or service is None:
            return
        while not self._stopped.is_set():
            try:
                msg = conn.receive(timeout=0.2)
            except TimeoutError:
                continue
            except OSError:
                if self._stopped.is_set():
                    break  # closed underneath by release(); orderly shutdown
                logger.error("hub serve: bus connection lost; stopping serve loop")
                self._stopped.set()
                break
            except Exception:
                logger.exception("hub serve: receive failed")
                time.sleep(0.05)
                continue
            if msg.header.message_type != MessageType.method_call:
                continue
            try:
                self._answer(conn, service, msg)
            except Exception:
                logger.exception("hub serve: answer failed; continuing")

    def _answer(self, conn: DBusConnection, service: HubService, msg: Any) -> None:
        """Route one method call and send its reply (or typed error)."""
        fields = msg.header.fields
        interface = fields.get(HeaderFields.interface, "")
        member = fields.get(HeaderFields.member, "")
        path = fields.get(HeaderFields.path, "")
        signature: str
        body: tuple[Any, ...]
        try:
            if path != OBJECT_PATH:
                return  # not ours; ignore (never claim foreign objects)
            if interface == _INTROSPECT_INTERFACE and member == "Introspect":
                signature, body = "s", (introspect_xml(),)
            elif interface == _PEER_INTERFACE and member == "Ping":
                signature, body = "", ()
            elif interface == INTERFACE:
                signature, body = service.dispatch(member, tuple(msg.body))
            else:
                # Our path, foreign interface: fail loud (a call is never
                # dropped silently) instead of hanging the caller.
                raise WireError(
                    _UNKNOWN_METHOD,
                    f"no such interface on {OBJECT_PATH}: {interface!r}",
                )
        except Exception as exc:  # a call is never dropped silently
            name, text = wire_error_name(exc)
            try:
                conn.send_message(new_error(msg, name, "s", (text,)))
            except OSError:
                logger.warning("hub serve: failed to send error reply; stopping")
                self._stopped.set()
            return
        try:
            conn.send_message(new_method_return(msg, signature or None, body))
        except OSError:
            logger.warning("hub serve: failed to send reply; stopping")
            self._stopped.set()


def _close_quietly(conn: DBusConnection) -> None:
    """Best-effort close (acquire-failure paths must not raise teardown)."""
    try:
        conn.close()
    except Exception:
        logger.exception("hub: error closing bus connection")
