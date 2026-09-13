"""Session-bus hub adapter — jeepney transport + full event surface (Phase 5).

P5-1-2b-i published the 2a-owned job surface; P5-1-2b-ii-a added ``Emit``/
``GetTopicState``; P5-1-2b-ii-b adds the **signal + restart slice**:
all five ``org.dotfiles.Events1`` signals are emitted from the
:class:`HubEvent` sink records the domain produces, and the hub subscribes
to ``NameOwnerChanged`` on the well-known name to restart (epoch bump +
fresh registry + ``JobsCleared`` first). The whole surface is pinned to
``contracts/event-contract.xml`` by executable conformance tests.

Structure (transport split for hermetic tests):

- :data:`METHODS` / :data:`SIGNALS` — the single source: member name →
  args (name/type/direction), byte-matching the contract XML. Dispatch,
  introspection XML, and the conformance tests all derive from them.
- Name mapping at the seam (2a Gate-2): domain sink tags stay snake_case
  while wire names are PascalCase — ``job_started``→``JobStarted``,
  ``job_progress``→``JobProgress``, ``job_finished``→``JobFinished``,
  ``jobs_cleared``→``JobsCleared``, ``domain_event``→``DomainEvent``.
  ``job_adopted`` and the ``pid`` field have NO wire signal (no contract
  signal carries a pid) and are dropped on the wire — the ownership trail
  stays observable in-process only.
- :class:`SignalSink` — the queue between the domain sink and the adapter.
  The domain only appends (list append, infallible), so the registry call
  stays emission-free (sink-must-not-raise, verbatim); :class:`HubService`
  drains it AFTER the registry call returns and OUTSIDE the dispatcher
  lock, then binds records to signals (queue-then-emit, Gate-1 Q8).
- :class:`HubService` — transport-free dispatch over an
  :class:`IJobRegistry`: ``dispatch(member, args)`` returns
  ``(out_signature, out_body)`` and raises domain errors /
  :class:`WireError` / ``ValueError``; it also binds
  :class:`IEventSubscriber` (in-process handlers) and owns the restart
  path. Fully unit-testable with no bus.
- :class:`JeepneyNameOwner` — the :class:`IBusNameOwner` over a jeepney
  blocking connection: connect (jeepney Hellos inside its constructor),
  ``RequestName(DO_NOT_QUEUE)`` fail-fast, a ``NameOwnerChanged`` match
  subscription (restart path), a serve thread running the receive loop,
  release-driven stop. A failed signal send logs and keeps serving; only
  bus death (``OSError``) stops the loop (Gate-1 Q7). jeepney was chosen
  over dasbus at build time: dasbus unconditionally imports ``gi``
  (PyGObject, a system dependency), which is absent from the hermetic uv
  venv — so dasbus is unwirable both in tests and in the deployed tool
  env, exactly the fallback case the 2b-i story names.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from jeepney import DBusAddress, HeaderFields, MessageType, new_error, new_method_return, new_signal
from jeepney.bus_messages import DBusNameFlags, message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection

from runtime.adapters.emit_validation import EmitValidator, sv_variant_signature
from runtime.domain.hub import KNOWN_TOPICS, HubEvent
from runtime.domain.models import (
    BusNameContentionError,
    BusUnavailableError,
    JobEnded,
    NotControllable,
    PayloadTooLarge,
    RateLimited,
    UnknownJob,
    UnknownTopic,
)
from runtime.ports.bus_name_owner import BUS_NAME, IBusNameOwner
from runtime.ports.event_bus import IEventPublisher, IEventSubscriber, IJobRegistry

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

#: Standard error for unbound members (anything outside METHODS).
_UNKNOWN_METHOD = "org.freedesktop.DBus.Error.UnknownMethod"

#: The bus daemon interface (NameOwnerChanged subscription source).
_DBUS_INTERFACE = "org.freedesktop.DBus"

#: Match rule for the restart signal: ownership of the well-known name.
_NAME_OWNER_MATCH = (
    "type='signal',"
    f"sender='{_DBUS_INTERFACE}',"
    f"interface='{_DBUS_INTERFACE}',"
    "member='NameOwnerChanged',"
    f"arg0='{BUS_NAME}'"
)

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
    "Emit": {"in": (("topic", "s"), ("payload", "a{sv}")), "out": ()},
    "GetTopicState": {"in": (("topic", "s"),), "out": (("state", "a{sv}"),)},
}

#: The emitted signals: signal name → args as (name, signature) in wire
#: order. Byte-matches ``contracts/event-contract.xml`` (signals block);
#: the conformance tests enforce it executable. No signal carries ``pid``
#: (``job_adopted`` has no wire representation and is dropped).
SIGNALS: dict[str, tuple[tuple[str, str], ...]] = {
    "JobStarted": (("job_id", "s"), ("kind", "s"), ("epoch", "u")),
    "JobProgress": (("job_id", "s"), ("fraction", "d"), ("epoch", "u")),
    "JobFinished": (("job_id", "s"), ("exit_code", "i"), ("epoch", "u")),
    "DomainEvent": (
        ("topic", "s"),
        ("producer", "s"),
        ("seq", "u"),
        ("epoch", "u"),
        ("payload", "a{sv}"),
    ),
    "JobsCleared": (("epoch", "u"),),
}


def introspect_xml() -> str:
    """Introspection XML for the served object, generated from the tables.

    Single-sourced (no hand-written XML to drift): the served
    ``Introspect()`` reply and the conformance tests read this. Signals
    are appended after methods, matching the contract XML layout.
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
    for name, args in SIGNALS.items():
        lines.append(f'    <signal name="{name}">')
        for arg_name, arg_type in args:
            lines.append(f'      <arg name="{arg_name}" type="{arg_type}"/>')
        lines.append("    </signal>")
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
    if isinstance(
        exc, (UnknownJob, JobEnded, NotControllable, UnknownTopic, PayloadTooLarge, RateLimited)
    ):
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


