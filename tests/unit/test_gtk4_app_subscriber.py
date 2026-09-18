"""Unit tests for the GTK4 app consumer binding (no live bus, no processes).

`Gtk4AppSubscriber` is exercised over a scripted fake connection: `start()`
registration order (subscribe-before-read), hydration, `(epoch, seq)`
staleness, `JobsCleared`/`NameOwnerChanged` re-hydration, consumer-side
payload validation, the `trigger` gate, and the receive-loop decode path.
"""

from __future__ import annotations

import logging
import time
from typing import Any
from unittest.mock import patch

import pytest
from jeepney import DBusAddress, HeaderFields, Message, new_method_return, new_signal

from runtime.adapters import dbus_event_bus
from runtime.adapters import gtk4_app_subscriber as gs
from runtime.adapters.gtk4_app_reloader import Gtk4AppReloader
from runtime.adapters.gtk4_app_subscriber import Gtk4AppSubscriber
from runtime.domain import hub as hub_module


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


class _RestartSpy:
    """Injectable restart action that counts calls and never spawns."""

    def __init__(self, result: bool = True) -> None:
        self.calls = 0
        self.result = result

    def __call__(self) -> bool:
        self.calls += 1
        return self.result


def _subscriber(
    states: dict[str, dict[str, object]] | None = None,
    *,
    restart: Any = None,
) -> tuple[Gtk4AppSubscriber, _FakeConn, Any]:
    conn = _FakeConn(states)
    spy = restart if restart is not None else _RestartSpy()
    return Gtk4AppSubscriber(connect=lambda: conn, restart=spy), conn, spy


def _domain_event(
    topic: str,
    seq: int,
    epoch: int,
    payload: dict[str, object],
) -> tuple[Any, ...]:
    """A jeepney-shaped ``DomainEvent`` body (payload variants included)."""
    return (topic, ":1.5", seq, epoch, {key: _variant(value) for key, value in payload.items()})


def _deliver(
    sub: Gtk4AppSubscriber,
    payload: dict[str, object],
    *,
    seq: int = 1,
    epoch: int = 1,
) -> None:
    sub.handle_signal(
        gs.INTERFACE,
        "DomainEvent",
        _domain_event(gs.WALLPAPER_TOPIC, seq, epoch, payload),
    )


class TestEmbeddedContractConstants:
    """Import must not depend on the repo-root ``contracts/`` directory.

    The installed tool wheel ships only the ``runtime`` package; sourcing the
    binding's constants from the module's embedded, conformance-pinned tables
    is what lets the daemon host it there (AD-44).
    """

    def test_constants_are_the_embedded_hub_tables(self) -> None:
        assert gs.OBJECT_PATH == dbus_event_bus.OBJECT_PATH
        assert gs.INTERFACE == dbus_event_bus.INTERFACE
        assert gs.BUS_NAME == "org.dotfiles.Events"
        assert gs.KNOWN_TOPICS == hub_module.KNOWN_TOPICS
        assert gs.DOMAIN_EVENT_ARGS == dbus_event_bus.SIGNALS["DomainEvent"]

    def test_no_repo_root_contract_lookup_survives(self) -> None:
        assert not hasattr(gs, "_find_contract")
        assert not hasattr(gs, "_CONTRACT")
        assert not hasattr(gs, "_signal_args")


class TestSubscribeBeforeRead:
    def test_matches_registered_before_hydration(self) -> None:
        sub, conn, _ = _subscriber()
        sub.start()
        adds = [index for index, member in enumerate(conn.calls) if member == "AddMatch"]
        gets = [index for index, member in enumerate(conn.calls) if member == "GetTopicState"]
        assert len(adds) == 3
        assert len(gets) == 1
        assert max(adds) < min(gets), "subscribe-before-read violated"

    def test_match_rules_cover_domain_event_restart_and_owner(self) -> None:
        sub, conn, _ = _subscriber()
        sub.start()
        joined = "\n".join(conn.match_rules)
        assert "member='DomainEvent'" in joined
        assert "member='JobsCleared'" in joined
        assert "member='NameOwnerChanged'" in joined
        assert all(f"path='{gs.OBJECT_PATH}'" in rule for rule in conn.match_rules[:2])

    def test_hydrates_baseline(self) -> None:
        sub, conn, _ = _subscriber({gs.WALLPAPER_TOPIC: {"_epoch": 4, "_seq": 7}})
        sub.start()
        assert sub.hydrated_pair() == (4, 7)
        assert sub.last_epoch == 4
        assert conn.calls.count("GetTopicState") == 1

    def test_hydration_unwraps_nested_variants(self) -> None:
        sub, _, _ = _subscriber({gs.WALLPAPER_TOPIC: {"state": "done", "_epoch": 2, "_seq": 3}})
        sub.start()
        assert sub.hydrated_pair() == (2, 3)

    def test_hydration_failure_is_not_fatal(self) -> None:
        class _BoomConn(_FakeConn):
            def send_and_get_reply(self, message: Any, timeout: float | None = None) -> Any:
                member = message.header.fields.get(HeaderFields.member, "")
                if member == "GetTopicState":
                    raise RuntimeError("hub gone")
                return super().send_and_get_reply(message, timeout)

        sub = Gtk4AppSubscriber(connect=lambda: _BoomConn(), restart=_RestartSpy())
        sub.start()
        assert sub.hydrated_pair() == (0, 0)

    def test_start_is_idempotent(self) -> None:
        sub, conn, _ = _subscriber()
        sub.start()
        count = len(conn.calls)
        sub.start()
        assert len(conn.calls) == count


