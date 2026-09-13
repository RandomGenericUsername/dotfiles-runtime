"""Unit tests for the D-Bus job client (Phase 5, 5-4) — no live bus.

A scripted fake connection stands in for jeepney; the client must call the
exact hub methods (names/signatures/order) and publish via ``Emit`` with
``a{sv}`` variant wrapping — never construct a signal. The same fake also
serves the job-side ``org.dotfiles.Job1.Control`` surface so the routing to
the registered handler is pinned without a bus.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from runtime.adapters.dbus_job_client import DbusJobClient


class _FakeConn:
    """Scripted stand-in for a jeepney blocking connection.

    Outbound method calls (``send_message`` of a method_call) are recorded in
    ``sent`` and a scripted reply is queued for ``receive``; the client's
    ``_call`` pump then reads it. Replies the client itself sends are recorded
    in ``delivered``. ``deliver`` can be preloaded with inbound calls the pump
    must serve while waiting.
    """

    def __init__(self, error_member: str | None = None) -> None:
        self.sent: list[Any] = []
        self.delivered: list[Any] = []
        self.deliver: list[Any] = []
        self.order: list[str] = []
        self.error_member = error_member
        self.closed = False
        self.outgoing_serial = itertools.count(1)

    def send_message(self, message: Any, serial: int | None = None) -> None:
        from jeepney import HeaderFields, MessageType

        if message.header.message_type == MessageType.method_call:
            if serial is not None:
                message.header.serial = serial
            member = message.header.fields.get(HeaderFields.member, "")
            self.sent.append(message)
            self.order.append(f"call:{member}")
            self.deliver.append(self._reply_for(message, member))
        else:
            self.delivered.append(message)
            self.order.append(f"reply:{message.header.message_type.name}")

    def _reply_for(self, message: Any, member: str) -> Any:
        from jeepney import new_error, new_method_return

        if member == self.error_member:
            return new_error(message, "org.dotfiles.Events1.RateLimited", "s", ("too fast",))
        if member == "BeginJob":
            return new_method_return(message, "s", ("job-7",))
        return new_method_return(message, None, ())

    def receive(self, *, timeout: float | None = None) -> Any:
        if self.deliver:
            return self.deliver.pop(0)
        raise TimeoutError

    def close(self) -> None:
        self.closed = True


def _job_call(
    member: str, signature: str, body: tuple[Any, ...], *, interface: str = "org.dotfiles.Job1"
) -> Any:
    from jeepney import DBusAddress, new_method_call

    address = DBusAddress(
        "/org/dotfiles/Job",
        bus_name=":1.77",
        interface=interface,
    )
    call = new_method_call(address, member, signature, body)
    call.header.serial = 3
    return call


def _client(conn: _FakeConn) -> DbusJobClient:
    return DbusJobClient(connect=lambda: conn, timeout=1.0)


def _body(conn: _FakeConn, index: int = -1) -> tuple[Any, ...]:
    return tuple(conn.sent[index].body)


class TestCalls:
    def test_begin_returns_job_id_and_uses_contract_signature(self) -> None:
        conn = _FakeConn()
        client = _client(conn)
        assert client.begin("capture", 60) == "job-7"
        assert _body(conn) == ("capture", 60)

    def test_renew_report_end_use_contract_signatures(self) -> None:
        conn = _FakeConn()
        client = _client(conn)
        client.renew("job-7")
        client.report_progress("job-7", 0.25)
        client.end("job-7", 0)
        assert _body(conn, 0) == ("job-7",)
        assert _body(conn, 1) == ("job-7", 0.25)
        assert _body(conn, 2) == ("job-7", 0)

    def test_publish_wraps_variants_and_never_emits_a_signal(self) -> None:
        from jeepney import MessageType

        conn = _FakeConn()
        client = _client(conn)
        client.publish(
            "capture.state",
            {"state": "recording", "elapsed_seconds": 5, "job_id": "job-7"},
        )
        (message,) = conn.sent
        assert message.header.message_type == MessageType.method_call
        topic, payload = _body(conn)
        assert topic == "capture.state"
        assert payload == {
            "state": ("s", "recording"),
            "elapsed_seconds": ("x", 5),
            "job_id": ("s", "job-7"),
        }

    def test_speedtest_payload_encodes_doubles(self) -> None:
        conn = _FakeConn()
        client = _client(conn)
        client.publish(
            "speedtest.finished",
            {"down_mbps": 100.0, "up_mbps": 50.0, "latency_ms": 12.5},
        )
        _, payload = _body(conn)
        assert payload == {
            "down_mbps": ("d", 100.0),
            "up_mbps": ("d", 50.0),
            "latency_ms": ("d", 12.5),
        }

    def test_connection_is_reused(self) -> None:
        conn = _FakeConn()
        client = _client(conn)
        client.renew("job-7")
        client.renew("job-7")
        assert len(conn.sent) == 2


class TestErrors:
    def test_error_reply_raises_with_the_error_name(self) -> None:
        conn = _FakeConn(error_member="EndJob")
        client = _client(conn)
        with pytest.raises(RuntimeError, match="RateLimited"):
            client.end("job-7", 0)

    def test_close_is_idempotent(self) -> None:
        conn = _FakeConn()
        client = _client(conn)
        client.renew("job-7")
        client.close()
        client.close()
        assert conn.closed is True


class TestServingJobControl:
    """The job serves org.dotfiles.Job1.Control to the hub (5-4)."""

    def _serving_client(self, conn: _FakeConn) -> DbusJobClient:
        client = _client(conn)
        assert client.begin("capture", 60) == "job-7"
        return client

    def test_control_routes_to_handler_and_acks(self) -> None:
        from jeepney import HeaderFields, MessageType

        conn = _FakeConn()
        client = self._serving_client(conn)
        calls: list[tuple[str, str]] = []
        client.set_control_handler(lambda job_id, action: calls.append((job_id, action)))

        client._answer(_job_call("Control", "s", ("pause",)))

        assert calls == [("job-7", "pause")]
        reply = conn.delivered[-1]
        assert reply.header.message_type == MessageType.method_return
        assert reply.header.fields.get(HeaderFields.reply_serial) == 3

    def test_control_without_handler_returns_typed_error(self) -> None:
        from jeepney import HeaderFields, MessageType

        conn = _FakeConn()
        client = _client(conn)  # never began: no job id, no handler
        client._answer(_job_call("Control", "s", ("pause",)))
        error = conn.delivered[-1]
        assert error.header.message_type == MessageType.error
        assert error.header.fields.get(HeaderFields.error_name) == "org.dotfiles.Job1.UnknownJob"

    def test_typed_handler_error_crosses_as_job_error(self) -> None:
        from jeepney import HeaderFields, MessageType

        from runtime.domain.models import NotControllable

        conn = _FakeConn()
        client = self._serving_client(conn)

        def _boom(job_id: str, action: str) -> None:
            raise NotControllable(job_id, action)

        client.set_control_handler(_boom)
        client._answer(_job_call("Control", "s", ("explode",)))
        error = conn.delivered[-1]
        assert error.header.message_type == MessageType.error
        assert (
            error.header.fields.get(HeaderFields.error_name)
            == "org.dotfiles.Job1.NotControllable"
        )

    def test_introspect_describes_job_surface(self) -> None:
        from jeepney import MessageType

        conn = _FakeConn()
        client = _client(conn)
        client._answer(
            _job_call(
                "Introspect",
                "",
                (),
                interface="org.freedesktop.DBus.Introspectable",
            )
        )
        reply = conn.delivered[-1]
        assert reply.header.message_type == MessageType.method_return
        assert 'interface name="org.dotfiles.Job1"' in str(reply.body[0])

    def test_foreign_object_is_ignored(self) -> None:
        from jeepney import DBusAddress, new_method_call

        conn = _FakeConn()
        client = _client(conn)
        call = new_method_call(
            DBusAddress(
                "/org/dotfiles/Events", bus_name=":1.77", interface="org.dotfiles.Job1"
            ),
            "Control",
            "s",
            ("pause",),
        )
        client._answer(call)
        assert conn.delivered == []

    def test_serve_loop_delivers_and_stops(self) -> None:
        conn = _FakeConn()
        client = self._serving_client(conn)
        calls: list[tuple[str, str]] = []
        client.set_control_handler(lambda job_id, action: calls.append((job_id, action)))
        conn.deliver.append(_job_call("Control", "s", ("stop",)))
        client.serve(lambda: bool(calls), timeout=0.01)
        assert calls == [("job-7", "stop")]

    def test_control_routes_to_capture_controller_pause_resume_stop(self) -> None:
        from runtime.application.capture import CaptureController
        from runtime.ports.jobs import IRecorderProcess

        class _Clock:
            def __call__(self) -> float:
                return 1000.0

        class _Recorder(IRecorderProcess):
            def __init__(self) -> None:
                self.calls: list[str] = []

            def start(self) -> None:
                self.calls.append("start")

            def pause(self) -> None:
                self.calls.append("pause")

            def resume(self) -> None:
                self.calls.append("resume")

            def stop(self) -> None:
                self.calls.append("stop")

            def is_running(self) -> bool:
                return "start" in self.calls and "stop" not in self.calls

        conn = _FakeConn()
        client = _client(conn)
        recorder = _Recorder()
        controller = CaptureController(client, recorder, clock=_Clock())
        client.set_control_handler(controller.control)
        assert controller.start() == "job-7"

        client._answer(_job_call("Control", "s", ("pause",)))
        assert controller.state == "paused"
        client._answer(_job_call("Control", "s", ("resume",)))
        assert controller.state == "recording"
        client._answer(_job_call("Control", "s", ("stop",)))
        assert controller.state == "idle"
        assert recorder.calls == ["start", "pause", "resume", "stop"]

    def test_inbound_control_is_served_while_a_job_call_waits(self) -> None:
        """The job's own reply-wait must not drop an inbound Control (N1).

        The hub can call ``Job1.Control`` while the job is waiting for its
        ``Emit``/``RenewJob`` reply; the client serves it mid-wait instead of
        swallowing it (which would strand the hub until its 5s timeout).
        """
        from jeepney import HeaderFields, MessageType

        conn = _FakeConn()
        client = self._serving_client(conn)
        seen: list[tuple[str, str]] = []
        client.set_control_handler(lambda job_id, action: seen.append((job_id, action)))

        # Arrives before the synchronized reply for the Emit below.
        conn.deliver.append(_job_call("Control", "s", ("pause",)))
        client.publish("capture.state", {"state": "recording", "elapsed_seconds": 1})

        assert seen == [("job-7", "pause")]
        assert any(
            message.header.fields.get(HeaderFields.member) == "Emit"
            for message in conn.sent
        )
        assert any(
            message.header.message_type == MessageType.method_return
            for message in conn.delivered
        )

    def test_nested_call_pump_holds_the_outer_reply_for_its_owner(self) -> None:
        """A Control handler's own call must not consume the outer call's reply.

        When a Control arrives mid-``Emit``, the handler publishes again; that
        inner ``_call`` pump can read the OUTER Emit's reply first. It must
        stash it (not drop it), so the outer call still completes.
        """
        conn = _FakeConn()
        client = self._serving_client(conn)

        def _handler(job_id: str, action: str) -> None:
            client.publish("capture.state", {"state": "paused"})

        client.set_control_handler(_handler)
        conn.deliver.append(_job_call("Control", "s", ("pause",)))
        # Outer call. Its reply is queued before the inner call's reply, so the
        # inner pump sees the outer reply first.
        client.publish("capture.state", {"state": "recording"})

        assert len(conn.sent) == 3  # BeginJob + outer Emit + inner Emit
