"""Unit tests for the hub-side D-Bus control channel (5-4) — no live bus.

``DbusControlChannel`` is the hub's request/response path to a job: it resolves
``job_id`` to the job's bus-attested unique name and calls
``org.dotfiles.Job1.Control``. These tests pin: the outbound call target and
timeout, success only on the ack, loud ``UnknownJob`` for missing/dead/
timed-out endpoints (N2), and typed-error pass-through.
"""

from __future__ import annotations

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


def _error_reply(name: str) -> Callable[[Any], Any]:
    def factory(message: Any) -> Any:
        from jeepney import new_error

        return new_error(message, name, "s", ("nope",))

    return factory


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
