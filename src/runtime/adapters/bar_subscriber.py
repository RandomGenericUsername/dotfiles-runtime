"""Bar consumer binding — the bar's subscription to the hub event surface (5-2).

P5-2: the bar is a THIN CONSUMER of the same ``org.dotfiles.Events1``
names the hub serves (AD-34). It does **not** import the runtime core:
contract constants are read from ``contracts/event-contract.json`` at
import time (never from ``runtime.domain`` / ``runtime.ports``), and the
session-bus transport is jeepney directly — the same library the hub
adapter uses. Both worlds depend on the contract, never on each other.

Protocol this binding implements (``contracts/event-contract.json``
`delivery`):

- **Subscribe-before-read hydration.** ``start()`` first registers the
  D-Bus match rules (``DomainEvent`` and ``JobsCleared`` on the hub's
  object path; ``NameOwnerChanged`` on the well-known name) and only then
  reads ``GetTopicState`` per tracked topic. Any signal racing the
  subscription is therefore either delivered after the match rule is
  installed or superseded by the hydrated ``(epoch, seq)`` baseline.
- **Discard signals whose ``(epoch, seq)`` is not greater than hydrated.**
  Comparison is lexicographic on the pair, never ``seq`` alone, so an
  epoch bump (which resets ``seq``) still wins.
- **``JobsCleared(epoch)`` means the hub (re)started** — all prior state is
  invalid; the consumer re-hydrates and notifies its restart handlers.
  ``NameOwnerChanged`` on ``org.dotfiles.Events`` is the belt-and-braces
  restart trigger (``JobsCleared`` is itself at-most-once).
- **Consumer-side structural validation.** Even though the hub validates on
  ``Emit`` (AD-38), the bar checks payload size (64 KiB) and nesting depth
  (8) before dispatching to a render handler — an oversized or over-deep
  payload is logged and dropped, never rendered (AD-44, "embedded per
  side").

A UI indicator is driven by DOMAIN events only, never
``JobStarted``/``JobFinished`` (AD-37): this binding subscribes to
``DomainEvent`` and ``JobsCleared`` and nothing else.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection

logger = logging.getLogger(__name__)

__all__ = [
    "BAR_TOPICS",
    "BUS_NAME",
    "DOMAIN_EVENT_ARGS",
    "DOMAIN_EVENT_SIGNAL",
    "HANDLED_SIGNALS",
    "HYDRATION_METHOD",
    "INTERFACE",
    "KNOWN_TOPICS",
    "MAX_PAYLOAD_BYTES",
    "MAX_PAYLOAD_DEPTH",
    "OBJECT_PATH",
    "RESTART_SIGNAL",
    "BarSubscriber",
    "payload_depth",
    "payload_size_bytes",
    "validate_payload",
]

#: Topic handler: ``handler(topic, payload)`` (mirrors the runtime's
#: ``IEventSubscriber`` shape without importing it).
TopicHandler = Callable[[str, Mapping[str, object]], None]

#: Restart handler: ``handler(new_epoch)`` called after re-hydration.
RestartHandler = Callable[[int], None]


# ── Contract constants (read from the machine definition, not prose) ──────


def _find_contract() -> Path:
    """Walk up from this file for ``contracts/event-contract.json``.

    Robust to repo move / alternate checkout depth; fails loud (an
    installed-only layout without the contract cannot bind the surface).
    """
    start = Path(__file__).resolve()
    for parent in start.parents:
        candidate = parent / "contracts" / "event-contract.json"
        if candidate.is_file():
            return candidate
    raise RuntimeError(f"contracts/event-contract.json not found above {start}")


_CONTRACT: dict[str, Any] = json.loads(_find_contract().read_text(encoding="utf-8"))

#: Versionless well-known bus name (AD-34/C4).
BUS_NAME: str = _CONTRACT["well_known_name"]

#: Object path serving the hub.
OBJECT_PATH: str = _CONTRACT["object_path"]

#: Versioned interface (version in the interface name only).
INTERFACE: str = _CONTRACT["interface"]

#: Every topic the contract advertises (the hub never emits off-table ones).
KNOWN_TOPICS: tuple[str, ...] = tuple(_CONTRACT["topics"])

#: Topics the bar renders: the recording indicator (``capture.state``) and
#: speed-test results (``speedtest.finished``). ``icme.saved`` triggers
#: regeneration; it does not render on the bar (epics 5-3/5-4).
BAR_TOPICS: tuple[str, ...] = ("capture.state", "speedtest.finished")


def _signal_args(name: str) -> tuple[tuple[str, str], ...]:
    """Parse a contract signal's ``["arg:type", ...]`` into ordered pairs."""
    args: list[tuple[str, str]] = []
    for spec in _CONTRACT["signals"][name]:
        arg, separator, signature = spec.partition(":")
        if not separator:  # pragma: no cover - contract JSON is pinned upstream
            raise RuntimeError(f"malformed signal arg spec {spec!r} for {name}")
        args.append((arg, signature))
    return tuple(args)


