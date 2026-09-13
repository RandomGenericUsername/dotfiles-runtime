"""Unit tests for the resident capture controller (Phase 5, 5-4, AD-37).

No bus, no recorder, no real time: the hub client, recorder child, and
monotonic clock are fakes. Coverage: launch/lease, transition emissions,
monotonic elapsed across pause/resume, the >= 1/s cadence, the resident
serve loop, and the Control surface.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from runtime.application.capture import CaptureController
from runtime.domain.jobs import CAPTURE_STATES
from runtime.ports.jobs import IJobClient, IRecorderProcess


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeClient(IJobClient):
    def __init__(self) -> None:
        self.begins: list[tuple[str, float]] = []
        self.renews: list[str] = []
        self.progress: list[tuple[str, float]] = []
        self.ends: list[tuple[str, int]] = []
        self.published: list[tuple[str, dict[str, object]]] = []

    def begin(self, kind: str, ttl: float) -> str:
        self.begins.append((kind, ttl))
        return "job-1"

    def renew(self, job_id: str) -> None:
        self.renews.append(job_id)

    def report_progress(self, job_id: str, fraction: float) -> None:
        self.progress.append((job_id, fraction))

    def end(self, job_id: str, exit_code: int) -> None:
        self.ends.append((job_id, exit_code))

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        self.published.append((topic, dict(payload)))

    def states(self) -> list[dict[str, object]]:
        return [payload for topic, payload in self.published if topic == "capture.state"]


class _FakeRecorder(IRecorderProcess):
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


def _controller(
    clock: _Clock | None = None,
) -> tuple[CaptureController, _FakeClient, _FakeRecorder, _Clock]:
    clock = clock if clock is not None else _Clock()
    client = _FakeClient()
    recorder = _FakeRecorder()
    controller = CaptureController(client, recorder, clock=clock, ttl=60.0, cadence=1.0)
    return controller, client, recorder, clock


def _last_state(client: _FakeClient) -> dict[str, object]:
    return client.states()[-1]


class TestLifecycle:
    def test_start_begins_job_starts_recorder_and_emits_recording(self) -> None:
        controller, client, recorder, _ = _controller()
        job_id = controller.start()
        assert job_id == "job-1"
        assert client.begins == [("capture", 60.0)]
        assert recorder.calls == ["start"]
        assert controller.state == "recording"
        assert _last_state(client) == {
            "state": "recording",
            "elapsed_seconds": 0,
            "job_id": "job-1",
        }

    def test_pause_resume_stop_transitions_emit_and_stop_ends_job(self) -> None:
        controller, client, recorder, clock = _controller()
        controller.start()
        clock.advance(5.0)
        controller.pause()
        assert controller.state == "paused"
        assert _last_state(client)["state"] == "paused"
        assert _last_state(client)["elapsed_seconds"] == 5
        clock.advance(3.0)
        controller.resume()
        assert controller.state == "recording"
        assert recorder.calls == ["start", "pause", "resume"]
        clock.advance(2.0)
        controller.stop()
        assert controller.state == "idle"
        assert recorder.calls[-1] == "stop"
        assert client.ends == [("job-1", 0)]
        assert _last_state(client) == {
            "state": "idle",
            "elapsed_seconds": 7,
            "job_id": "job-1",
        }

    def test_paused_time_is_excluded_from_elapsed(self) -> None:
        controller, client, _, clock = _controller()
        controller.start()
        clock.advance(4.0)
        controller.pause()
        clock.advance(100.0)  # paused: must not count
        controller.resume()
        clock.advance(1.6)
        controller.tick()
        assert _last_state(client)["elapsed_seconds"] == 5

    def test_every_state_is_a_contract_state(self) -> None:
        controller, client, _, clock = _controller()
        controller.start()
        clock.advance(2.0)
        controller.pause()
        controller.resume()
        controller.stop()
        assert {payload["state"] for payload in client.states()} <= CAPTURE_STATES

    def test_invalid_transitions_raise_without_touching_recorder(self) -> None:
        controller, _, recorder, _ = _controller()
        with pytest.raises(RuntimeError, match="pause"):
            controller.pause()
        with pytest.raises(RuntimeError, match="resume"):
            controller.resume()
        with pytest.raises(RuntimeError, match="stop"):
            controller.stop()
        controller.start()
        with pytest.raises(RuntimeError, match="already active"):
            controller.start()
        with pytest.raises(RuntimeError, match="resume"):
            controller.resume()
        assert recorder.calls == ["start"]

    def test_bad_ttl_and_cadence_rejected(self) -> None:
        client, recorder = _FakeClient(), _FakeRecorder()
        with pytest.raises(ValueError, match="ttl"):
            CaptureController(client, recorder, clock=_Clock(), ttl=0)
        with pytest.raises(ValueError, match="cadence"):
            CaptureController(client, recorder, clock=_Clock(), cadence=0)


class TestRecorderStartFailure:
    def test_recorder_start_failure_ends_the_job_and_reraises(self) -> None:
        """A dead recorder must not leak the hub lease (5-4 review)."""

        class _BoomRecorder(_FakeRecorder):
            def start(self) -> None:
                raise RuntimeError("recorder backend exited during startup")

        client = _FakeClient()
        controller = CaptureController(client, _BoomRecorder(), clock=_Clock(), ttl=60.0)
        with pytest.raises(RuntimeError, match="exited during startup"):
            controller.start()
        # No lease leak: the just-begun job is ended non-zero and state is idle.
        assert client.begins == [("capture", 60.0)]
        assert client.ends == [("job-1", 1)]
        assert controller.job_id is None
        assert controller.state == "idle"

    def test_end_failure_is_contained_and_original_error_propagates(self) -> None:
        """If EndJob itself fails, the recorder error still surfaces (no mask)."""

        class _BoomRecorder(_FakeRecorder):
            def start(self) -> None:
                raise RuntimeError("recorder backend exited during startup")

        class _Flaky(_FakeClient):
            def end(self, job_id: str, exit_code: int) -> None:
                raise RuntimeError("hub went away")

        controller = CaptureController(_Flaky(), _BoomRecorder(), clock=_Clock())
        with pytest.raises(RuntimeError, match="exited during startup"):
            controller.start()
        assert controller.job_id is None


class TestCadence:
    def test_tick_emits_at_least_once_per_second_while_recording(self) -> None:
        controller, client, _, clock = _controller()
        controller.start()
        assert len(client.states()) == 1  # start transition
        clock.advance(0.5)
        assert controller.tick() is False
        assert len(client.states()) == 1
        clock.advance(0.5)
        assert controller.tick() is True
        assert len(client.states()) == 2
        clock.advance(1.0)
        assert controller.tick() is True
        assert len(client.states()) == 3
        assert [payload["state"] for payload in client.states()] == [
            "recording",
            "recording",
            "recording",
        ]

    def test_tick_does_not_emit_when_idle_or_paused(self) -> None:
        controller, client, _, clock = _controller()
        assert controller.tick() is False
        controller.start()
        controller.pause()
        emitted = len(client.states())
        clock.advance(5.0)
        assert controller.tick() is False
        assert len(client.states()) == emitted

    def test_tick_renews_the_lease(self) -> None:
        controller, client, _, clock = _controller()
        controller.start()
        clock.advance(1.0)
        controller.tick()
        assert client.renews == ["job-1"]

    def test_publish_or_renew_failure_never_stops_recording(self) -> None:
        class _Flaky(_FakeClient):
            def publish(self, topic: str, payload: Mapping[str, object]) -> None:
                raise RuntimeError("hub went away")

            def renew(self, job_id: str) -> None:
                raise RuntimeError("hub went away")

        clock = _Clock()
        client = _Flaky()
        recorder = _FakeRecorder()
        controller = CaptureController(client, recorder, clock=clock)
        controller.start()  # publish failure contained
        clock.advance(1.0)
        assert controller.tick() is True  # renew failure contained
        assert controller.state == "recording"


class TestServeLoop:
    def test_serve_ticks_until_stop_and_sleeps_the_cadence(self) -> None:
        controller, client, _, clock = _controller()
        controller.start()

        class _Loop:
            def __init__(self) -> None:
                self.sleeps = 0

            def sleep(self, seconds: float) -> None:
                assert seconds == 1.0
                self.sleeps += 1
                clock.advance(seconds)

        loop = _Loop()

        def stop() -> bool:
            return loop.sleeps >= 3

        controller.serve(stop, loop.sleep)
        # start emits 1; ticks at +1s and +2s emit 2 more; +3s loop exits.
        assert len(client.states()) == 3
        assert loop.sleeps == 3


class TestControl:
    def test_control_routes_pause_resume_stop_for_its_job(self) -> None:
        controller, client, recorder, _ = _controller()
        job_id = controller.start()
        controller.control(job_id, "pause")
        assert controller.state == "paused"
        controller.control(job_id, "resume")
        assert controller.state == "recording"
        controller.control(job_id, "stop")
        assert controller.state == "idle"
        assert recorder.calls == ["start", "pause", "resume", "stop"]

    def test_control_rejects_unknown_job_and_action(self) -> None:
        controller, _, _, _ = _controller()
        controller.start()
        with pytest.raises(RuntimeError, match="unknown capture job"):
            controller.control("job-else", "pause")
        with pytest.raises(ValueError, match="unsupported capture action"):
            controller.control("job-1", "explode")

    def test_control_after_stop_is_unknown(self) -> None:
        controller, _, _, _ = _controller()
        job_id = controller.start()
        controller.stop()
        with pytest.raises(RuntimeError, match="unknown capture job"):
            controller.control(job_id, "pause")
