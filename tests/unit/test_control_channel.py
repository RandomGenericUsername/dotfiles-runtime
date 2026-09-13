"""Unit tests for the hub-side D-Bus control channel (5-4) — no live bus.

``DbusControlChannel`` is the hub's request/response path to a job: it resolves
``job_id`` to the job's bus-attested unique name and calls
``org.dotfiles.Job1.Control``. These tests pin: the outbound call target and
timeout, success only on the ack, loud ``UnknownJob`` for missing/dead/
timed-out endpoints (N2), and typed-error pass-through.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

import pytest

from runtime.adapters.dbus_event_bus import (
    JOB_INTERFACE,
    JOB_OBJECT_PATH,
    DbusControlChannel,
)
from runtime.domain.models import HubError, JobEnded, NotControllable, UnknownJob


class _FakeConn:
    """Scripted stand-in for a jeepney blocking connection."""

    def __init__(
        self,
        reply_factory: Callable[[Any], Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.sent: list[Any] = []
        self.timeout: float | None = None
        self.deliver: list[Any] = []
        self.outgoing_serial = itertools.count(1)
        self._reply_factory = reply_factory
        self._error = error

    def send_and_get_reply(self, message: Any, timeout: float | None = None) -> Any:
        from jeepney import new_method_return

        self.sent.append(message)
        self.timeout = timeout
        if self._error is not None:
            raise self._error
        if self._reply_factory is not None:
            return self._reply_factory(message)
        return new_method_return(message, None, ())

    def send_message(self, message: Any, serial: int | None = None) -> None:
        """Pumping path: record the call and queue its reply for ``receive``."""
        from jeepney import new_method_return

        if serial is not None:
            message.header.serial = serial
        self.sent.append(message)
        self.deliver.append(new_method_return(message, None, ()))

    def receive(self, *, timeout: float | None = None) -> Any:
        if self.deliver:
            return self.deliver.pop(0)
        raise TimeoutError


def _error_reply(name: str) -> Callable[[Any], Any]:
    def factory(message: Any) -> Any:
        from jeepney import new_error

        return new_error(message, name, "s", ("nope",))

    return factory


class _PumpConn:
    """Fake connection whose receive queue is scripted by the test (no auto-ack)."""

    def __init__(self) -> None:
        self.outgoing_serial = itertools.count(1)
        self.sent: list[Any] = []
        self.deliver: list[Any] = []

    def send_message(self, message: Any, serial: int | None = None) -> None:
        if serial is not None:
            message.header.serial = serial
        self.sent.append(message)

    def receive(self, *, timeout: float | None = None) -> Any:
        if self.deliver:
            return self.deliver.pop(0)
        raise TimeoutError


def _reply_with_serial(serial: int) -> Any:
    """A method_return whose ``reply_serial`` is ``serial`` (a craftable ack)."""
    from jeepney import DBusAddress, HeaderFields, new_method_call, new_method_return

    template = new_method_call(
        DBusAddress(
            "/org/dotfiles/Events",
            bus_name="org.dotfiles.Events",
            interface="org.dotfiles.Events1",
        ),
        "Noop",
        "",
        (),
    )
    reply = new_method_return(template, None, ())
    reply.header.fields[HeaderFields.reply_serial] = serial
    return reply


def _inbound_control_call(action: str) -> Any:
    from jeepney import DBusAddress, new_method_call

    return new_method_call(
        DBusAddress(
            "/org/dotfiles/Events",
            bus_name="org.dotfiles.Events",
            interface="org.dotfiles.Events1",
        ),
        "Control",
        "ss",
        ("job-2", action),
    )


class TestDbusControlChannel:
    def test_unbound_endpoint_fails_loud(self) -> None:
        channel = DbusControlChannel()
        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")

    def test_connection_bound_but_no_endpoint_fails_loud(self) -> None:
        channel = DbusControlChannel()
        channel.bind_connection(_FakeConn())
        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")

    def test_success_on_job_ack_calls_the_unique_name(self) -> None:
        from jeepney import HeaderFields, MessageType

        conn = _FakeConn()
        channel = DbusControlChannel(timeout=7.5)
        channel.bind_connection(conn)
        channel.bind("job-1", ":1.42")

        channel.send_control("job-1", "pause")

        (message,) = conn.sent
        assert message.header.message_type == MessageType.method_call
        assert message.header.fields.get(HeaderFields.destination) == ":1.42"
        assert message.header.fields.get(HeaderFields.path) == JOB_OBJECT_PATH
        assert message.header.fields.get(HeaderFields.interface) == JOB_INTERFACE
        assert message.header.fields.get(HeaderFields.member) == "Control"
        assert tuple(message.body) == ("pause",)
        assert conn.timeout == 7.5

    def test_timeout_fails_loud_and_drops_the_endpoint(self) -> None:
        conn = _FakeConn(error=TimeoutError("no reply"))
        channel = DbusControlChannel()
        channel.bind_connection(conn)
        channel.bind("job-1", ":1.42")

        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")
        # Stale mapping dropped: even with the connection still bound, a
        # second call fails loud instead of calling a dead endpoint.
        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")

    def test_dead_peer_fails_loud(self) -> None:
        conn = _FakeConn(error=ConnectionResetError("peer vanished"))
        channel = DbusControlChannel()
        channel.bind_connection(conn)
        channel.bind("job-1", ":1.42")
        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")

    @pytest.mark.parametrize(
        ("error_name", "expected"),
        [
            ("org.dotfiles.Job1.NotControllable", NotControllable),
            ("org.dotfiles.Job1.JobEnded", JobEnded),
            ("org.dotfiles.Job1.UnknownJob", UnknownJob),
        ],
    )
    def test_typed_job_error_crosses_through(
        self, error_name: str, expected: type[Exception]
    ) -> None:
        channel = DbusControlChannel()
        channel.bind_connection(_FakeConn(reply_factory=_error_reply(error_name)))
        channel.bind("job-1", ":1.42")
        with pytest.raises(expected):
            channel.send_control("job-1", "pause")

    def test_untyped_job_error_is_a_hub_error(self) -> None:
        channel = DbusControlChannel()
        channel.bind_connection(_FakeConn(reply_factory=_error_reply("org.example.Boom")))
        channel.bind("job-1", ":1.42")
        with pytest.raises(HubError):
            channel.send_control("job-1", "pause")

    def test_forget_endpoint_drops_only_that_owner(self) -> None:
        conn = _FakeConn()
        channel = DbusControlChannel()
        channel.bind_connection(conn)
        channel.bind("job-1", ":1.42")
        channel.bind("job-2", ":1.99")
        channel.forget_endpoint(":1.42")
        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")
        channel.send_control("job-2", "pause")  # untouched
        assert len(conn.sent) == 1

    def test_unbind_drops_the_mapping(self) -> None:
        channel = DbusControlChannel()
        channel.bind_connection(_FakeConn())
        channel.bind("job-1", ":1.42")
        channel.unbind("job-1")
        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")

    def test_close_clears_connection_and_endpoints(self) -> None:
        channel = DbusControlChannel()
        channel.bind_connection(_FakeConn())
        channel.bind("job-1", ":1.42")
        channel.close()
        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")

    def test_outer_ack_is_stashed_across_a_nested_pump(self) -> None:
        """A nested Control's pump must not swallow the outer Control's ack.

        Two interleaved ``Control`` calls share one connection. When the outer
        pump serves an interleaved message that starts a nested Control, the
        nested pump reads the OUTER ack first; it must stash it for the outer
        pump instead of dropping it (reply mis-routing).
        """
        channel = DbusControlChannel()
        conn = _PumpConn()
        channel.bind_connection(conn)
        channel.bind("job-1", ":1.42")
        channel.bind("job-2", ":1.43")

        served: list[Any] = []

        def serve(msg: Any) -> None:
            served.append(msg)
            if len(served) == 1:
                channel.send_control("job-2", "stop")

        conn.deliver.append(_inbound_control_call("pause"))  # outer pumps this
        conn.deliver.append(_reply_with_serial(1))  # outer ack (nested reads it)
        conn.deliver.append(_reply_with_serial(2))  # nested ack

        channel.set_serve_callback(serve)
        channel.send_control("job-1", "pause")  # must NOT raise UnknownJob

        from jeepney import HeaderFields

        members = [m.header.fields.get(HeaderFields.member) for m in conn.sent]
        assert members == ["Control", "Control"]

    def test_control_pump_serves_interleaved_messages_until_ack(self) -> None:
        """The hub keeps serving the job's calls while waiting for the ack.

        A job reports back (Emit/EndJob) from inside its Control handler; the
        hub must dispatch those interleaved messages rather than block and
        drop them (N1 re-entrancy).
        """
        from jeepney import DBusAddress, HeaderFields, new_method_call

        conn = _FakeConn()
        channel = DbusControlChannel()
        channel.bind_connection(conn)
        channel.bind("job-1", ":1.42")
        served: list[Any] = []
        channel.set_serve_callback(served.append)

        interleaved = new_method_call(
            DBusAddress(
                "/org/dotfiles/Events",
                bus_name="org.dotfiles.Events",
                interface="org.dotfiles.Events1",
            ),
            "Emit",
            "sa{sv}",
            ("capture.state", {}),
        )
        conn.deliver.append(interleaved)

        channel.send_control("job-1", "pause")

        assert served == [interleaved]
        (sent,) = conn.sent
        assert sent.header.fields.get(HeaderFields.member) == "Control"
