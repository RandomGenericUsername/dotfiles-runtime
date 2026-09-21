"""Unit tests for capture-host unexpected-death detection.

The bar timer is monotonic and decoupled from the recorder child: without a
health check, a dead backend keeps publishing growing ``recording`` elapsed
for a truncated file. ``CaptureHost.check_health`` must catch that on the
cadence tick and steer the host to a nonzero ``EndJob`` + failure toast.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from runtime.application.capture_host import CaptureHost
from runtime.ports.jobs import IControllableJobClient, IRecorderProcess


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FakeClient(IControllableJobClient):
    def __init__(self) -> None:
        self.ends: list[tuple[str, int]] = []

    def begin(self, kind: str, ttl: float) -> str:
        return "job-1"

    def renew(self, job_id: str) -> None:
        pass

    def report_progress(self, job_id: str, fraction: float) -> None:
        pass

    def end(self, job_id: str, exit_code: int) -> None:
        self.ends.append((job_id, exit_code))

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        pass

    def set_control_handler(self, handler) -> None:
        pass


class _FakeRecorder(IRecorderProcess):
    """Recorder that can be killed externally to simulate a crash."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._running = False

    def start(self) -> None:
        self.calls.append("start")
        self._running = True

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def stop(self) -> None:
        self.calls.append("stop")
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def kill(self) -> None:
        """Simulate an unexpected backend death (no graceful finalize)."""
        self._running = False


def _host() -> tuple[CaptureHost, _FakeClient, _FakeRecorder]:
    client = _FakeClient()
    recorder = _FakeRecorder()
    host = CaptureHost(client, recorder, clock=_Clock())
    return host, client, recorder


class TestCheckHealth:
    def test_healthy_recording_passes(self) -> None:
        host, _, _ = _host()
        host.start()
        assert host.check_health() is True
        assert host.failed is None
        assert not host.stop_requested()

    def test_dead_recorder_marks_failed_and_requests_stop(self) -> None:
        host, _, recorder = _host()
        host.start()
        recorder.kill()
        assert host.check_health() is False
        assert host.failed == "recorder exited unexpectedly"
        assert host.stop_requested()

    def test_idle_job_never_flags(self) -> None:
        host, _, _ = _host()
        assert host.check_health() is True
        assert host.failed is None

    def test_stop_already_requested_never_flags(self) -> None:
        host, _, recorder = _host()
        host.start()
        recorder.kill()
        host.request_stop()
        assert host.check_health() is True
        assert host.failed is None

    def test_mark_failed_first_wins(self) -> None:
        host, _, _ = _host()
        host.mark_failed("first")
        host.mark_failed("second")
        assert host.failed == "first"

    def test_stop_propagates_nonzero_end(self) -> None:
        host, client, recorder = _host()
        host.start()
        recorder.kill()
        host.check_health()
        host.stop(exit_code=1)
        assert client.ends == [("job-1", 1)]
        assert recorder.calls == ["start", "stop"]

    def test_stop_defaults_to_zero(self) -> None:
        host, client, _ = _host()
        host.start()
        host.stop()
        assert client.ends == [("job-1", 0)]

    def test_probe_exception_treated_as_dead(self) -> None:
        host, _, recorder = _host()
        host.start()

        def _boom() -> bool:
            raise OSError("probe exploded")

        recorder.is_running = _boom  # type: ignore[method-assign]
        assert host.check_health() is False
        assert host.failed == "recorder exited unexpectedly"

    def test_custom_stop_event_respected(self) -> None:
        event = threading.Event()
        client = _FakeClient()
        recorder = _FakeRecorder()
        host = CaptureHost(client, recorder, clock=_Clock(), stop_event=event)
        host.start()
        recorder.kill()
        assert host.check_health() is False
        assert event.is_set()