#: The signal carrying a tool-specific domain event.
DOMAIN_EVENT_SIGNAL: str = "DomainEvent"

#: The hub (re)start signal.
RESTART_SIGNAL: str = "JobsCleared"

#: Hydration method (contract `methods`).
HYDRATION_METHOD: str = "GetTopicState"

#: Contract signals this consumer binds (drift-tested against the contract).
HANDLED_SIGNALS: tuple[str, ...] = (DOMAIN_EVENT_SIGNAL, RESTART_SIGNAL)

#: Ordered members of ``DomainEvent`` (contract-exact).
DOMAIN_EVENT_ARGS: tuple[tuple[str, str], ...] = _signal_args(DOMAIN_EVENT_SIGNAL)

#: Reserved hydration members owned by the hub.
RESERVED_EPOCH: str = "_epoch"
RESERVED_SEQ: str = "_seq"

#: Bus-daemon interface + signal used as the restart fallback.
_DBUS_INTERFACE = "org.freedesktop.DBus"
_NAME_OWNER_CHANGED = "NameOwnerChanged"

# ── Consumer-side structural caps (AD-44 mirror of the hub's Gate-1 caps) ──

#: Maximum canonical-JSON UTF-8 bytes per payload (mirrors the hub's cap).
MAX_PAYLOAD_BYTES: int = 64 * 1024

#: Maximum container-nesting depth, top-level dict = 1 (mirrors the hub's cap).
MAX_PAYLOAD_DEPTH: int = 8


