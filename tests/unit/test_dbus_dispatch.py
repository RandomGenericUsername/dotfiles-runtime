"""Unit tests for P5-1-2b-i wire dispatch (no live bus).

`HubService.dispatch` is transport-free: every test calls it directly over
a real domain `EventHub` (fake clock/sink) and asserts either the
`(signature, body)` reply shape or the mapped failure. The serve loop and
name acquisition are covered with a scripted fake connection (no bus).
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any

import pytest

from runtime.adapters.dbus_event_bus import (
    HubService,
    JeepneyNameOwner,
    WireError,
    wire_error_name,
)
from runtime.domain.hub import EventHub, HubEvent
from runtime.domain.models import (
    BusNameContentionError,
    BusUnavailableError,
    JobEnded,
    NotControllable,
    UnknownJob,
)


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
    @pytest.mark.parametrize("member", ["Emit", "GetTopicState", "Nope", "Introspect"])
    def test_unbound_members_fail_loud(self, member: str) -> None:
        """Emit/GetTopicState travel with 2b-ii; anything else is unknown."""
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
        from jeepney import HeaderFields

        member = message.header.fields.get(HeaderFields.member, "")
        if member == "Hello":
            return self._hello_reply
        if member == "RequestName":
            return self._request_reply
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
