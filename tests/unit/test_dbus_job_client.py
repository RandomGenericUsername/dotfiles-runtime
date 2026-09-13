"""Unit tests for the D-Bus job client (Phase 5, 5-4) — no live bus.

A scripted fake connection stands in for jeepney; the client must call the
exact hub methods (names/signatures/order) and publish via ``Emit`` with
``a{sv}`` variant wrapping — never construct a signal.
"""

from __future__ import annotations

from typing import Any

import pytest

from runtime.adapters.dbus_job_client import DbusJobClient


class _FakeConn:
    """Scripted stand-in for a jeepney blocking connection."""

    def __init__(self, error_member: str | None = None) -> None:
        self.sent: list[Any] = []
        self.error_member = error_member
        self.closed = False

    def send_and_get_reply(self, message: Any, timeout: float | None = None) -> Any:
        from jeepney import HeaderFields, new_error, new_method_return

        member = message.header.fields.get(HeaderFields.member, "")
        self.sent.append(message)
        if member == self.error_member:
            return new_error(message, "org.dotfiles.Events1.RateLimited", "s", ("too fast",))
        if member == "BeginJob":
            return new_method_return(message, "s", ("job-7",))
        return new_method_return(message, None, ())

    def close(self) -> None:
        self.closed = True


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
