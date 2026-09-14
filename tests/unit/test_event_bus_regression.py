"""Regression tests for the Phase 5 heterogeneous-array BLOCKING bug (no bus).

Reproduced at ``b1674aa``:

- ``EmitValidator`` accepted a heterogeneous array (``[1, "a"]``) because
  ``_check_sv_compatible`` validated each element independently, while the
  wire encoder ``_variant_sig`` cannot express ``a<elem>`` for mixed element
  types and raised ``RuntimeError``.
- ``HubService._drain_signals`` mapped records (``signal_for`` →
  ``_variant_sig``) OUTSIDE its guard, and ``SignalSink.drain()`` clears the
  queue before mapping. So one bad payload (wire-reachable via an ``av``-style
  variant inside an ``a{sv}``) dropped every other queued record and escaped
  into the dispatch ``finally`` as a generic ``Failed`` D-Bus error.

This module pins both layers:

1. the validator refuses heterogeneous arrays (top-level and nested) for
   EVERY known topic, before any store entry / seq increment / sink record
   (proved with ``GetTopicState`` / ``topic_state``),
2. the drain is infallible by construction: an unmappable record is logged
   and skipped, the rest are still emitted, and nothing escapes,
3. homogeneous mixed-scalar arrays (all ints / all strings / ...) still
   validate and encode (round-trip over jeepney).
"""

from __future__ import annotations

import itertools

import pytest

from runtime.adapters.dbus_event_bus import (
    HubService,
    SignalSink,
    _variant_sig,
    _wrap_value,
)
from runtime.adapters.emit_validation import EmitValidator, sv_variant_signature
from runtime.adapters.in_process_hub import InProcessJobRegistry
from runtime.domain.hub import KNOWN_TOPICS, EventHub, HubEvent
from runtime.domain.models import PayloadTooLarge

#: A schema-valid base payload per known topic, so a heterogeneous-array
#: rejection is proven on the array itself and not on a missing required
#: field (the sv check runs before the schema, but the test stays honest).
_VALID_BASE: dict[str, dict[str, object]] = {
    "icme.saved": {"path": "/a/b"},
    "capture.state": {"state": "recording", "elapsed_seconds": 1},
    "speedtest.finished": {"down_mbps": 1.0, "up_mbps": 2.0, "latency_ms": 3.0},
    "clipboard.update": {"type": "text", "hash": "h", "path": "", "preview": "hi"},
    "clipboard.state": {"state": "running", "job_id": "job-1"},
    "wallpaper.state": {"state": "applying", "wallpaper_hash": "f" * 64},
}

#: The reproduced payload shapes. ``bool`` vs ``int`` is included because
#: ``bool`` is an ``int`` subclass but infers the distinct signature ``b``.
_HETEROGENEOUS = {
    "flat": [1, "a"],
    "nested-list": [[1], ["a"]],
    "nested-dict": {"inner": [1, "a"]},
    "bool-vs-int": [1, True],
}


def _with_bad_array(payload: dict[str, object], value: object) -> dict[str, object]:
    return {**payload, "v": value}


@pytest.mark.parametrize("topic", sorted(KNOWN_TOPICS))
@pytest.mark.parametrize("shape", sorted(_HETEROGENEOUS))
def test_heterogeneous_array_rejected_for_every_topic(topic: str, shape: str) -> None:
    """Every known topic refuses a heterogeneous array via the validator."""
    validator = EmitValidator()
    payload = _with_bad_array(_VALID_BASE[topic], _HETEROGENEOUS[shape])
    with pytest.raises(PayloadTooLarge, match="heterogeneous"):
        validator.validate(topic, payload, ":1.1")


@pytest.mark.parametrize("topic", sorted(KNOWN_TOPICS))
def test_rejected_heterogeneous_emit_leaves_no_store_entry_or_seq(topic: str) -> None:
    """A refused emit must not store a payload or advance the topic ``seq``.

    ``GetTopicState`` is the public proof: a known-but-never-emitted topic
    reads back ``{_epoch, _seq: 0}``. The sink is also checked for the
    absence of any ``domain_event`` record (no store entry, seq, or sink
    record before the rejection).
    """
    events: list[HubEvent] = []
    ids = itertools.count(1)
    hub = EventHub(
        epoch=7,
        clock=lambda: 1000.0,
        id_factory=lambda: f"job-{next(ids)}",
        sink=events.append,
    )
    service = HubService(InProcessJobRegistry(hub), EmitValidator())

    payload = _with_bad_array(_VALID_BASE[topic], _HETEROGENEOUS["flat"])
    with pytest.raises(PayloadTooLarge, match="heterogeneous"):
        service.dispatch("Emit", (topic, payload), ":1.42")

    _, (state,) = service.dispatch("GetTopicState", (topic,))
    assert state == {"_epoch": 7, "_seq": 0}
    assert hub.topic_state(topic) == {"_epoch": 7, "_seq": 0}
    assert [event for event in events if event.event == "domain_event"] == []


