"""GTK4 app consumer binding — restarts GTK4 apps on palette-affecting swaps.

The GTK4 restart is hosted where the hub lives (the daemon) as a THIN
CONSUMER of the ``org.dotfiles.Events1`` surface. Like the bar binding, it
does **not** import the runtime core: contract constants are read from
``contracts/event-contract.json`` at import time (never from
``runtime.domain`` / ``runtime.ports``) and the session-bus transport is
jeepney directly.

Protocol this binding implements (``contracts/event-contract.json``
`delivery`):

- **Subscribe-before-read hydration.** ``start()`` first registers the D-Bus
  match rules (``DomainEvent`` and ``JobsCleared`` on the hub's object path;
  ``NameOwnerChanged`` on the well-known name) and only then reads
  ``GetTopicState`` for ``wallpaper.state``. Any signal racing the
  subscription is therefore either delivered after the match rule is installed
  or superseded by the hydrated ``(epoch, seq)`` baseline.
- **Discard signals whose ``(epoch, seq)`` is not greater than hydrated.**
  Comparison is lexicographic on the pair, never ``seq`` alone, so an epoch
  bump (which resets ``seq``) still wins.
- **``JobsCleared(epoch)`` / ``NameOwnerChanged`` re-hydrate only.** A hub
  restart invalidates all hydrated state; the binding re-reads the baseline
  and never replays a pre-restart event or restarts an app because of it.
- **Consumer-side structural validation.** Payload size (64 KiB) and nesting
  depth (8) are checked before dispatch; an oversized or over-deep payload is
  logged and dropped.

Dispatch rule (the whole point of ``trigger``): only a ``wallpaper.state``
event in state ``done`` acts, and only when the change can affect the GTK4
palette:

- ``trigger == "regenerate"`` is skipped (icons-only; ``colors.adw.css`` did
  not change) and the skip is logged with topic and trigger;
- ``set`` / ``reconcile`` / ``reactive`` restart the allowlisted apps;
- an **absent** ``trigger`` is treated conservatively as palette-affecting
  when ``state == "done"``, so a publisher that predates the additive field
  (or a rolling rollout where the hub emits before the field is wired) still
  refreshes the apps rather than silently rotting on a stale palette. A
  present-but-unknown trigger is not in the allowlist and is skipped.

The restart primitive is the hardened ``Gtk4AppReloader`` (discovery via
``/proc`` + wait-for-exit then relaunch); it is injected via ``restart=`` so
tests never spawn a process. ``serve(stop_event)`` is the daemon's
background-thread hosting entry point; ``run()`` stays the bounded test hook.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.bus_messages import message_bus
from jeepney.io.blocking import DBusConnection, open_dbus_connection

from runtime.adapters.gtk4_app_reloader import Gtk4AppReloader

logger = logging.getLogger(__name__)

__all__ = [
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
    "RESTART_TRIGGERS",
    "SKIP_TRIGGER",
    "WALLPAPER_TOPIC",
    "Gtk4AppSubscriber",
    "payload_depth",
    "payload_size_bytes",
    "validate_payload",
]

#: Restart action: ``action() -> bool`` (True when every target restarted).
RestartAction = Callable[[], bool]


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

#: Versionless well-known bus name.
BUS_NAME: str = _CONTRACT["well_known_name"]

#: Object path serving the hub.
OBJECT_PATH: str = _CONTRACT["object_path"]

#: Versioned interface (version in the interface name only).
INTERFACE: str = _CONTRACT["interface"]

#: Every topic the contract advertises (the hub never emits off-table ones).
KNOWN_TOPICS: tuple[str, ...] = tuple(_CONTRACT["topics"])


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

#: The one topic this binding consumes.
WALLPAPER_TOPIC: str = "wallpaper.state"

#: Triggers that change the GTK4 palette and therefore require a restart.
RESTART_TRIGGERS: frozenset[str] = frozenset({"set", "reconcile", "reactive"})

#: Icons-only trigger: ``colors.adw.css`` is untouched, so no restart.
SKIP_TRIGGER: str = "regenerate"

# ── Consumer-side structural caps (mirror of the hub's Gate-1 caps) ────────

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
    """Consumer-side structural caps; ``None`` when the payload is dispatchable.

    Mirrors the hub's structural limits exactly (size then depth) so the two
    sides cannot drift. Returns a human-readable reason otherwise; never
    raises on hostile input.
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


