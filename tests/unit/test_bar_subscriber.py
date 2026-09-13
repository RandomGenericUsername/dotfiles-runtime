"""Unit tests for the bar consumer binding (P5-2, no live bus).

`BarSubscriber` is exercised over a scripted fake connection: `start()`
registration order (subscribe-before-read), hydration, `(epoch, seq)`
staleness, `JobsCleared`/`NameOwnerChanged` re-hydration, consumer-side
payload validation, handler-exception containment, and the receive-loop
decode path (a real jeepney signal round-tripped through the wire codec).
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from jeepney import DBusAddress, HeaderFields, Message, new_method_return, new_signal

from runtime.adapters import bar_subscriber as bs
from runtime.adapters.bar_subscriber import BarSubscriber


def _variant(value: object) -> tuple[str, object]:
    """Encode a plain value the way jeepney delivers an ``a{sv}`` member."""
    if isinstance(value, bool):
        return ("b", value)
    if isinstance(value, str):
        return ("s", value)
    if isinstance(value, int):
        return ("u", value)
    if isinstance(value, float):
        return ("d", value)
    return ("s", value)


class _FakeConn:
    """Scripted stand-in for a jeepney blocking connection (no bus)."""

    def __init__(self, states: dict[str, dict[str, object]] | None = None) -> None:
        self.calls: list[str] = []
        self.match_rules: list[str] = []
        self.states: dict[str, dict[str, object]] = dict(states or {})
        self.deliver: list[Message] = []
        self.closed = False

    def send_and_get_reply(self, message: Any, timeout: float | None = None) -> Any:
        member = message.header.fields.get(HeaderFields.member, "")
        self.calls.append(member)
        if member == "AddMatch":
            self.match_rules.append(message.body[0])
            return new_method_return(message, None, ())
        if member == "GetTopicState":
            topic = message.body[0]
            state = self.states.get(topic, {"_epoch": 1, "_seq": 0})
            encoded = {key: _variant(value) for key, value in state.items()}
            return new_method_return(message, "a{sv}", (encoded,))
        raise AssertionError(f"unexpected bus call: {member}")

    def receive(self, *, timeout: float | None = None) -> Any:
        if self.deliver:
            return self.deliver.pop(0)
        if self.closed:
            raise OSError("connection closed")
        time.sleep(0.005)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def _subscriber(
    states: dict[str, dict[str, object]] | None = None,
    *,
    topics: tuple[str, ...] = bs.BAR_TOPICS,
) -> tuple[BarSubscriber, _FakeConn]:
    conn = _FakeConn(states)
    return BarSubscriber(connect=lambda: conn, topics=topics), conn


def _domain_event(
    topic: str,
    seq: int,
    epoch: int,
    payload: dict[str, object],
) -> tuple[Any, ...]:
    """A jeepney-shaped ``DomainEvent`` body (payload variants included)."""
    return (topic, ":1.5", seq, epoch, {key: _variant(value) for key, value in payload.items()})


class TestSubscribeBeforeRead:
    def test_matches_registered_before_hydration(self) -> None:
        sub, conn = _subscriber()
        sub.start()
        adds = [index for index, member in enumerate(conn.calls) if member == "AddMatch"]
        gets = [index for index, member in enumerate(conn.calls) if member == "GetTopicState"]
        assert len(adds) == 3
        assert len(gets) == len(bs.BAR_TOPICS)
        assert max(adds) < min(gets), "subscribe-before-read violated"

    def test_match_rules_cover_domain_event_restart_and_owner(self) -> None:
        sub, conn = _subscriber()
        sub.start()
        joined = "\n".join(conn.match_rules)
        assert "member='DomainEvent'" in joined
        assert "member='JobsCleared'" in joined
        assert "member='NameOwnerChanged'" in joined
        assert all(f"path='{bs.OBJECT_PATH}'" in rule for rule in conn.match_rules[:2])

    def test_hydrates_every_tracked_topic(self) -> None:
        sub, conn = _subscriber({"capture.state": {"_epoch": 4, "_seq": 7}})
        sub.start()
        assert sub.hydrated_pair("capture.state") == (4, 7)
        assert sub.hydrated_pair("speedtest.finished") == (1, 0)
        assert sub.last_epoch == 4
        assert conn.calls.count("GetTopicState") == len(bs.BAR_TOPICS)

    def test_hydration_unwraps_nested_variants(self) -> None:
        sub, _ = _subscriber({"capture.state": {"state": "recording", "_epoch": 2, "_seq": 3}})
        sub.start()
        assert sub.hydrated_pair("capture.state") == (2, 3)

    def test_start_is_idempotent(self) -> None:
        sub, conn = _subscriber()
        sub.start()
        count = len(conn.calls)
        sub.start()
        assert len(conn.calls) == count


class TestStaleness:
    def _started(
        self, states: dict[str, dict[str, object]] | None = None
    ) -> tuple[BarSubscriber, list[object]]:
        sub, _ = _subscriber(states)
        sub.start()
        got: list[object] = []
        sub.subscribe("capture.state", lambda topic, payload: got.append(payload))
        return sub, got

    def test_fresh_signal_dispatches(self) -> None:
        sub, got = self._started()
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 1, 1, {"state": "recording", "elapsed_seconds": 3}),
        )
        assert got == [{"state": "recording", "elapsed_seconds": 3}]

    def test_equal_seq_is_stale(self) -> None:
        sub, got = self._started({"capture.state": {"_epoch": 1, "_seq": 5}})
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 5, 1, {"state": "recording", "elapsed_seconds": 3}),
        )
        assert got == []

    def test_lower_seq_is_stale(self) -> None:
        sub, got = self._started({"capture.state": {"_epoch": 1, "_seq": 5}})
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 3, 1, {"state": "recording", "elapsed_seconds": 3}),
        )
        assert got == []

    def test_new_epoch_with_lower_seq_is_fresh(self) -> None:
        """Comparison is on the (epoch, seq) pair — never seq alone."""
        sub, got = self._started({"capture.state": {"_epoch": 5, "_seq": 99}})
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 1, 6, {"state": "recording", "elapsed_seconds": 3}),
        )
        assert len(got) == 1

    def test_accepted_signal_advances_baseline(self) -> None:
        sub, got = self._started()
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 4, 1, {"state": "recording", "elapsed_seconds": 3}),
        )
        assert sub.hydrated_pair("capture.state") == (1, 4)
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 4, 1, {"state": "paused", "elapsed_seconds": 4}),
        )
        assert len(got) == 1

    def test_untracked_topic_signal_ignored(self) -> None:
        sub, got = self._started()
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("icme.saved", 1, 1, {"path": "/a"}),
        )
        assert got == []

    def test_wrong_arity_signal_ignored(self) -> None:
        sub, got = self._started()
        sub.handle_signal(bs.INTERFACE, "DomainEvent", ("capture.state",))
        assert got == []


class TestPayloadValidation:
    def _started(self) -> tuple[BarSubscriber, list[object]]:
        sub, _ = _subscriber()
        sub.start()
        got: list[object] = []
        sub.subscribe("capture.state", lambda topic, payload: got.append(payload))
        return sub, got

    def test_oversize_payload_dropped(self) -> None:
        sub, got = self._started()
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event(
                "capture.state", 1, 1, {"state": "recording", "path": "x" * bs.MAX_PAYLOAD_BYTES}
            ),
        )
        assert got == []

    def test_overdeep_payload_dropped(self) -> None:
        sub, got = self._started()
        nested: dict[str, object] = {}
        node = nested
        for _ in range(bs.MAX_PAYLOAD_DEPTH + 1):
            child: dict[str, object] = {}
            node["child"] = child
            node = child
        sub.handle_signal(bs.INTERFACE, "DomainEvent", _domain_event("capture.state", 1, 1, nested))
        assert got == []

    def test_non_object_payload_dropped(self) -> None:
        sub, got = self._started()
        sub.handle_signal(bs.INTERFACE, "DomainEvent", ("capture.state", ":1.5", 1, 1, "nope"))
        assert got == []

    def test_payload_at_depth_cap_is_dispatched(self) -> None:
        sub, got = self._started()
        nested: dict[str, object] = {}
        node = nested
        for _ in range(bs.MAX_PAYLOAD_DEPTH - 1):
            child: dict[str, object] = {}
            node["child"] = child
            node = child
        sub.handle_signal(bs.INTERFACE, "DomainEvent", _domain_event("capture.state", 1, 1, nested))
        assert len(got) == 1


class TestHandlerContainment:
    def test_one_raising_handler_does_not_block_others(self) -> None:
        sub, _ = _subscriber()
        sub.start()
        seen: list[object] = []

        def _boom(topic: str, payload: object) -> None:
            raise RuntimeError("render exploded")

        sub.subscribe("capture.state", _boom)
        sub.subscribe("capture.state", lambda topic, payload: seen.append(payload))
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 1, 1, {"state": "recording", "elapsed_seconds": 1}),
        )
        assert len(seen) == 1

    def test_restart_handler_exception_contained(self) -> None:
        sub, conn = _subscriber({"capture.state": {"_epoch": 1, "_seq": 0}})
        sub.on_restart(lambda epoch: (_ for _ in ()).throw(RuntimeError("boom")))
        sub.start()
        conn.states["capture.state"] = {"_epoch": 2, "_seq": 0}
        sub.handle_signal(bs.INTERFACE, "JobsCleared", (2,))
        assert sub.hydrated_pair("capture.state") == (2, 0)


class TestRestartRehydration:
    def test_jobs_cleared_rehydrates_and_notifies(self) -> None:
        sub, conn = _subscriber({"capture.state": {"_epoch": 1, "_seq": 4}})
        restarts: list[int] = []
        sub.on_restart(restarts.append)
        sub.start()
        conn.states["capture.state"] = {"_epoch": 2, "_seq": 0}
        sub.handle_signal(bs.INTERFACE, "JobsCleared", (2,))
        assert sub.hydrated_pair("capture.state") == (2, 0)
        assert restarts == [2]

    def test_stale_jobs_cleared_ignored(self) -> None:
        sub, _ = _subscriber()
        restarts: list[int] = []
        sub.on_restart(restarts.append)
        sub.start()
        sub.handle_signal(bs.INTERFACE, "JobsCleared", (1,))
        assert restarts == []

    def test_restart_drops_old_epoch_and_accepts_new_epoch(self) -> None:
        sub, conn = _subscriber({"capture.state": {"_epoch": 1, "_seq": 9}})
        got: list[object] = []
        sub.subscribe("capture.state", lambda topic, payload: got.append(payload))
        sub.start()
        conn.states["capture.state"] = {"_epoch": 2, "_seq": 0}
        sub.handle_signal(bs.INTERFACE, "JobsCleared", (2,))
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 10, 1, {"state": "recording", "elapsed_seconds": 1}),
        )
        assert got == []
        sub.handle_signal(
            bs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 1, 2, {"state": "paused", "elapsed_seconds": 2}),
        )
        assert len(got) == 1

    def test_name_owner_changed_new_owner_rehydrates(self) -> None:
        sub, conn = _subscriber({"capture.state": {"_epoch": 1, "_seq": 0}})
        restarts: list[int] = []
        sub.on_restart(restarts.append)
        sub.start()
        conn.states["capture.state"] = {"_epoch": 3, "_seq": 0}
        sub.handle_signal(
            "org.freedesktop.DBus", "NameOwnerChanged", (bs.BUS_NAME, ":1.1", ":1.2")
        )
        assert restarts == [3]

    def test_name_owner_changed_loss_ignored(self) -> None:
        sub, _ = _subscriber()
        restarts: list[int] = []
        sub.on_restart(restarts.append)
        sub.start()
        sub.handle_signal("org.freedesktop.DBus", "NameOwnerChanged", (bs.BUS_NAME, ":1.1", ""))
        sub.handle_signal(
            "org.freedesktop.DBus", "NameOwnerChanged", ("org.example.Other", "", ":2")
        )
        assert restarts == []


class TestRegistrationErrors:
    def test_subscribe_rejects_unknown_contract_topic(self) -> None:
        sub, _ = _subscriber()
        with pytest.raises(ValueError, match="contract topic"):
            sub.subscribe("nope.topic", lambda topic, payload: None)

    def test_subscribe_rejects_non_callable(self) -> None:
        sub, _ = _subscriber()
        with pytest.raises(ValueError, match="callable"):
            sub.subscribe("capture.state", "not-callable")  # type: ignore[arg-type]

    def test_construct_rejects_non_contract_topic(self) -> None:
        with pytest.raises(ValueError, match="contract topic"):
            BarSubscriber(topics=("nope.topic",), connect=lambda: _FakeConn())

    def test_on_restart_rejects_non_callable(self) -> None:
        sub, _ = _subscriber()
        with pytest.raises(ValueError, match="callable"):
            sub.on_restart("not-callable")  # type: ignore[arg-type]


class TestReceiveLoop:
    def test_run_decodes_and_dispatches_a_real_signal(self) -> None:
        sub, conn = _subscriber()
        got: list[tuple[str, object]] = []
        sub.subscribe("capture.state", lambda topic, payload: got.append((topic, payload)))
        signal = new_signal(
            DBusAddress(bs.OBJECT_PATH, interface=bs.INTERFACE),
            "DomainEvent",
            "ssuua{sv}",
            (
                "capture.state",
                ":1.5",
                1,
                1,
                {"state": ("s", "recording"), "elapsed_seconds": ("x", 3)},
            ),
        )
        conn.deliver.append(Message.from_buffer(signal.serialise(serial=2)))
        sub.run(timeout=0.3)
        assert got == [("capture.state", {"state": "recording", "elapsed_seconds": 3})]

    def test_stop_closes_the_connection(self) -> None:
        sub, conn = _subscriber()
        sub.start()
        sub.stop()
        assert conn.closed is True
        sub.stop()  # idempotent
