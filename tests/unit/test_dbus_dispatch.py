"""Unit tests for P5-1-2b wire dispatch + signals (no live bus).

`HubService.dispatch` is transport-free: every test calls it directly over
a real domain `EventHub` (fake clock/sink) and asserts either the
`(signature, body)` reply shape or the mapped failure. The serve loop and
name acquisition are covered with a scripted fake connection (no bus).
P5-1-2b-ii-b coverage: all five signals drained from `SignalSink` records
(including synthetic-expiry `JobFinished`), `JobsCleared` first on start
and restart, `job_adopted` dropped, the in-process `IEventSubscriber`
binding, emitter-failure containment, and the `NameOwnerChanged` restart.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any

import pytest

from runtime.adapters.dbus_event_bus import (
    SIGNALS,
    HubService,
    JeepneyNameOwner,
    SignalSink,
    WireError,
    signal_for,
    wire_error_name,
)
from runtime.domain.hub import EventHub, HubEvent
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
from runtime.ports.bus_name_owner import BUS_NAME


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _service(
    clock: _Clock | None = None, sink: list[HubEvent] | None = None
) -> tuple[HubService, _Clock, list[HubEvent], EventHub]:
    from runtime.adapters.in_process_hub import InProcessJobRegistry

    clock = clock if clock is not None else _Clock()
    events: list[HubEvent] = sink if sink is not None else []
    counter = itertools.count(1)
    hub = EventHub(
        epoch=1,
        clock=clock,
        id_factory=lambda: f"job-{next(counter)}",
        sink=events.append,
    )
    return HubService(InProcessJobRegistry(hub)), clock, events, hub


class TestHappyPaths:
    def test_begin_returns_job_id(self) -> None:
        service, _, _, _ = _service()
        signature, body = service.dispatch("BeginJob", ("capture", 30))
        assert signature == "s"
        assert body[0] == "job-1"

    def test_renew_adopt_progress_end_control(self) -> None:
        service, _, _, _ = _service()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        assert service.dispatch("RenewJob", (job,)) == ("", ())
        assert service.dispatch("AdoptJob", (job, 4242)) == ("", ())
        assert service.dispatch("ReportProgress", (job, 0.5)) == ("", ())
        assert service.dispatch("Control", (job, "pause")) == ("", ())
        assert service.dispatch("EndJob", (job, 0)) == ("", ())

    def test_get_active_jobs_shape(self) -> None:
        service, _, _, _ = _service()
        _, (live,) = service.dispatch("BeginJob", ("capture", 60))
        _, (done,) = service.dispatch("BeginJob", ("speedtest", 60))
        service.dispatch("EndJob", (done, 0))
        signature, (jobs,) = service.dispatch("GetActiveJobs", ())
        assert signature == "a{ss}"
        assert jobs == {live: "capture"}


class TestUnknownAndEnded:
    def test_unknown_job_on_all_calling_methods(self) -> None:
        service, _, _, _ = _service()
        with pytest.raises(UnknownJob):
            service.dispatch("RenewJob", ("job-nope",))
        with pytest.raises(UnknownJob):
            service.dispatch("AdoptJob", ("job-nope", 1))
        with pytest.raises(UnknownJob):
            service.dispatch("ReportProgress", ("job-nope", 0.5))
        with pytest.raises(UnknownJob):
            service.dispatch("EndJob", ("job-nope", 0))
        with pytest.raises(UnknownJob):
            service.dispatch("Control", ("job-nope", "stop"))

    def test_ended_job_on_all_calling_methods(self) -> None:
        service, _, _, _ = _service()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        service.dispatch("EndJob", (job, 0))
        with pytest.raises(JobEnded):
            service.dispatch("RenewJob", (job,))
        with pytest.raises(JobEnded):
            service.dispatch("AdoptJob", (job, 1))
        with pytest.raises(JobEnded):
            service.dispatch("ReportProgress", (job, 0.5))
        with pytest.raises(JobEnded):
            service.dispatch("EndJob", (job, 0))
        with pytest.raises(JobEnded):
            service.dispatch("Control", (job, "stop"))

    def test_stale_id_on_fresh_hub_is_unknown_job(self) -> None:
        from runtime.adapters.in_process_hub import InProcessJobRegistry

        clock = _Clock()
        events: list[HubEvent] = []
        counter = itertools.count(1)
        old = HubService(
            InProcessJobRegistry(
                EventHub(
                    epoch=1,
                    clock=clock,
                    id_factory=lambda: f"job-{next(counter)}",
                    sink=events.append,
                )
            )
        )
        _, (stale,) = old.dispatch("BeginJob", ("capture", 60))
        new, _, _, _ = _service()
        with pytest.raises(UnknownJob):
            new.dispatch("RenewJob", (stale,))


class TestWireWidths:
    @pytest.mark.parametrize("ttl", [-1, 2**32, 60.5, "60", True, None])
    def test_ttl_width_rejections(self, ttl: object) -> None:
        service, _, _, _ = _service()
        with pytest.raises(ValueError):
            service.dispatch("BeginJob", ("capture", ttl))

    def test_ttl_u32_boundaries(self) -> None:
        service, _, _, _ = _service()
        _, (zero,) = service.dispatch("BeginJob", ("capture", 0))
        assert zero.startswith("job-")
        _, (maximum,) = service.dispatch("BeginJob", ("capture", 2**32 - 1))
        assert maximum.startswith("job-")

    @pytest.mark.parametrize("pid", [-1, 0, 2**32, 4.5, "4242", True])
    def test_pid_width_rejections(self, pid: object) -> None:
        service, _, _, _ = _service()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        with pytest.raises(ValueError):
            service.dispatch("AdoptJob", (job, pid))

    @pytest.mark.parametrize("code", [2**31, -(2**31) - 1, 1.5, "0", True])
    def test_exit_code_width_rejections(self, code: object) -> None:
        service, _, _, _ = _service()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        with pytest.raises(ValueError):
            service.dispatch("EndJob", (job, code))

    def test_exit_minus_one_reserved(self) -> None:
        service, _, _, _ = _service()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        with pytest.raises(ValueError):
            service.dispatch("EndJob", (job, -1))

    @pytest.mark.parametrize("fraction", [1.5, -0.5, "half", None])
    def test_fraction_range_stays_domain_value_error(self, fraction: object) -> None:
        """No BadFraction in the typed list: a generic D-Bus error (2a rec 6)."""
        service, _, _, _ = _service()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        with pytest.raises(ValueError) as excinfo:
            service.dispatch("ReportProgress", (job, fraction))
        name, _ = wire_error_name(excinfo.value)
        assert name == "org.freedesktop.DBus.Error.InvalidArgs"

    def test_unhashable_action_is_not_controllable(self) -> None:
        service, _, _, _ = _service()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        with pytest.raises(NotControllable):
            service.dispatch("Control", (job, ["pause"]))


class TestUnboundMembers:
    @pytest.mark.parametrize("member", ["Nope", "Introspect"])
    def test_unbound_members_fail_loud(self, member: str) -> None:
        """Unbound members fail loud (Emit/GetTopicState are bound since ii-a)."""
        service, _, _, _ = _service()
        with pytest.raises(WireError) as excinfo:
            service.dispatch(member, ())
        assert excinfo.value.dbus_name == "org.freedesktop.DBus.Error.UnknownMethod"

    def test_arg_count_mismatch_is_invalid_args(self) -> None:
        """Existing method + wrong arity: InvalidArgs, not UnknownMethod."""
        service, _, _, _ = _service()
        with pytest.raises(WireError) as excinfo:
            service.dispatch("BeginJob", ("capture",))
        assert excinfo.value.dbus_name == "org.freedesktop.DBus.Error.InvalidArgs"

    def test_none_args_is_invalid_args(self) -> None:
        service, _, _, _ = _service()
        with pytest.raises(WireError) as excinfo:
            service.dispatch("GetActiveJobs", None)  # type: ignore[arg-type]
        assert excinfo.value.dbus_name == "org.freedesktop.DBus.Error.InvalidArgs"


class TestEmitDispatch:
    def _emit_service(
        self, clock: _Clock | None = None
    ) -> tuple[HubService, _Clock, list[HubEvent], EventHub]:
        from runtime.adapters.emit_validation import EmitValidator
        from runtime.adapters.in_process_hub import InProcessJobRegistry
        from runtime.domain.hub import EventHub

        clock = clock if clock is not None else _Clock()
        events: list[HubEvent] = []
        counter = itertools.count(1)
        hub = EventHub(
            epoch=3,
            clock=clock,
            id_factory=lambda: f"job-{next(counter)}",
            sink=events.append,
        )
        validator = EmitValidator(clock=clock)
        return HubService(InProcessJobRegistry(hub), validator), clock, events, hub

    def test_emit_happy_path_stores_seq_and_sink(self) -> None:
        service, _, events, _ = self._emit_service()
        assert service.dispatch(
            "Emit", ("capture.state", {"state": "recording", "elapsed_seconds": 7}), ":1.42"
        ) == ("", ())
        assert service.dispatch(
            "Emit", ("capture.state", {"state": "paused", "elapsed_seconds": 9}), ":1.42"
        ) == ("", ())
        records = [e for e in events if e.event == "domain_event"]
        assert [(e.topic, e.producer, e.seq, e.epoch) for e in records] == [
            ("capture.state", ":1.42", 1, 3),
            ("capture.state", ":1.42", 2, 3),
        ]
        assert records[0].payload == {"state": "recording", "elapsed_seconds": 7}
        _, (state,) = service.dispatch("GetTopicState", ("capture.state",))
        assert state == {"state": "paused", "elapsed_seconds": 9, "_epoch": 3, "_seq": 2}

    def test_emit_defaults_to_local_sender(self) -> None:
        service, _, events, _ = self._emit_service()
        service.dispatch("Emit", ("icme.saved", {"path": "/a"}))
        assert [e for e in events if e.event == "domain_event"][0].producer == "(local)"

    def test_emit_unknown_topic(self) -> None:
        service, _, _, _ = self._emit_service()
        with pytest.raises(UnknownTopic):
            service.dispatch("Emit", ("nope.topic", {"a": "b"}), ":1.1")
        with pytest.raises(UnknownTopic):
            service.dispatch("GetTopicState", ("nope.topic",))

    def test_emit_violations_are_payload_too_large(self) -> None:
        service, _, _, _ = self._emit_service()
        with pytest.raises(PayloadTooLarge):
            service.dispatch(
                "Emit", ("capture.state", {"state": "bogus", "elapsed_seconds": 1}), ":1.1"
            )
        with pytest.raises(PayloadTooLarge):
            service.dispatch("Emit", ("icme.saved", {"path": "x" * (64 * 1024)}), ":1.1")

    def test_emit_non_dict_payload_is_invalid_args(self) -> None:
        """Shape violations outside the typed list stay generic."""
        service, _, _, _ = self._emit_service()
        with pytest.raises(WireError) as excinfo:
            service.dispatch("Emit", ("icme.saved", "not-a-dict"), ":1.1")
        assert excinfo.value.dbus_name == "org.freedesktop.DBus.Error.InvalidArgs"

    def test_emit_rate_limited_at_61st(self) -> None:
        clock = _Clock()
        service, _, _, _ = self._emit_service(clock=clock)
        for _ in range(60):
            service.dispatch("Emit", ("icme.saved", {"path": "/a"}), ":1.7")
        with pytest.raises(RateLimited):
            service.dispatch("Emit", ("icme.saved", {"path": "/a"}), ":1.7")
        service.dispatch("Emit", ("icme.saved", {"path": "/a"}), ":1.8")

    def test_get_topic_state_never_emitted(self) -> None:
        service, _, _, _ = self._emit_service()
        _, (state,) = service.dispatch("GetTopicState", ("speedtest.finished",))
        assert state == {"_epoch": 3, "_seq": 0}

    def test_publish_binding_parity(self) -> None:
        from runtime.ports.event_bus import IEventPublisher, IEventSubscriber

        service, _, events, hub = self._emit_service()
        assert isinstance(service, IEventPublisher)
        assert isinstance(service, IEventSubscriber)
        service.publish("icme.saved", {"path": "/a"})
        assert hub.topic_state("icme.saved")["path"] == "/a"
        assert [e for e in events if e.event == "domain_event"][0].producer == "(local)"
        with pytest.raises(UnknownTopic):
            service.publish("nope.topic", {})

    def test_subscribe_binding_rejects_unknown_topic_and_non_callable(self) -> None:
        service, _, _, _ = self._emit_service()
        with pytest.raises(UnknownTopic):
            service.subscribe("nope.topic", lambda topic, payload: None)
        with pytest.raises(ValueError, match="callable"):
            service.subscribe("icme.saved", "not-callable")  # type: ignore[arg-type]


class TestSignalEmission:
    """P5-1-2b-ii-b: sink records → signals (queue-then-emit, no live bus)."""

    def _signal_service(
        self, clock: _Clock | None = None
    ) -> tuple[HubService, _Clock, list[tuple[str, str, tuple[object, ...]]]]:
        from runtime.adapters.emit_validation import EmitValidator
        from runtime.adapters.in_process_hub import InProcessJobRegistry
        from runtime.domain.hub import EventHub

        clock = clock if clock is not None else _Clock()
        sink = SignalSink()
        epochs = itertools.count(1)
        ids = itertools.count(1)

        def make() -> object:
            return InProcessJobRegistry(
                EventHub(
                    epoch=next(epochs),
                    clock=clock,
                    id_factory=lambda: f"job-{next(ids)}",
                    sink=sink,
                )
            )

        service = HubService(
            make(), EmitValidator(clock=clock), sink=sink, registry_factory=make  # type: ignore[arg-type]
        )
        emitted: list[tuple[str, str, tuple[object, ...]]] = []
        service.bind_emitter(lambda name, sig, body: emitted.append((name, sig, body)))
        return service, clock, emitted

    def test_jobs_cleared_first_on_start(self) -> None:
        service, _, emitted = self._signal_service()
        service.flush()
        assert emitted == [("JobsCleared", "u", (1,))]

    def test_lifecycle_and_domain_signals(self) -> None:
        service, _, emitted = self._signal_service()
        service.flush()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        service.dispatch("ReportProgress", (job, 0.5))
        service.dispatch("EndJob", (job, 0))
        service.dispatch(
            "Emit", ("capture.state", {"state": "recording", "elapsed_seconds": 3}), ":1.5"
        )
        assert [name for name, _, _ in emitted] == [
            "JobsCleared",
            "JobStarted",
            "JobProgress",
            "JobFinished",
            "DomainEvent",
        ]
        assert emitted[1] == ("JobStarted", "ssu", (job, "capture", 1))
        assert emitted[2] == ("JobProgress", "sdu", (job, 0.5, 1))
        assert emitted[3] == ("JobFinished", "siu", (job, 0, 1))
        name, signature, body = emitted[4]
        assert (name, signature) == ("DomainEvent", "ssuua{sv}")
        assert body == (
            "capture.state",
            ":1.5",
            1,
            1,
            {"state": ("s", "recording"), "elapsed_seconds": ("x", 3)},
        )

    def test_synthetic_expiry_binds_job_finished(self) -> None:
        service, clock, emitted = self._signal_service()
        service.flush()
        service.dispatch("BeginJob", ("capture", 10))
        clock.advance(11)
        service.dispatch("GetActiveJobs", ())
        finishes = [item for item in emitted if item[0] == "JobFinished"]
        assert finishes == [("JobFinished", "siu", ("job-1", -1, 1))]

    def test_job_adopted_has_no_wire_signal(self) -> None:
        service, _, emitted = self._signal_service()
        service.flush()
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        service.dispatch("AdoptJob", (job, 4242))
        assert [name for name, _, _ in emitted] == ["JobsCleared", "JobStarted"]

    def test_signal_for_drops_job_adopted(self) -> None:
        assert signal_for(HubEvent(event="job_adopted", job_id="j", epoch=1, pid=4)) is None
        assert signal_for(HubEvent(event="unknown_tag", job_id=None, epoch=1)) is None

    def test_all_contract_signals_are_reachable_from_sink_tags(self) -> None:
        events = [
            HubEvent(event="job_started", job_id="j", epoch=1, kind="capture"),
            HubEvent(event="job_progress", job_id="j", epoch=1, fraction=0.5),
            HubEvent(event="job_finished", job_id="j", epoch=1, exit_code=0),
            HubEvent(event="jobs_cleared", job_id=None, epoch=1),
        ]
        assert {signal_for(e)[0] for e in events} == set(SIGNALS) - {"DomainEvent"}
        assert set(SIGNALS) >= {signal_for(e)[0] for e in events}

    def test_raising_emitter_does_not_break_dispatch(self) -> None:
        """Sink-must-not-raise / Gate-1 Q7: a failed send logs and continues."""
        service, _, _ = self._signal_service()

        def _boom(name: str, signature: str, body: tuple[object, ...]) -> None:
            raise OSError("bus went away")

        service.bind_emitter(_boom)
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        assert job == "job-1"
        # The failing emitter never wedges the service or the sink below.
        assert len(service._sink) == 0  # type: ignore[arg-type]
        assert service.dispatch("GetActiveJobs", ())[1] == ({job: "capture"},)

    def test_restart_bumps_epoch_and_jobs_cleared_first(self) -> None:
        service, _, emitted = self._signal_service()
        service.flush()
        old_epoch = emitted[0][2][0]
        emitted.clear()
        service.restart()
        assert emitted == [("JobsCleared", "u", (old_epoch + 1,))]

    def test_name_owner_changed_triggers_restart_only_for_our_name(self) -> None:
        service, _, emitted = self._signal_service()
        service.flush()
        emitted.clear()
        service.handle_name_owner_changed("org.example.Other", "", ":1.2")
        service.handle_name_owner_changed(BUS_NAME, ":1.1", "")
        assert emitted == []  # other name / ownership loss: no restart
        service.handle_name_owner_changed(BUS_NAME, ":1.1", ":1.2")
        assert len(emitted) == 1 and emitted[0][0] == "JobsCleared"

    def test_subscribe_receives_domain_event_and_handler_errors_stay_contained(self) -> None:
        service, _, _ = self._signal_service()
        got: list[tuple[str, object]] = []
        service.subscribe("icme.saved", lambda topic, payload: got.append((topic, payload)))

        def _boom(topic: str, payload: object) -> None:
            raise RuntimeError("handler failed")

        service.subscribe("icme.saved", _boom)
        service.dispatch("Emit", ("icme.saved", {"path": "/a"}), ":1.7")
        assert got == [("icme.saved", {"path": "/a"})]

    def test_domain_event_signal_round_trips_over_jeepney(self) -> None:
        from jeepney import DBusAddress, Message, new_method_call, new_method_return

        service, _, emitted = self._signal_service()
        service.flush()
        service.dispatch(
            "Emit", ("capture.state", {"state": "paused", "elapsed_seconds": 9}), ":1.5"
        )
        name, signature, body = emitted[-1]
        # Reuse the out-arg a{sv} encoder the signal body mirrors, then
        # round-trip the exact members a consumer would receive.
        call = new_method_call(
            DBusAddress(
                "/org/dotfiles/Events", bus_name=":1.5", interface="org.dotfiles.Events1"
            ),
            "GetTopicState",
            "s",
            ("capture.state",),
        )
        call.header.serial = 11
        reply = new_method_return(call, signature, body)
        back = Message.from_buffer(reply.serialise(serial=12))
        topic, producer, seq, epoch, payload = back.body
        assert (topic, producer, seq, epoch) == ("capture.state", ":1.5", 1, 1)
        assert payload == {
            "state": ("s", "paused"),
            "elapsed_seconds": ("x", 9),
        }
        assert name == "DomainEvent"


class TestErrorMapping:
    def test_typed_errors_keep_contract_names_with_detail(self) -> None:
        name, text = wire_error_name(UnknownJob("job-9"))
        assert name == "org.dotfiles.Events1.UnknownJob"
        assert "job-9" in text
        name, text = wire_error_name(JobEnded("job-9"))
        assert name == "org.dotfiles.Events1.JobEnded"
        assert "job-9" in text
        name, text = wire_error_name(NotControllable("job-9", "explode"))
        assert name == "org.dotfiles.Events1.NotControllable"
        assert "explode" in text

    def test_emit_side_errors_keep_contract_names(self) -> None:
        from runtime.domain.models import PayloadTooLarge, RateLimited, UnknownTopic

        name, text = wire_error_name(UnknownTopic("nope.topic"))
        assert name == "org.dotfiles.Events1.UnknownTopic"
        assert "nope.topic" in text
        name, _ = wire_error_name(PayloadTooLarge("capture.state", 70000, "too big"))
        assert name == "org.dotfiles.Events1.PayloadTooLarge"
        name, _ = wire_error_name(RateLimited("capture.state"))
        assert name == "org.dotfiles.Events1.RateLimited"

    def test_unexpected_becomes_failed(self) -> None:
        name, _ = wire_error_name(RuntimeError("boom"))
        assert name == "org.freedesktop.DBus.Error.Failed"

    def test_wire_error_passes_through(self) -> None:
        name, _ = wire_error_name(WireError("org.freedesktop.DBus.Error.UnknownMethod", "nope"))
        assert name == "org.freedesktop.DBus.Error.UnknownMethod"


class TestSerialization:
    def test_concurrent_dispatch_stays_consistent(self) -> None:
        """AC 5 behaviorally: one dispatcher-side lock, no interleaved
        check-then-act corruption (no live bus involved)."""
        service, _, _, _ = _service()
        errors: list[BaseException] = []

        def _worker() -> None:
            try:
                for _ in range(20):
                    _, (job,) = service.dispatch("BeginJob", ("capture", 60))
                    service.dispatch("ReportProgress", (job, 0.5))
                    service.dispatch("EndJob", (job, 0))
            except BaseException as exc:  # recorded, not swallowed
                errors.append(exc)

        workers = [threading.Thread(target=_worker) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)
        assert errors == []
        _, (jobs,) = service.dispatch("GetActiveJobs", ())
        assert jobs == {}


class _FakeConn:
    """Scripted stand-in for a jeepney blocking connection (no bus)."""

    def __init__(self, request_code: int = 1, request_error: bool = False) -> None:
        from jeepney import new_error, new_method_return
        from jeepney.bus_messages import message_bus

        self.request_code = request_code
        self.sent: list[Any] = []
        self.closed = False
        self.deliver: list[Any] = []
        self._hello_reply = new_method_return(message_bus.Hello(), "s", (":1.99",))
        if request_error:
            self._request_reply = new_error(
                message_bus.RequestName("org.dotfiles.Events", flags=4),
                "org.freedesktop.DBus.Error.AccessDenied",
                "s",
                ("not permitted",),
            )
        else:
            self._request_reply = new_method_return(
                message_bus.RequestName("org.dotfiles.Events", flags=4),
                "u",
                (request_code,),
            )

    def send_and_get_reply(self, message: Any, timeout: float | None = None) -> Any:
        from jeepney import HeaderFields, new_method_return

        member = message.header.fields.get(HeaderFields.member, "")
        if member == "Hello":
            return self._hello_reply
        if member == "RequestName":
            return self._request_reply
        if member == "AddMatch":
            return new_method_return(message, None, ())
        raise AssertionError(f"unexpected bus call: {member}")

    def send_message(self, message: Any, serial: int | None = None) -> None:
        self.sent.append(message)

    def receive(self, *, timeout: float | None = None) -> Any:
        if self.deliver:
            return self.deliver.pop(0)
        if self.closed:
            raise OSError("connection closed")
        time.sleep(0.01)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def _method_call(member: str, signature: str, body: tuple[Any, ...]) -> Any:
    from jeepney import DBusAddress, new_method_call

    address = DBusAddress(
        "/org/dotfiles/Events",
        bus_name=":1.99",
        interface="org.dotfiles.Events1",
    )
    return new_method_call(address, member, signature, body)


class TestOwnerServeLoop:
    def test_contention_closes_and_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeConn(request_code=3)
        monkeypatch.setattr(
            "runtime.adapters.dbus_event_bus.open_dbus_connection", lambda **kwargs: fake
        )
        owner = JeepneyNameOwner()
        with pytest.raises(BusNameContentionError):
            owner.acquire()
        assert fake.closed is True

    def test_request_name_error_is_unavailable_not_contention(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bus refusal (error reply) must not masquerade as contention."""
        fake = _FakeConn(request_error=True)
        monkeypatch.setattr(
            "runtime.adapters.dbus_event_bus.open_dbus_connection", lambda **kwargs: fake
        )
        owner = JeepneyNameOwner()
        with pytest.raises(BusUnavailableError, match="refused"):
            owner.acquire()
        assert fake.closed is True

    def test_reacquire_after_release_serves_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The stopped flag re-arms: a second acquire serves, not silence."""
        service, _, _, _ = _service()
        fake = _FakeConn(request_code=1)
        monkeypatch.setattr(
            "runtime.adapters.dbus_event_bus.open_dbus_connection", lambda **kwargs: fake
        )
        owner = JeepneyNameOwner(service=service)
        owner.acquire()
        owner.release()
        assert fake.closed is True
        second = _FakeConn(request_code=1)
        monkeypatch.setattr(
            "runtime.adapters.dbus_event_bus.open_dbus_connection", lambda **kwargs: second
        )
        second.deliver.append(_method_call("GetActiveJobs", "", ()))
        owner.acquire()
        try:
            deadline = time.monotonic() + 5
            while len(second.sent) < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(second.sent) >= 1
        finally:
            owner.release()
        assert second.closed is True

    def test_foreign_interface_gets_unknown_method(self) -> None:
        """Our path + foreign interface: loud UnknownMethod, never ignored."""
        from jeepney import DBusAddress, MessageType, new_method_call

        service, _, _, _ = _service()
        owner = JeepneyNameOwner(service=service)
        sent: list[Any] = []

        class _SendConn:
            def send_message(self, message: Any, serial: int | None = None) -> None:
                sent.append(message)

        call = new_method_call(
            DBusAddress(
                "/org/dotfiles/Events",
                bus_name=":1.99",
                interface="org.freedesktop.DBus.Properties",
            ),
            "Get",
            "ss",
            ("org.dotfiles.Events1", "Anything"),
        )
        owner._answer(_SendConn(), service, call)  # type: ignore[arg-type]
        from jeepney import HeaderFields

        assert len(sent) == 1
        assert sent[0].header.message_type == MessageType.error
        assert (
            sent[0].header.fields.get(HeaderFields.error_name, "")
            == "org.freedesktop.DBus.Error.UnknownMethod"
        )

    def test_acquire_serves_and_release_stops(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Full serve path hermetically: a BeginJob call is dispatched and
        answered, then release stops the loop and frees everything."""
        from runtime.adapters.in_process_hub import InProcessJobRegistry
        from runtime.domain.hub import EventHub

        fake = _FakeConn(request_code=1)
        monkeypatch.setattr(
            "runtime.adapters.dbus_event_bus.open_dbus_connection", lambda **kwargs: fake
        )
        clock = _Clock()
        events: list[HubEvent] = []
        counter = itertools.count(1)
        service_owner_service = HubService(
            InProcessJobRegistry(
                EventHub(
                    epoch=1,
                    clock=clock,
                    id_factory=lambda: f"job-{next(counter)}",
                    sink=events.append,
                )
            )
        )
        owner = JeepneyNameOwner(service=service_owner_service)
        fake.deliver.append(_method_call("BeginJob", "su", ("capture", 30)))
        owner.acquire()
        try:
            deadline = time.monotonic() + 5
            while len(fake.sent) < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(fake.sent) >= 1
            reply = fake.sent[0]
            from jeepney import MessageType

            assert reply.header.message_type == MessageType.method_return
            assert reply.body[0] == "job-1"
            assert [e.event for e in events][-1] == "job_started"
        finally:
            owner.release()
        assert fake.closed is True

    def test_acquire_emits_jobs_cleared_first(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Start flush: the first bus signal after acquiring is JobsCleared."""
        from jeepney import HeaderFields, MessageType

        from runtime.adapters.in_process_hub import InProcessJobRegistry
        from runtime.domain.hub import EventHub

        fake = _FakeConn(request_code=1)
        monkeypatch.setattr(
            "runtime.adapters.dbus_event_bus.open_dbus_connection", lambda **kwargs: fake
        )
        sink = SignalSink()
        clock = _Clock()
        counter = itertools.count(1)
        service = HubService(
            InProcessJobRegistry(
                EventHub(
                    epoch=1,
                    clock=clock,
                    id_factory=lambda: f"job-{next(counter)}",
                    sink=sink,
                )
            ),
            sink=sink,
        )
        owner = JeepneyNameOwner(service=service)
        owner.acquire()
        try:
            signals = [m for m in fake.sent if m.header.message_type == MessageType.signal]
            assert len(signals) == 1
            assert signals[0].header.fields[HeaderFields.member] == "JobsCleared"
            assert signals[0].body == (1,)
        finally:
            owner.release()

    def test_double_acquire_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeConn(request_code=1)
        monkeypatch.setattr(
            "runtime.adapters.dbus_event_bus.open_dbus_connection", lambda **kwargs: fake
        )
        owner = JeepneyNameOwner()
        owner.acquire()
        try:
            with pytest.raises(RuntimeError, match="already serving"):
                owner.acquire()
        finally:
            owner.release()

    def test_release_without_acquire_is_clean(self) -> None:
        JeepneyNameOwner().release()
        JeepneyNameOwner().release()


class TestWireCodecRoundTrip:
    """The a{sv} codec must survive jeepney serialise/parse (no bus).

    Regression: _encode_out_body once wrapped the whole dict as one
    variant instead of wrapping its values — live-visible only, now
    pinned bus-free.
    """

    def test_get_topic_state_body_round_trips(self) -> None:
        from jeepney import DBusAddress, Message, new_method_call, new_method_return

        from runtime.adapters.dbus_event_bus import _encode_out_body

        plain = {"state": "recording", "elapsed_seconds": 42, "_epoch": 1, "_seq": 2}
        (encoded,) = _encode_out_body("GetTopicState", (plain,))
        call = new_method_call(
            DBusAddress("/o", bus_name=":1.1", interface="org.dotfiles.Events1"),
            "GetTopicState",
            "s",
            ("capture.state",),
        )
        call.header.serial = 7
        reply = new_method_return(call, "a{sv}", (encoded,))
        back = Message.from_buffer(reply.serialise(serial=8))
        assert back.body[0] == {
            "state": ("s", "recording"),
            "elapsed_seconds": ("x", 42),
            "_epoch": ("u", 1),
            "_seq": ("u", 2),
        }

    def test_reserved_members_force_uint32(self) -> None:
        """Contract-exact variant types: _epoch/_seq ride as `u`, not `x`."""
        from runtime.adapters.dbus_event_bus import _encode_out_body

        (encoded,) = _encode_out_body("GetTopicState", ({"_epoch": 1, "_seq": 0},))
        assert encoded == {"_epoch": ("u", 1), "_seq": ("u", 0)}

    def test_emit_in_arg_decodes(self) -> None:
        from runtime.adapters.dbus_event_bus import _decode_in_args

        wrapped = {"state": ("s", "recording"), "elapsed_seconds": ("x", 42)}
        topic, plain = _decode_in_args("Emit", ("capture.state", wrapped))
        assert topic == "capture.state"
        assert plain == {"state": "recording", "elapsed_seconds": 42}