# ── Wire helpers (jeepney is an adapters-only import) ──────────────────────


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
        f"type='signal',interface='{INTERFACE}',member='{DOMAIN_EVENT_SIGNAL}',path='{OBJECT_PATH}'"
    )


def _restart_match() -> str:
    return f"type='signal',interface='{INTERFACE}',member='{RESTART_SIGNAL}',path='{OBJECT_PATH}'"


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
        raise RuntimeError(f"gtk4 subscriber cannot connect to session bus: {exc}") from exc


def _restart_allowlisted_apps() -> bool:
    """Run the hardened restart over the allowlisted GTK4 apps."""
    return Gtk4AppReloader().reload()


class Gtk4AppSubscriber:
    """Hub consumer that restarts allowlisted GTK4 apps after a palette swap.

    Transport-agnostic over a jeepney blocking connection (inject one with
    ``connect=`` for hermetic tests). Tracks the ``wallpaper.state``
    ``(epoch, seq)`` baseline, drops stale signals, validates payloads
    structurally, and re-hydrates on ``JobsCleared`` / ``NameOwnerChanged``
    without replaying stale events. The restart action is injectable
    (``restart=``) so unit tests never spawn a process.
    """

    def __init__(
        self,
        *,
        connect: Callable[[], DBusConnection] | None = None,
        timeout: float = 5.0,
        restart: RestartAction | None = None,
    ) -> None:
        self._connect = connect if connect is not None else _connect_session
        self._conn: DBusConnection | None = None
        self._timeout = timeout
        self._restart: RestartAction = restart if restart is not None else _restart_allowlisted_apps
        self._hydrated: tuple[int, int] = (0, 0)
        self._last_epoch = 0
        self._started = False

    # ── Lifecycle ──────────────────────────────────────────────────────

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
            raise RuntimeError("gtk4 subscriber is not connected")
        return conn.send_and_get_reply(message, timeout=self._timeout)

    # ── Hydration ──────────────────────────────────────────────────────

    def _hydrate_all(self) -> int:
        """Re-read ``wallpaper.state``'s ``(epoch, seq)``; return its epoch."""
        self._hydrated = (0, 0)
        try:
            return self._hydrate_topic()
        except Exception as exc:  # absence/reduced function is never fatal
            logger.warning("gtk4: hydration failed for %s: %s", WALLPAPER_TOPIC, exc)
            return 0

    def _hydrate_topic(self) -> int:
        state = self._get_topic_state(WALLPAPER_TOPIC)
        epoch = _as_uint(state.get(RESERVED_EPOCH, 0))
        seq = _as_uint(state.get(RESERVED_SEQ, 0))
        self._hydrated = (epoch, seq)
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
            logger.warning("gtk4: DomainEvent arity %d != %d, dropped", len(body), len(args))
            return
        fields: dict[str, object] = {}
        for (name, signature), value in zip(args, body, strict=True):
            fields[name] = _unwrap(value) if signature == "a{sv}" else value

        topic = fields.get("topic")
        if topic != WALLPAPER_TOPIC:
            return  # not the topic this consumer tracks (unknown/off-table)
        epoch = _as_uint(fields.get("epoch", 0))
        seq = _as_uint(fields.get("seq", 0))
        if (epoch, seq) <= self._hydrated:
            return  # stale: not greater than the hydrated pair

        # Advance the baseline as soon as the pair is accepted (at-most-once):
        # a malformed payload position is still observed, never replayed.
        self._hydrated = (epoch, seq)

        payload = fields.get("payload")
        if not isinstance(payload, dict):
            logger.warning("gtk4: %s seq=%d non-object payload, dropped", topic, seq)
            return
        error = validate_payload(payload)
        if error is not None:
            logger.warning("gtk4: %s seq=%d dropped: %s", topic, seq, error)
            return
        self._dispatch(topic, payload)

    def _dispatch(self, topic: str, payload: Mapping[str, object]) -> None:
        """Apply the trigger gate, then run the injected restart action."""
        if payload.get("state") != "done":
            return
        trigger = payload.get("trigger")
        if trigger == SKIP_TRIGGER:
            logger.info(
                "gtk4: skipping restart for %s (trigger=%s): icons-only, adw CSS unchanged",
                topic,
                trigger,
            )
            return
        if trigger is not None and trigger not in RESTART_TRIGGERS:
            logger.warning("gtk4: %s done has unknown trigger %r; not restarting", topic, trigger)
            return
        # trigger is None (absent) -> conservatively palette-affecting.
        try:
            restarted = self._restart()
        except Exception:
            logger.exception("gtk4: restart action raised; contained")
            return
        if not restarted:
            logger.warning("gtk4: restart reported failure for %s (trigger=%s)", topic, trigger)

    def _on_jobs_cleared(self, epoch: int) -> None:
        """Re-hydrate after a hub (re)start; never replay or restart apps."""
        if epoch <= self._last_epoch:
            return  # not a new epoch (duplicate / replayed restart)
        self._last_epoch = epoch
        highest = self._hydrate_all()
        self._last_epoch = max(self._last_epoch, highest)

    def _on_name_owner_changed(self, body: tuple[Any, ...]) -> None:
        if len(body) != 3:
            return
        name, _old_owner, new_owner = body
        if name != BUS_NAME or not new_owner:
            return  # other name, or ownership loss (supervisor's job)
        previous = self._last_epoch
        highest = self._hydrate_all()
        self._last_epoch = max(previous, highest)

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
                logger.exception("gtk4: signal handling failed; continuing")

    def serve(self, stop_event: threading.Event) -> None:
        """Hosting entry point: blocking receive loop that exits when stopped.

        The daemon runs this on a background thread with its own bus
        connection. Every failure is logged and contained — a host thread must
        never propagate into the daemon — and a failed initial connection
        returns immediately, so the daemon degrades to "no automatic restart"
        instead of crashing. ``stop_event`` is polled between receives
        (200 ms), so host shutdown joins promptly; ``stop()`` afterwards wakes
        a blocked receive.
        """
        try:
            self.start()
        except Exception:
            logger.exception("gtk4: subscriber could not start; automatic restart disabled")
            return
        conn = self._conn
        if conn is None:  # pragma: no cover - start() guarantees a connection
            return
        while not stop_event.is_set():
            try:
                message = conn.receive(timeout=0.2)
            except TimeoutError:
                continue
            except OSError:
                if not stop_event.is_set():
                    logger.warning("gtk4: bus connection lost; subscriber stopping")
                break
            except Exception:
                logger.exception("gtk4: subscriber receive failed; continuing")
                continue
            if message.header.message_type != MessageType.signal:
                continue
            fields = message.header.fields
            interface = fields.get(HeaderFields.interface, "")
            member = fields.get(HeaderFields.member, "")
            try:
                self.handle_signal(interface, member, tuple(message.body))
            except Exception:
                logger.exception("gtk4: signal handling failed; continuing")
        self.stop()

    def stop(self) -> None:
        """Close the connection (idempotent, never raises)."""
        conn, self._conn = self._conn, None
        self._started = False
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logger.exception("gtk4: error closing bus connection")

    # ── Observability (tests / diagnostics) ────────────────────────────

    def hydrated_pair(self) -> tuple[int, int]:
        """The current ``wallpaper.state`` ``(epoch, seq)`` baseline."""
        return self._hydrated

    @property
    def last_epoch(self) -> int:
        """The highest hub epoch observed (0 = never hydrated)."""
        return self._last_epoch