def _unwrap_value(value: Any) -> Any:
    """Normalize a jeepney-parsed ``a{sv}`` value to plain Python.

    Wire variants arrive as ``(signature, value)`` tuples; plain values
    (in-process callers, tests) pass through. Tuples are never valid
    payload content, so the normalization is total: anything left over
    that is not a plain scalar/list/dict fails the sv-compatibility
    check downstream.
    """
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return _unwrap_value(value[1])
    if isinstance(value, list):
        return [_unwrap_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _unwrap_value(item) for key, item in value.items()}
    return value


def _variant_sig(value: Any) -> str:
    """Infer the D-Bus signature for a validated plain value.

    Delegates to the validator's single source of truth
    (:func:`runtime.adapters.emit_validation.sv_variant_signature`), so the
    encoder can never serialise a shape the validator would refuse:
    homogeneous non-empty arrays (``a<elem>``), dicts (``a{sv}``),
    ``bool``→``b`` (precedes ``int``: subclass trap), ``int``→``x``,
    ``float``→``d``, ``str``→``s``. Inputs are validator-clean on the emit
    path; a failure here is a programming error and raises loudly instead of
    emitting a malformed variant.
    """
    return sv_variant_signature(value)


def _wrap_value(value: Any) -> tuple[str, Any]:
    """Encode a validated plain value as a jeepney ``(sig, value)`` variant."""
    if isinstance(value, list):
        return (_variant_sig(value), [_wrap_value(item)[1] for item in value])
    if isinstance(value, dict):
        return (_variant_sig(value), {key: _wrap_value(item) for key, item in value.items()})
    return (_variant_sig(value), value)


def _decode_in_args(member: str, args: tuple[Any, ...]) -> tuple[Any, ...]:
    """Unwrap ``a{sv}`` in-args per the METHODS table (transport boundary)."""
    spec = METHODS.get(member)
    if spec is None or len(args) != len(spec["in"]):
        return args  # dispatch raises UnknownMethod/arity loudly
    return tuple(
        _unwrap_value(value) if signature == "a{sv}" else value
        for value, (_, signature) in zip(args, spec["in"], strict=True)
    )


def _encode_out_body(member: str, body: tuple[Any, ...]) -> tuple[Any, ...]:
    """Wrap ``a{sv}`` out-args per the METHODS table (transport boundary).

    The body element for an ``a{sv}`` out-arg is the dict itself with
    variant-tuple *values* (``{k: (sig, v)}``) — NOT the dict wrapped as
    one variant. jeepney serialises the dict against the ``a{sv}``
    signature directly. Per-member overrides pin contract-exact variant
    types where inference would diverge (``_epoch``/``_seq`` are ``u``
    per the contract, while bare ints infer ``x``).
    """
    spec = METHODS.get(member)
    if spec is None or len(body) != len(spec["out"]):
        return body  # dispatch already failed; send path reports it
    overrides = _OUT_TYPE_OVERRIDES.get(member, {})
    encoded: list[Any] = []
    for value, (_, signature) in zip(body, spec["out"], strict=True):
        if signature != "a{sv}" or not isinstance(value, dict):
            encoded.append(value)
            continue
        wrapped: dict[str, Any] = {}
        for key, item in value.items():
            forced = overrides.get(key)
            if forced is not None:
                if not isinstance(item, int) or isinstance(item, bool) or not 0 <= item <= _U32_MAX:
                    raise RuntimeError(f"override member {key!r} is not a uint32: {item!r}")
                wrapped[key] = (forced, item)
            else:
                wrapped[key] = _wrap_value(item)
        encoded.append(wrapped)
    return tuple(encoded)