def _sink_service() -> tuple[HubService, SignalSink, list[tuple[str, str, tuple[object, ...]]]]:
    """A real HubService draining a shared SignalSink with a recorder emitter."""
    sink = SignalSink()
    ids = itertools.count(1)
    hub = EventHub(
        epoch=5,
        clock=lambda: 1000.0,
        id_factory=lambda: f"job-{next(ids)}",
        sink=sink,
    )
    service = HubService(InProcessJobRegistry(hub), EmitValidator(), sink=sink)
    emitted: list[tuple[str, str, tuple[object, ...]]] = []
    service.bind_emitter(lambda name, signature, body: emitted.append((name, signature, body)))
    return service, sink, emitted


def _domain_event(seq: int, payload: dict[str, object]) -> HubEvent:
    return HubEvent(
        event="domain_event",
        job_id=None,
        epoch=5,
        topic="icme.saved",
        producer=":1.1",
        seq=seq,
        payload=payload,
    )


class TestDrainInfallible:
    """One unmappable record must not take down the drain (B1 defensive leg)."""

    def test_bad_record_does_not_drop_others_nor_escape(self) -> None:
        service, sink, emitted = _sink_service()
        sink.append(_domain_event(1, {"path": "/a"}))
        sink.append(_domain_event(2, {"path": "x", "v": [1, "a"]}))  # unmappable
        sink.append(_domain_event(3, {"path": "/c"}))

        service.flush()  # must not raise

        names = [name for name, _, _ in emitted]
        assert names == ["JobsCleared", "DomainEvent", "DomainEvent"]
        delivered_seqs = [body[2] for name, _, body in emitted if name == "DomainEvent"]
        assert delivered_seqs == [1, 3]

    def test_bad_record_does_not_escape_the_dispatch_finally(self) -> None:
        service, sink, emitted = _sink_service()
        sink.append(_domain_event(1, {"path": "x", "v": [1, "a"]}))  # unmappable
        sink.append(_domain_event(2, {"path": "/b"}))

        # The drain runs in dispatch()'s finally; it must not surface here.
        assert service.dispatch("GetActiveJobs", ()) == ("a{ss}", ({},))

        names = [name for name, _, _ in emitted]
        assert names == ["JobsCleared", "DomainEvent"]
        assert emitted[-1][2][2] == 2  # the record after the bad one survived

    def test_in_process_subscribers_still_delivered_for_bad_wire_payload(self) -> None:
        """Mapping failure skips only the wire send — in-process fan-out holds."""
        service, sink, _ = _sink_service()
        delivered: list[object] = []
        service.subscribe("icme.saved", lambda topic, payload: delivered.append(payload["path"]))
        sink.append(_domain_event(1, {"path": "/a"}))
        sink.append(_domain_event(2, {"path": "x", "v": [1, "a"]}))  # unmappable
        sink.append(_domain_event(3, {"path": "/c"}))

        service.flush()

        assert delivered == ["/a", "x", "/c"]


class TestHomogeneousArrays:
    @pytest.mark.parametrize(
        ("value", "signature"),
        [
            ([1, 2, 3], "ax"),
            (["a", "b"], "as"),
            ([1.5, 2.5], "ad"),
            ([True, False], "ab"),
            ([[1, 2], [3, 4]], "aax"),
        ],
    )
    def test_homogeneous_arrays_validate_and_encode(self, value: object, signature: str) -> None:
        validator = EmitValidator()
        validator.validate("icme.saved", {"path": "x", "v": value}, ":1.1")
        assert sv_variant_signature(value) == signature
        assert _variant_sig(value) == signature
        assert _wrap_value(value) == (signature, value)

    def test_homogeneous_array_signal_round_trips_over_jeepney(self) -> None:
        from jeepney import DBusAddress, Message, new_method_call, new_method_return

        service, _, emitted = _sink_service()
        service.flush()  # consume the start JobsCleared
        service.dispatch("Emit", ("icme.saved", {"path": "/a", "v": [1, 2, 3]}), ":1.5")

        name, signature, body = emitted[-1]
        assert name == "DomainEvent"
        call = new_method_call(
            DBusAddress(
                "/org/dotfiles/Events", bus_name=":1.5", interface="org.dotfiles.Events1"
            ),
            "GetTopicState",
            "s",
            ("icme.saved",),
        )
        call.header.serial = 11
        reply = new_method_return(call, signature, body)
        back = Message.from_buffer(reply.serialise(serial=12))
        topic, producer, seq, epoch, payload = back.body
        assert (topic, producer, seq, epoch) == ("icme.saved", ":1.5", 1, 5)
        assert payload == {"path": ("s", "/a"), "v": ("ax", [1, 2, 3])}
