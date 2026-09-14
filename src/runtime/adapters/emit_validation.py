"""Emit validation — structural + rate enforcement for hub publications (Phase 5).

P5-1-2b-ii-a: every ``Emit`` is validated BEFORE the registry call, so a
rejected publication leaves no store entry, no ``seq`` gap, and no sink
record (every ``seq`` a consumer can hydrate corresponds to a stored
payload). Order: known-topic → sv-compatibility (+ reserved keys) →
size → depth → schema → rate. Only accepted emits consume rate quota.

- **Schema:** per-topic JSON Schemas embedded from
  ``contracts/event-contract.json`` `topics` (AD-44 "embedded per side +
  gated by a repo test" — the installed tool cannot read the repo
  `contracts/` path, so embedding is mandatory), compiled once with
  ``fastjsonschema`` (already declared, the R-4 engine — no new
  dependency). ``additionalProperties`` stays open deliberately: an
  additive optional field is non-breaking on ``Events1`` (AD-34), so the
  hub must not reject future fields — it checks required presence, known
  field types, and enums.
- **Size:** UTF-8 bytes of canonical JSON (``sort_keys``, compact
  separators) — deterministic across callers, transport-independent.
- **Depth:** container nesting with the top-level dict at depth 1, cap 8.
- **sv-compatibility:** hand-check (str keys; ``str``/``bool``/``int``/
  ``float``/``list``/``dict`` leaves only — the schema compiler cannot
  express the no-opaque-binary rule). ``None`` has no D-Bus representation
  and is rejected; empty arrays are unwrappable (no element type to
  infer) and are rejected; ``int`` is range-checked to int64 (the wire
  ``x``); non-finite floats are unmeasurable and rejected. Arrays must be
  **homogeneous** — every element has to infer one signature, because an
  ``a<elem>`` wire type carries exactly one element type; heterogeneity is
  rejected recursively (nested arrays/dicts included) so the validator
  admits exactly what the wire encoder ``_variant_sig`` can serialise.
  Reserved top-level keys ``_epoch``/``_seq`` are rejected (the hub owns
  them on output).
- **Rate:** sliding 60 s window keyed on (sender, topic) at 60/min plus a
  600/min hub-wide ceiling (both named constants in code + tests, never
  in the contract files). A per-topic override table raises the per-key
  budget for contract-mandated high-cadence streams (``capture.state``
  emits >= 1/s while recording, exactly the base cap); overrides only
  raise the floor and the global ceiling remains the backstop. The sender
  half is the bus-attested unique name, never self-asserted (AD-38
  accounting, not authorization).

Every structural rejection raises typed :class:`PayloadTooLarge` (the
contract's typed list has no ``BadSchema`` — the 2b-i ``BadFraction``
precedent); quota excess raises :class:`RateLimited`; off-table topics
raise :class:`UnknownTopic`. Details ride the exceptions.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import Any

import fastjsonschema

from runtime.domain.hub import KNOWN_TOPICS
from runtime.domain.models import PayloadTooLarge, RateLimited, UnknownTopic

__all__ = [
    "MAX_PAYLOAD_BYTES",
    "MAX_PAYLOAD_DEPTH",
    "PER_TOPIC_RATE_PER_MIN",
    "RATE_GLOBAL_PER_MIN",
    "RATE_PER_KEY_PER_MIN",
    "RATE_WINDOW_S",
    "TOPIC_SCHEMAS",
    "EmitRateLimiter",
    "EmitValidator",
    "payload_depth",
    "payload_size_bytes",
    "sv_variant_signature",
]

#: Maximum canonical-JSON UTF-8 bytes per payload (2a Gate-1 ruling).
MAX_PAYLOAD_BYTES = 64 * 1024

#: Maximum container-nesting depth, top-level dict = 1 (2a Gate-1 ruling).
MAX_PAYLOAD_DEPTH = 8

#: Accepted emits per (sender, topic) per sliding window (2a Gate-1 ruling).
RATE_PER_KEY_PER_MIN = 60

#: Per-(sender, topic) accepted-emit overrides for topics whose contract
#: cadence exceeds :data:`RATE_PER_KEY_PER_MIN`. Overrides only RAISE the
#: floor (a bad entry can never weaken the base spam bound) and the
#: :data:`RATE_GLOBAL_PER_MIN` ceiling still applies as backstop.
#:
#: ``capture.state`` rationale: the contract mandates >= 1 emit/s while
#: recording (exactly 60/min), so the base cap is fully consumed and any
#: start/pause/resume/stop transition in the same window would be throttled.
#: 4x (240/min) leaves room for transition churn plus tick jitter over a
#: multi-minute recording while staying below the 600/min hub-wide ceiling
#: (>= 360/min remains available to every other topic).
PER_TOPIC_RATE_PER_MIN: Mapping[str, int] = {
    "capture.state": RATE_PER_KEY_PER_MIN * 4,
}

#: Accepted emits hub-wide per sliding window (backstop for the per-key caps).
RATE_GLOBAL_PER_MIN = 600

#: Sliding-window length in seconds.
RATE_WINDOW_S = 60.0

#: int64 bounds (wire ``x`` for inferred ints).
_I64_MIN = -(2**63)
_I64_MAX = 2**63 - 1

#: Reserved output members (owned by the hub on GetTopicState output;
#: rejected in inbound payloads).
_RESERVED_KEYS = frozenset({"_epoch", "_seq"})

#: Embedded topic schemas (mirror ``contracts/event-contract.json``
#: `topics`; pinned executable by the conformance tests).
TOPIC_SCHEMAS: dict[str, dict[str, Any]] = {
    "icme.saved": {
        "type": "object",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
        "additionalProperties": True,
    },
    "capture.state": {
        "type": "object",
        "required": ["state", "elapsed_seconds"],
        "properties": {
            "state": {"type": "string", "enum": ["idle", "recording", "paused"]},
            "elapsed_seconds": {"type": "integer"},
        },
        "additionalProperties": True,
    },
    "speedtest.finished": {
        "type": "object",
        "required": ["down_mbps", "up_mbps", "latency_ms"],
        "properties": {
            "down_mbps": {"type": "number"},
            "up_mbps": {"type": "number"},
            "latency_ms": {"type": "number"},
        },
        "additionalProperties": True,
    },
    "clipboard.update": {
        "type": "object",
        "required": ["type", "hash", "path", "preview"],
        "properties": {
            "type": {
                "type": "string",
                "enum": ["text", "image", "link", "code", "color", "emoji"],
            },
            "hash": {"type": "string"},
            "path": {"type": "string"},
            "preview": {"type": "string"},
        },
        "additionalProperties": True,
    },
    "clipboard.state": {
        "type": "object",
        "required": ["state", "job_id"],
        "properties": {
            "state": {"type": "string", "enum": ["idle", "running", "paused"]},
            "job_id": {"type": "string"},
        },
        "additionalProperties": True,
    },
}


def payload_size_bytes(payload: dict[str, object]) -> int:
    """Canonical-JSON UTF-8 length (sort_keys, compact separators)."""
    return len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def payload_depth(value: object) -> int:
    """Container nesting: top dict = 1, scalars = 0."""
    if isinstance(value, dict):
        return 1 + max((payload_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((payload_depth(item) for item in value), default=0)
    return 0


class _IncompatibleValueError(ValueError):
    """Internal: one incompatibility with its JSON-pointer-ish path."""


def sv_variant_signature(value: object, path: str = "$") -> str:
    """Validate ``a{sv}``-compatibility and infer the D-Bus variant signature.

    The single source of truth for *both* the emit validator and the wire
    encoder (``dbus_event_bus._variant_sig`` delegates here), so a value the
    validator accepts is exactly a value the encoder can serialise — they
    cannot drift. Returns the variant signature (``b``/``s``/``x``/``d``,
    ``a<elem>`` for arrays, ``a{sv}`` for dicts) and raises
    :class:`_IncompatibleValueError` for anything without a D-Bus ``a{sv}``
    representation.

    Arrays must be non-empty AND **homogeneous**: every element has to infer
    the same signature, because an ``a<elem>`` wire type carries exactly one
    element type. Heterogeneity is rejected recursively — a bad element
    inside a nested array/dict is caught at its own path.
    """
    if isinstance(value, bool):
        return "b"
    if isinstance(value, str):
        return "s"
    if isinstance(value, int):
        if not _I64_MIN <= value <= _I64_MAX:
            raise _IncompatibleValueError(f"{path}: int out of int64 range")
        return "x"
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _IncompatibleValueError(f"{path}: non-finite float")
        return "d"
    if isinstance(value, list):
        if not value:
            raise _IncompatibleValueError(f"{path}: empty array has no element type")
        element_signature = sv_variant_signature(value[0], f"{path}[0]")
        for index, item in enumerate(value[1:], start=1):
            item_signature = sv_variant_signature(item, f"{path}[{index}]")
            if item_signature != element_signature:
                raise _IncompatibleValueError(
                    f"{path}: heterogeneous array at [{index}] "
                    f"({item_signature} != {element_signature})"
                )
        return f"a{element_signature}"
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _IncompatibleValueError(f"{path}: non-string key {key!r}")
            sv_variant_signature(item, f"{path}.{key}")
        return "a{sv}"
    raise _IncompatibleValueError(f"{path}: no D-Bus representation for {type(value).__name__}")


def _check_sv_compatible(value: object, path: str) -> None:
    """Reject anything without a D-Bus ``a{sv}`` representation.

    Thin wrapper over :func:`sv_variant_signature` (the validator and the
    wire encoder share one implementation).
    """
    sv_variant_signature(value, path)


class EmitRateLimiter:
    """Sliding-window publish accounting (injected clock, deterministic).

    The effective per-(sender, topic) limit is
    ``max(per_key, per_topic.get(topic, 0))``. Per-topic overrides raise the
    budget for contract-mandated high-cadence streams (``capture.state``)
    without ever lowering the base spam bound; the global ceiling is
    unchanged and still applies.
    """

    def __init__(
        self,
        *,
        per_key: int = RATE_PER_KEY_PER_MIN,
        global_limit: int = RATE_GLOBAL_PER_MIN,
        window: float = RATE_WINDOW_S,
        clock: Callable[[], float] = time.monotonic,
        per_topic: Mapping[str, int] | None = None,
    ) -> None:
        self._per_key = per_key
        self._global_limit = global_limit
        self._window = window
        self._clock = clock
        self._per_topic: Mapping[str, int] = dict(
            PER_TOPIC_RATE_PER_MIN if per_topic is None else per_topic
        )
        self._key_hits: dict[tuple[str, str], deque[float]] = {}
        self._global_hits: deque[float] = deque()

    def per_key_limit(self, topic: str) -> int:
        """Effective per-(sender, topic) budget for ``topic`` (override-aware)."""
        return max(self._per_key, self._per_topic.get(topic, 0))

    def check(self, sender: str, topic: str) -> None:
        """Count one accepted emit or raise :class:`RateLimited`."""
        now = self._clock()
        cutoff = now - self._window
        key_hits = self._key_hits.setdefault((sender, topic), deque())
        while key_hits and key_hits[0] <= cutoff:
            key_hits.popleft()
        while self._global_hits and self._global_hits[0] <= cutoff:
            self._global_hits.popleft()
        if (
            len(key_hits) >= self.per_key_limit(topic)
            or len(self._global_hits) >= self._global_limit
        ):
            raise RateLimited(topic)
        key_hits.append(now)
        self._global_hits.append(now)


class EmitValidator:
    """Full pre-mutation validation for one Emit (schemas compiled once)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._compiled = {
            topic: fastjsonschema.compile(TOPIC_SCHEMAS[topic]) for topic in KNOWN_TOPICS
        }
        self._limiter = EmitRateLimiter(clock=clock)

    def validate(self, topic: object, payload: object, sender: str) -> None:
        """Validate; raise UnknownTopic/PayloadTooLarge/RateLimited."""
        if not isinstance(topic, str) or topic not in self._compiled:
            raise UnknownTopic(topic)
        if not isinstance(payload, dict):
            raise PayloadTooLarge(
                topic, -1, f"payload must be an object, got {type(payload).__name__}"
            )
        try:
            _check_sv_compatible(payload, "$")
        except _IncompatibleValueError as exc:
            raise PayloadTooLarge(topic, -1, str(exc)) from exc
        for reserved in _RESERVED_KEYS:
            if reserved in payload:
                raise PayloadTooLarge(topic, -1, f"reserved member {reserved!r}")
        size = payload_size_bytes(payload)
        if size > MAX_PAYLOAD_BYTES:
            raise PayloadTooLarge(topic, size, f"{size} bytes exceeds {MAX_PAYLOAD_BYTES}")
        depth = payload_depth(payload)
        if depth > MAX_PAYLOAD_DEPTH:
            raise PayloadTooLarge(topic, size, f"depth {depth} exceeds {MAX_PAYLOAD_DEPTH}")
        try:
            self._compiled[topic](payload)
        except fastjsonschema.JsonSchemaException as exc:
            raise PayloadTooLarge(topic, size, f"schema: {exc.message}") from exc
        self._limiter.check(sender, topic)