#: Per-member variant-type overrides for ``a{sv}`` out-args (contract-exact
#: types where inference would diverge).
_OUT_TYPE_OVERRIDES: dict[str, dict[str, str]] = {
    "GetTopicState": {"_epoch": "u", "_seq": "u"},
}


#: An emitter callable: ``(signal_name, signature, body)``.  The bus owner
#: binds the real send; tests bind a recorder.  Emitters are invoked after
#: the registry call and outside the dispatcher lock (queue-then-emit).
SignalEmitter = Callable[[str, str, tuple[Any, ...]], None]


def _drop_signal(name: str, signature: str, body: tuple[Any, ...]) -> None:
    """Default emitter: no bus bound yet, so records are dropped quietly."""
    logger.debug("hub: no emitter bound; dropping %s", name)


class SignalSink:
    """Thread-safe queue between the domain sink and the adapter drain.

    The domain :class:`EventHub` writes every :class:`HubEvent` through
    this callable.  It only appends under a short lock — no bus, no I/O —
    so the domain's "sink must not raise" precondition holds by
    construction and a registry call never performs bus emission.  The
    dispatcher drains it AFTER the registry call returns and OUTSIDE the
    lock, binds records to signals, and sends them (queue-then-emit,
    P5-1-2b-ii-b Gate-1 Q8).
    """

    __slots__ = ("_events", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list[HubEvent] = []

    def __call__(self, event: HubEvent) -> None:
        with self._lock:
            self._events.append(event)

    def append(self, event: HubEvent) -> None:
        """Alias for :meth:`__call__` (the injected domain sink)."""
        self(event)

    def drain(self) -> list[HubEvent]:
        """Return queued records in order and clear the queue."""
        with self._lock:
            events = self._events
            self._events = []
            return events

    def discard(self) -> None:
        """Drop queued records (restart: they belong to the old epoch)."""
        with self._lock:
            self._events = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


def signal_for(event: HubEvent) -> tuple[str, str, tuple[Any, ...]] | None:
    """Map one domain sink record to ``(signal, signature, body)``.

    Returns ``None`` for records with no wire representation —
    ``job_adopted`` (no contract signal carries a ``pid``) and any
    unknown tag.  The payload of ``DomainEvent`` is re-wrapped as
    variant tuples, exactly as an ``a{sv}`` out-arg is.
    """
    tag = event.event
    if tag == "job_started":
        return "JobStarted", "ssu", (event.job_id, event.kind, event.epoch)
    if tag == "job_progress":
        return "JobProgress", "sdu", (event.job_id, event.fraction, event.epoch)
    if tag == "job_finished":
        return "JobFinished", "siu", (event.job_id, event.exit_code, event.epoch)
    if tag == "jobs_cleared":
        return "JobsCleared", "u", (event.epoch,)
    if tag == "domain_event":
        payload = event.payload or {}
        wrapped = {key: _wrap_value(value) for key, value in payload.items()}
        return (
            "DomainEvent",
            "ssuua{sv}",
            (event.topic, event.producer, event.seq, event.epoch, wrapped),
        )
    return None


class HubService(IEventPublisher, IEventSubscriber):
    """Transport-free job+event dispatch over an :class:`IJobRegistry`.

    ``dispatch(member, args, sender)`` returns ``(out_signature, out_body)``
    with ``""`` for void returns, raising domain errors / WireError /
    ``ValueError``. ``sender`` is the bus-attested unique name (accounting
    only, never authorization); in-process callers use ``"(local)"``.
    ``a{sv}`` args arrive jeepney-shaped and are normalized here, so
    direct (bus-free) callers and the wire share one path.  All hub calls
    are serialized under one lock (the 2a single-threaded-caller
    precondition; domain stays lock-free).

    Signal binding (2b-ii-b): the registry's :class:`SignalSink` is drained
    AFTER each call returns and OUTSIDE the lock, so no bus emission ever
    happens inside a registry call (sink-must-not-raise holds by
    construction).  A raising emitter is logged and swallowed — the
    at-most-once contract permits a dropped signal and hydration repairs
    it (Gate-1 Q7).  ``subscribe`` binds :class:`IEventSubscriber` to the
    in-process event path: handlers fire on every accepted ``DomainEvent``
    (the hub is the emitter and cannot subscribe to its own bus signals,
    so the subscribe-before-read protocol binds consumers, not the hub —
    Gate-1 Q9).
    """

    def __init__(
        self,
        registry: IJobRegistry,
        validator: EmitValidator | None = None,
        *,
        sink: SignalSink | None = None,
        emitter: SignalEmitter | None = None,
        registry_factory: Callable[[], IJobRegistry] | None = None,
    ) -> None:
        self._registry = registry
        self._validator = validator if validator is not None else EmitValidator()
        self._lock = threading.Lock()
        self._sink = sink
        self._emitter: SignalEmitter = emitter if emitter is not None else _drop_signal
        self._registry_factory = registry_factory
        self._handlers: dict[str, list[Callable[[str, Mapping[str, object]], None]]] = {}
        self._control_handlers: dict[str, list[Callable[[str, str], None]]] = {}
        self._handlers_lock = threading.Lock()

    def bind_emitter(self, emitter: SignalEmitter) -> None:
        """Bind the bus send (called by the owner once connected)."""
        self._emitter = emitter

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        """``IEventPublisher`` over the validated emit path (in-process).

        Same validator + hub ``emit`` as the wire arm, with the fixed
        ``"(local)"`` sender so local publishes are rate-counted but never
        collide with bus peers; the resulting ``domain_event`` record is
        drained to the ``DomainEvent`` signal like any wire emit.
        """
        plain = dict(payload)
        try:
            with self._lock:
                self._validator.validate(topic, plain, "(local)")
                self._registry.emit(topic, plain, producer="(local)")
        finally:
            self._drain_signals()

    def subscribe(self, topic: str, handler: Callable[[str, Mapping[str, object]], None]) -> None:
        """``IEventSubscriber``: register ``handler(topic, payload)``.

        Topic-scoped (contract topics only) and in-process: the hub is the
        emitter, so a consumer subscribes here before reading
        ``GetTopicState`` (subscribe-before-read hydration).
        """
        if not isinstance(topic, str) or topic not in KNOWN_TOPICS:
            raise UnknownTopic(topic)
        if not callable(handler):
            raise ValueError(f"handler must be callable, got {type(handler).__name__}")
        with self._handlers_lock:
            self._handlers.setdefault(topic, []).append(handler)

    def register_control(self, kind: str, handler: Callable[[str, str], None]) -> None:
        """Register ``handler(job_id, action)`` for a job kind's Control path.

        The hub validates the action against the kind allowlist; a
        registered handler receives the validated action so a co-hosted job
        can act on it. The hub stays the single control path (AD-37): a UI
        calls ``Control`` and never reaches the job directly.
        """
        if not isinstance(kind, str) or not kind:
            raise ValueError(f"control kind must be a non-empty string, got {kind!r}")
        if not callable(handler):
            raise ValueError(f"handler must be callable, got {type(handler).__name__}")
        with self._handlers_lock:
            self._control_handlers.setdefault(kind, []).append(handler)

    def flush(self) -> None:
        """Emit every queued sink record (start: ``JobsCleared`` first)."""
        self._drain_signals()

    def restart(self) -> None:
        """Epoch bump + fresh registry + ``JobsCleared`` first (restart path).

        Triggered by ``NameOwnerChanged`` on the well-known name.  Stale
        records from the old epoch are discarded; the fresh registry's
        constructor queues ``JobsCleared(new_epoch)`` first, which the
        trailing drain emits before anything else.
        """
        factory = self._registry_factory
        if factory is None:
            raise RuntimeError("hub restart requires a registry factory")
        if self._sink is not None:
            self._sink.discard()
        with self._lock:
            self._registry = factory()
        self._drain_signals()

    def handle_name_owner_changed(self, name: str, old_owner: str, new_owner: str) -> None:
        """Restart on a new owner of the well-known name (ignore name loss).

        A loss (empty ``new_owner``) is the supervisor's job (systemd
        ``Restart=always``); re-announcing an epoch we no longer own would
        put signals on the bus under a name we lost.
        """
        if name != BUS_NAME or not new_owner:
            return
        logger.info(
            "hub: %s owner changed (%s -> %s); restarting epoch",
            name,
            old_owner or "-",
            new_owner,
        )
        self.restart()

    def dispatch(
        self, member: str, args: tuple[Any, ...] | None, sender: str = "(local)"
    ) -> tuple[str, tuple[Any, ...]]:
        """Dispatch one call; raise domain errors / WireError / ValueError.

        Signals recorded during the call (including lazy-expiry synthetic
        finishes) are emitted after the call returns / raises, outside the
        lock.
        """
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
        try:
            with self._lock:
                return self._dispatch_locked(member, args, sender)
        finally:
            self._drain_signals()

    def _drain_signals(self) -> None:
        """Emit queued sink records; never raises (sink-must-not-raise).

        Each record is processed independently under a containment guard, so
        a mapping/encoding failure for one record can neither drop the
        records queued after it nor escape into the dispatch ``finally`` (a
        single bad payload must never become a generic ``Failed`` reply or
        corrupt the bus).  The validator refuses such payloads before they
        are stored; this is the belt-and-suspenders second line.  A failing
        emitter is logged and swallowed too (Gate-1 Q7): a transient bus
        failure must not take the name off the bus.  Subscribers are notified
        for every ``domain_event`` regardless of send outcome.
        """
        sink = self._sink
        if sink is None:
            return
        for event in sink.drain():
            try:
                self._emit_record(event)
            except Exception:
                logger.exception("hub: dropping signal record %r; continuing", event.event)

    def _emit_record(self, event: HubEvent) -> None:
        """Bind one record to its signal and deliver in-process; contained.

        Mapping failure is separated from send failure so a bad wire payload
        skips only the send: in-process subscribers still see the
        ``domain_event`` (the wire is at-most-once, hydration repairs it).
        """
        try:
            mapped = signal_for(event)
        except Exception:
            logger.exception(
                "hub: signal mapping failed for record %r; dropping signal", event.event
            )
            mapped = None
        if mapped is not None:
            name, signature, body = mapped
            try:
                self._emitter(name, signature, body)
            except Exception:
                logger.exception("hub: signal emission failed for %s; continuing", name)
        if event.event == "domain_event":
            self._notify(event)
        elif event.event == "control":
            self._notify_control(event)

    def _notify_control(self, event: HubEvent) -> None:
        """Fan a validated ``control`` record out to kind handlers (contained).

        No contract signal carries a control delivery, so this is the
        in-process delivery path for a co-hosted job; a raising handler is
        logged and swallowed so one bad job cannot break the hub.
        """
        kind = event.kind
        job_id = event.job_id
        action = event.action
        if kind is None or job_id is None or action is None:
            return
        with self._handlers_lock:
            handlers = list(self._control_handlers.get(kind, ()))
        for handler in handlers:
            try:
                handler(job_id, action)
            except Exception:
                logger.exception("hub: control handler for %s raised", kind)

    def _notify(self, event: HubEvent) -> None:
        """Fan a ``domain_event`` out to in-process subscribers (caller-safe)."""
        topic = event.topic
        payload = event.payload
        if topic is None or payload is None:
            return
        with self._handlers_lock:
            handlers = list(self._handlers.get(topic, ()))
        for handler in handlers:
            try:
                handler(topic, payload)
            except Exception:
                logger.exception("hub: subscriber handler for %s raised", topic)

    def _dispatch_locked(
        self, member: str, args: tuple[Any, ...], sender: str
    ) -> tuple[str, tuple[Any, ...]]:
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
        if member == "Emit":
            topic = args[0]
            payload = args[1]
            if not isinstance(payload, dict):
                raise WireError(
                    _INVALID_ARGS, f"payload must be an object, got {type(payload).__name__}"
                )
            self._validator.validate(topic, payload, sender)
            self._registry.emit(topic, payload, producer=sender)
            return "", ()
        if member == "GetTopicState":
            return "a{sv}", (self._registry.topic_state(args[0]),)
        raise WireError(_UNKNOWN_METHOD, f"unbound method table entry: {member!r}")


class JeepneyNameOwner(IBusNameOwner):
    """Session-bus name owner + full event server over jeepney (2b-ii-b).

    ``acquire()`` connects (the constructor Hellos), takes the
    well-known name with ``DO_NOT_QUEUE`` (fail-fast: anything but
    primary-owner/already-owner is contention, and a bus refusal is
    unavailability — never confused), subscribes to ``NameOwnerChanged``
    for the name (restart path), binds the signal emitter, flushes the
    queued start records (``JobsCleared`` first), then serves. A serve
    thread runs the blocking receive loop with a short timeout;
    ``release()`` is idempotent and stops it (stop flag checked each
    iteration, so the loop exits within one timeout even if closing the
    socket does not wake the blocked receive);
    ``wait_until_terminated()`` parks on the stopped event (no polling,
    no timers). Re-acquire after release fully re-arms (stopped flag
    cleared on ownership).

    Signal sends go through the same blocking connection; a generic
    failure logs and keeps serving, while ``OSError`` (bus death) stops
    the loop (Gate-1 Q7). This is safe to call from the serve thread
    (dispatch) and from ``acquire`` (start flush, before the thread
    starts); in-process ``publish`` from an unrelated thread is not a
    supported concurrent-writer case (the daemon never does it).
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
            # Restart path: learn when ownership of the well-known name
            # changes.  Subscribed AFTER RequestName so our own initial
            # acquisition (already emitted) is not seen as a restart.
            try:
                conn.send_and_get_reply(message_bus.AddMatch(_NAME_OWNER_MATCH), timeout=5)
            except Exception as exc:
                self._conn = None
                self._owned = False
                _close_quietly(conn)
                raise BusUnavailableError(f"bus match subscription failed: {exc}") from exc
            self._service.bind_emitter(self._emit_signal)
            # Start records are already queued by the domain hub's
            # constructor; flush emits JobsCleared(epoch) FIRST.
            self._service.flush()
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
        """Blocking receive loop: route calls and signals, reply, never drop."""
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
            if msg.header.message_type == MessageType.signal:
                try:
                    self._handle_signal(service, msg)
                except Exception:
                    logger.exception("hub serve: signal handling failed; continuing")
                continue
            if msg.header.message_type != MessageType.method_call:
                continue
            try:
                self._answer(conn, service, msg)
            except Exception:
                logger.exception("hub serve: answer failed; continuing")

    def _handle_signal(self, service: HubService, msg: Any) -> None:
        """Route a received bus signal (restart path).

        Only ``NameOwnerChanged`` for our well-known name is acted on;
        ownership loss (empty new owner) is left to the supervisor.
        """
        fields = msg.header.fields
        if fields.get(HeaderFields.interface, "") != _DBUS_INTERFACE:
            return
        if fields.get(HeaderFields.member, "") != "NameOwnerChanged":
            return
        body = tuple(msg.body)
        if len(body) != 3 or not all(isinstance(item, str) for item in body):
            return
        service.handle_name_owner_changed(body[0], body[1], body[2])

    def _emit_signal(self, name: str, signature: str, body: tuple[Any, ...]) -> None:
        """Send one signal on the served object (never raises)."""
        conn = self._conn
        if conn is None:
            return
        emitter = DBusAddress(OBJECT_PATH, interface=INTERFACE)
        try:
            conn.send_message(new_signal(emitter, name, signature, body))
        except OSError:
            # Bus death (not a per-message failure): stop serving.
            logger.error("hub serve: signal send failed on a dead bus; stopping")
            self._stopped.set()
        except Exception:
            logger.exception("hub: failed to emit %s; continuing", name)

    def _answer(self, conn: DBusConnection, service: HubService, msg: Any) -> None:
        """Route one method call and send its reply (or typed error).

        Wire codec boundary: ``a{sv}`` in-args arrive jeepney-shaped
        (``(sig, value)`` tuples) and are normalized to plain Python
        before dispatch; ``a{sv}`` out-args are wrapped back. The sender
        unique-name is bus-attested (header), never self-asserted —
        accounting only (AD-38).
        """
        fields = msg.header.fields
        interface = fields.get(HeaderFields.interface, "")
        member = fields.get(HeaderFields.member, "")
        path = fields.get(HeaderFields.path, "")
        raw_sender = fields.get(HeaderFields.sender, "")
        sender = raw_sender if isinstance(raw_sender, str) and raw_sender else "(local)"
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
                signature, body = service.dispatch(
                    member, _decode_in_args(member, tuple(msg.body)), sender
                )
                body = _encode_out_body(member, body)
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