class TestStaleness:
    def test_fresh_signal_dispatches(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        _deliver(sub, {"state": "done", "trigger": "set"})
        assert spy.calls == 1

    def test_equal_seq_is_stale(self) -> None:
        sub, _, spy = _subscriber({gs.WALLPAPER_TOPIC: {"_epoch": 1, "_seq": 5}})
        sub.start()
        _deliver(sub, {"state": "done", "trigger": "set"}, seq=5, epoch=1)
        assert spy.calls == 0

    def test_lower_seq_is_stale(self) -> None:
        sub, _, spy = _subscriber({gs.WALLPAPER_TOPIC: {"_epoch": 1, "_seq": 5}})
        sub.start()
        _deliver(sub, {"state": "done", "trigger": "set"}, seq=3, epoch=1)
        assert spy.calls == 0

    def test_new_epoch_with_lower_seq_is_fresh(self) -> None:
        """Comparison is on the (epoch, seq) pair — never seq alone."""
        sub, _, spy = _subscriber({gs.WALLPAPER_TOPIC: {"_epoch": 5, "_seq": 99}})
        sub.start()
        _deliver(sub, {"state": "done", "trigger": "set"}, seq=1, epoch=6)
        assert spy.calls == 1

    def test_accepted_signal_advances_baseline(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        _deliver(sub, {"state": "done", "trigger": "set"}, seq=4, epoch=1)
        assert sub.hydrated_pair() == (1, 4)
        _deliver(sub, {"state": "done", "trigger": "set"}, seq=4, epoch=1)
        assert spy.calls == 1

    def test_untracked_topic_signal_ignored(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        sub.handle_signal(
            gs.INTERFACE,
            "DomainEvent",
            _domain_event("capture.state", 1, 1, {"state": "done", "trigger": "set"}),
        )
        assert spy.calls == 0

    def test_wrong_arity_signal_ignored(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        sub.handle_signal(gs.INTERFACE, "DomainEvent", (gs.WALLPAPER_TOPIC,))
        assert spy.calls == 0


class TestPayloadValidation:
    def test_oversize_payload_dropped(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        _deliver(
            sub,
            {"state": "done", "trigger": "set", "path": "x" * gs.MAX_PAYLOAD_BYTES},
        )
        assert spy.calls == 0

    def test_overdeep_payload_dropped(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        nested: dict[str, object] = {}
        node = nested
        for _ in range(gs.MAX_PAYLOAD_DEPTH + 1):
            child: dict[str, object] = {}
            node["child"] = child
            node = child
        _deliver(sub, nested)
        assert spy.calls == 0

    def test_non_object_payload_dropped(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        sub.handle_signal(gs.INTERFACE, "DomainEvent", (gs.WALLPAPER_TOPIC, ":1.5", 1, 1, "nope"))
        assert spy.calls == 0

    def test_payload_at_depth_cap_is_dispatched(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        nested: dict[str, object] = {"state": "done", "trigger": "set"}
        node = nested
        for _ in range(gs.MAX_PAYLOAD_DEPTH - 1):
            child: dict[str, object] = {}
            node["child"] = child
            node = child
        _deliver(sub, nested)
        assert spy.calls == 1


class TestTriggerGate:
    @pytest.mark.parametrize("state", ["applying", "visible", "error"])
    def test_non_done_states_never_restart(self, state: str) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        _deliver(sub, {"state": state, "trigger": "set"})
        assert spy.calls == 0

    @pytest.mark.parametrize("trigger", ["set", "reconcile", "reactive"])
    def test_palette_triggers_restart(self, trigger: str) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        _deliver(sub, {"state": "done", "trigger": trigger})
        assert spy.calls == 1

    def test_regenerate_is_skipped_and_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        with caplog.at_level(logging.INFO, logger="runtime.adapters.gtk4_app_subscriber"):
            _deliver(sub, {"state": "done", "trigger": "regenerate"})
        assert spy.calls == 0
        assert gs.WALLPAPER_TOPIC in caplog.text
        assert "regenerate" in caplog.text

    def test_missing_trigger_is_conservatively_palette_affecting(self) -> None:
        """Absent trigger + done -> restart (conservative for old publishers)."""
        sub, _, spy = _subscriber()
        sub.start()
        _deliver(sub, {"state": "done"})
        assert spy.calls == 1

    def test_unknown_trigger_is_skipped(self) -> None:
        sub, _, spy = _subscriber()
        sub.start()
        _deliver(sub, {"state": "done", "trigger": "future-thing"})
        assert spy.calls == 0

    def test_restart_failure_is_contained(self) -> None:
        sub, _, spy = _subscriber(restart=_RestartSpy(result=False))
        sub.start()
        _deliver(sub, {"state": "done", "trigger": "set"})
        assert spy.calls == 1

    def test_restart_action_exception_is_contained(self) -> None:
        def _boom() -> bool:
            raise RuntimeError("restart exploded")

        sub, _, _ = _subscriber(restart=_boom)
        sub.start()
        _deliver(sub, {"state": "done", "trigger": "set"})


class TestHubRestartRehydration:
    def test_jobs_cleared_rehydrates_without_restart(self) -> None:
        sub, conn, spy = _subscriber({gs.WALLPAPER_TOPIC: {"_epoch": 1, "_seq": 4}})
        sub.start()
        conn.states[gs.WALLPAPER_TOPIC] = {"_epoch": 2, "_seq": 0}
        sub.handle_signal(gs.INTERFACE, "JobsCleared", (2,))
        assert sub.hydrated_pair() == (2, 0)
        assert spy.calls == 0

    def test_stale_jobs_cleared_ignored(self) -> None:
        sub, conn, spy = _subscriber()
        sub.start()
        conn.states[gs.WALLPAPER_TOPIC] = {"_epoch": 1, "_seq": 0}
        sub.handle_signal(gs.INTERFACE, "JobsCleared", (1,))
        assert spy.calls == 0

    def test_restart_drops_old_epoch_and_accepts_new_epoch(self) -> None:
        sub, conn, spy = _subscriber({gs.WALLPAPER_TOPIC: {"_epoch": 1, "_seq": 9}})
        sub.start()
        conn.states[gs.WALLPAPER_TOPIC] = {"_epoch": 2, "_seq": 0}
        sub.handle_signal(gs.INTERFACE, "JobsCleared", (2,))
        # Pre-restart event at the old epoch: not replayed.
        _deliver(sub, {"state": "done", "trigger": "set"}, seq=10, epoch=1)
        assert spy.calls == 0
        # New-epoch event dispatches.
        _deliver(sub, {"state": "done", "trigger": "set"}, seq=1, epoch=2)
        assert spy.calls == 1

    def test_name_owner_changed_new_owner_rehydrates_without_restart(self) -> None:
        sub, conn, spy = _subscriber({gs.WALLPAPER_TOPIC: {"_epoch": 1, "_seq": 0}})
        sub.start()
        conn.states[gs.WALLPAPER_TOPIC] = {"_epoch": 3, "_seq": 0}
        sub.handle_signal("org.freedesktop.DBus", "NameOwnerChanged", (gs.BUS_NAME, ":1.1", ":1.2"))
        assert sub.hydrated_pair() == (3, 0)
        assert spy.calls == 0

    def test_name_owner_changed_loss_ignored(self) -> None:
        sub, conn, spy = _subscriber()
        sub.start()
        conn.states[gs.WALLPAPER_TOPIC] = {"_epoch": 3, "_seq": 0}
        sub.handle_signal("org.freedesktop.DBus", "NameOwnerChanged", (gs.BUS_NAME, ":1.1", ""))
        sub.handle_signal(
            "org.freedesktop.DBus", "NameOwnerChanged", ("org.example.Other", "", ":2")
        )
        assert sub.hydrated_pair() == (1, 0)
        assert spy.calls == 0


class TestNoTargets:
    def test_no_targets_is_a_vacuous_no_op(self) -> None:
        """The real hardened primitive with an empty app list spawns nothing."""
        conn = _FakeConn()
        sub = Gtk4AppSubscriber(
            connect=lambda: conn,
            restart=Gtk4AppReloader(app_lister=lambda: []).reload,
        )
        sub.start()
        with patch("runtime.adapters.gtk4_app_reloader.subprocess.Popen") as popen:
            _deliver(sub, {"state": "done", "trigger": "set"})
            popen.assert_not_called()


class TestReceiveLoop:
    def test_run_decodes_and_dispatches_a_real_signal(self) -> None:
        sub, conn, spy = _subscriber()
        signal = new_signal(
            DBusAddress(gs.OBJECT_PATH, interface=gs.INTERFACE),
            "DomainEvent",
            "ssuua{sv}",
            (
                gs.WALLPAPER_TOPIC,
                ":1.5",
                1,
                1,
                {"state": ("s", "done"), "trigger": ("s", "set")},
            ),
        )
        conn.deliver.append(Message.from_buffer(signal.serialise(serial=2)))
        sub.run(timeout=0.3)
        assert spy.calls == 1

    def test_stop_closes_the_connection(self) -> None:
        sub, conn, _ = _subscriber()
        sub.start()
        sub.stop()
        assert conn.closed is True
        sub.stop()  # idempotent