def payload_size_bytes(payload: Mapping[str, object]) -> int:
    """Canonical-JSON UTF-8 length (sort_keys, compact separators)."""
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def payload_depth(value: object) -> int:
    """Container nesting: top dict = 1, scalars = 0."""
    if isinstance(value, dict):
        return 1 + max((payload_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((payload_depth(item) for item in value), default=0)
    return 0


def validate_payload(payload: object) -> str | None:
    """Consumer-side structural caps; ``None`` when the payload is renderable.

    Mirrors the hub's structural limits exactly (size then depth) so the two
    sides cannot drift (pinned by the drift test). Returns a human-readable
    reason otherwise; never raises on hostile input.
    """
    if not isinstance(payload, dict):
        return f"payload must be an object, got {type(payload).__name__}"
    size = payload_size_bytes(payload)
    if size > MAX_PAYLOAD_BYTES:
        return f"payload {size} bytes exceeds {MAX_PAYLOAD_BYTES}"
    depth = payload_depth(payload)
    if depth > MAX_PAYLOAD_DEPTH:
        return f"depth {depth} exceeds {MAX_PAYLOAD_DEPTH}"
    return None


# ── Wire helpers (jeepney is an adapters-only import, AD-34) ───────────────


def _unwrap(value: Any) -> Any:
    """Normalize jeepney's ``a{sv}`` variant tuples to plain Python.

    Wire variants arrive as ``(signature, value)`` tuples; plain values
    (in-process callers, tests) pass through. Payloads are JSON-compatible,
    so tuples are never legitimate payload content.
    """
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return _unwrap(value[1])
    if isinstance(value, list):
        return [_unwrap(item) for item in value]
    if isinstance(value, dict):
        return {key: _unwrap(item) for key, item in value.items()}
    return value


def _as_uint(value: object) -> int:
    """Coerce a wire ``u`` member; anything unexpected becomes ``0``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(value, 0)


def _domain_event_match() -> str:
    return (
        f"type='signal',interface='{INTERFACE}',"
        f"member='{DOMAIN_EVENT_SIGNAL}',path='{OBJECT_PATH}'"
    )


def _restart_match() -> str:
    return (
        f"type='signal',interface='{INTERFACE}',member='{RESTART_SIGNAL}',path='{OBJECT_PATH}'"
    )


def _name_owner_match() -> str:
    return (
        f"type='signal',sender='{_DBUS_INTERFACE}',"
        f"interface='{_DBUS_INTERFACE}',member='{_NAME_OWNER_CHANGED}',arg0='{BUS_NAME}'"
    )


def _connect_session() -> DBusConnection:
    """Open the session bus (production default; tests inject a fake)."""
    try:
        return open_dbus_connection(bus="SESSION")
    except Exception as exc:
        raise RuntimeError(f"bar subscriber cannot connect to session bus: {exc}") from exc


class BarSubscriber:
    """The bar's binding to the hub's ``org.dotfiles.Events1`` surface.

    Transport-agnostic over a jeepney blocking connection (inject one with
    ``connect=`` for hermetic tests). Tracks a ``(epoch, seq)`` baseline per
    topic, drops stale signals, validates payloads structurally, and
    re-hydrates on ``JobsCleared`` / ``NameOwnerChanged``.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], DBusConnection] | None = None,
        topics: Sequence[str] = BAR_TOPICS,
        timeout: float = 5.0,
    ) -> None:
        self._connect = connect if connect is not None else _connect_session
        self._conn: DBusConnection | None = None
        self._timeout = timeout
        self._topics: list[str] = []
        self._handlers: dict[str, list[TopicHandler]] = {}
        self._restart_handlers: list[RestartHandler] = []
        self._hydrated: dict[str, tuple[int, int]] = {}
        self._last_epoch = 0
        self._started = False
        for topic in topics:
            self._track(topic)

    # ── Registration ───────────────────────────────────────────────────

    def _track(self, topic: object) -> None:
        if not isinstance(topic, str) or topic not in KNOWN_TOPICS:
            raise ValueError(f"not a contract topic: {topic!r}")
        if topic not in self._topics:
            self._topics.append(topic)

    def subscribe(self, topic: str, handler: TopicHandler) -> None:
        """Register ``handler(topic, payload)`` for a contract topic."""
        if not callable(handler):
            raise ValueError(f"handler must be callable, got {type(handler).__name__}")
        self._track(topic)
        self._handlers.setdefault(topic, []).append(handler)
        if self._started and topic not in self._hydrated:
            self._hydrate_topic(topic)

    def on_restart(self, handler: RestartHandler) -> None:
        """Register ``handler(epoch)`` fired after every successful re-hydration."""
        if not callable(handler):
            raise ValueError(f"handler must be callable, got {type(handler).__name__}")
        self._restart_handlers.append(handler)

    def start(self) -> None:
        """Subscribe (match rules) first, then hydrate (subscribe-before-read)."""
        if self._started:
            return
        if self._conn is None:
            self._conn = self._connect()
        self._register_matches()
        self._last_epoch = max(self._last_epoch, self._hydrate_all())
        self._started = True

    def _register_matches(self) -> None:
        for rule in (_domain_event_match(), _restart_match(), _name_owner_match()):
            self._send(message_bus.AddMatch(rule))

    def _send(self, message: Any) -> Any:
        """Send one message and await its reply over the blocking connection."""
        conn = self._conn
        if conn is None:
            raise RuntimeError("bar subscriber is not connected")
        return conn.send_and_get_reply(message, timeout=self._timeout)

    # ── Hydration ──────────────────────────────────────────────────────

    def _hydrate_all(self) -> int:
        """Re-read every tracked topic's ``(epoch, seq)``; return highest epoch."""
        self._hydrated.clear()
        highest = 0
        for topic in list(self._topics):
            try:
                highest = max(highest, self._hydrate_topic(topic))
            except Exception as exc:  # absence/reduced function is never fatal
                logger.warning("bar: hydration failed for %s: %s", topic, exc)
        return highest

    def _hydrate_topic(self, topic: str) -> int:
        state = self._get_topic_state(topic)
        epoch = _as_uint(state.get(RESERVED_EPOCH, 0))
        seq = _as_uint(state.get(RESERVED_SEQ, 0))
        self._hydrated[topic] = (epoch, seq)
        return epoch

    def _get_topic_state(self, topic: str) -> dict[str, object]:
        address = DBusAddress(OBJECT_PATH, bus_name=BUS_NAME, interface=INTERFACE)
        message = new_method_call(address, HYDRATION_METHOD, "s", (topic,))
        reply = self._send(message)
        if reply.header.message_type == MessageType.error:
            error_name = reply.header.fields.get(HeaderFields.error_name, "unknown error")
            raise RuntimeError(f"{HYDRATION_METHOD}({topic!r}) failed: {error_name}")
        body = tuple(reply.body)
        if not body:
            return {}
        state = _unwrap(body[0])
        return state if isinstance(state, dict) else {}

    # ── Signal handling ────────────────────────────────────────────────

    def handle_signal(self, interface: str, member: str, body: tuple[Any, ...]) -> None:
        """Entry point for one received bus signal (testable without a bus)."""
        if interface == INTERFACE and member == DOMAIN_EVENT_SIGNAL:
            self._dispatch_domain_event(tuple(body))
        elif interface == INTERFACE and member == RESTART_SIGNAL:
            if len(body) == 1:
                self._on_jobs_cleared(_as_uint(body[0]))
        elif interface == _DBUS_INTERFACE and member == _NAME_OWNER_CHANGED:
            self._on_name_owner_changed(tuple(body))

    def _dispatch_domain_event(self, body: tuple[Any, ...]) -> None:
        args = DOMAIN_EVENT_ARGS
        if len(body) != len(args):
            logger.warning("bar: DomainEvent arity %d != %d, dropped", len(body), len(args))
            return
        fields: dict[str, object] = {}
        for (name, signature), value in zip(args, body, strict=True):
            fields[name] = _unwrap(value) if signature == "a{sv}" else value

        topic = fields.get("topic")
        if not isinstance(topic, str) or topic not in self._topics:
            return  # not a topic this consumer tracks (unknown/off-table)
        epoch = _as_uint(fields.get("epoch", 0))
        seq = _as_uint(fields.get("seq", 0))
        if (epoch, seq) <= self._hydrated.get(topic, (0, 0)):
            return  # stale: not greater than the hydrated pair

        # Advance the baseline as soon as the pair is accepted (at-most-once):
        # a malformed seq position is still observed, never replayed.
        self._hydrated[topic] = (epoch, seq)

        payload = fields.get("payload")
        if not isinstance(payload, dict):
            logger.warning("bar: %s seq=%d non-object payload, dropped", topic, seq)
            return
        error = validate_payload(payload)
        if error is not None:
            logger.warning("bar: %s seq=%d dropped: %s", topic, seq, error)
            return
        for handler in list(self._handlers.get(topic, ())):
            try:
                handler(topic, payload)
            except Exception:
                logger.exception("bar: handler for %s raised; contained", topic)

    def _on_jobs_cleared(self, epoch: int) -> None:
        """Restart: a new epoch invalidates all prior hydrated state."""
        if epoch <= self._last_epoch:
            return  # not a new epoch (duplicate / replayed restart)
        self._last_epoch = epoch
        highest = self._hydrate_all()
        self._last_epoch = max(self._last_epoch, highest)
        self._notify_restart()

    def _on_name_owner_changed(self, body: tuple[Any, ...]) -> None:
        if len(body) != 3:
            return
        name, _old_owner, new_owner = body
        if name != BUS_NAME or not new_owner:
            return  # other name, or ownership loss (supervisor's job)
        previous = self._last_epoch
        highest = self._hydrate_all()
        self._last_epoch = max(previous, highest)
        if self._last_epoch > previous:
            self._notify_restart()

    def _notify_restart(self) -> None:
        for handler in list(self._restart_handlers):
            try:
                handler(self._last_epoch)
            except Exception:
                logger.exception("bar: restart handler raised; contained")

    # ── Receive loop ───────────────────────────────────────────────────

    def run(self, timeout: float | None = None) -> None:
        """Blocking receive loop (starts/hydrates first if needed)."""
        if not self._started:
            self.start()
        conn = self._conn
        if conn is None:  # pragma: no cover - start() guarantees a connection
            return
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                return
            try:
                message = conn.receive(timeout=0.2)
            except TimeoutError:
                continue
            except OSError:
                break
            if message.header.message_type != MessageType.signal:
                continue
            fields = message.header.fields
            interface = fields.get(HeaderFields.interface, "")
            member = fields.get(HeaderFields.member, "")
            try:
                self.handle_signal(interface, member, tuple(message.body))
            except Exception:
                logger.exception("bar: signal handling failed; continuing")

    def stop(self) -> None:
        """Close the connection (idempotent, never raises)."""
        conn, self._conn = self._conn, None
        self._started = False
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception("bar: error closing bus connection")

    # ── Observability (tests / diagnostics) ────────────────────────────

    def hydrated_pair(self, topic: str) -> tuple[int, int]:
        """The current ``(epoch, seq)`` baseline for ``topic`` (0,0 if unset)."""
        return self._hydrated.get(topic, (0, 0))

    @property
    def last_epoch(self) -> int:
        """The highest hub epoch observed (0 = never hydrated)."""
        return self._last_epoch
