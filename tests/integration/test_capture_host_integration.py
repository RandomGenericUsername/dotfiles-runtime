"""Integration: the production capture host against an in-process hub (5-4 N1).

Drives :class:`CaptureHost` end-to-end with a real ``EventHub`` +
``HubService`` + ``InProcessControlChannel`` and a fake recorder/clock: the
job begins, the cadence emits ``capture.state``, the hub's ``Control``
pauses/resumes/stops the real controller, and ``EndJob`` fires. No bus, no
live recorder, no real time.

The N1 proof is explicit: with the host running, ``Control`` returns success
ONLY after the action is applied; without a host-serving endpoint the hub
still fails loud (N2 preserved).
"""

from __future__ import annotations

import itertools
import threading

import pytest

from runtime.adapters.dbus_event_bus import HubService, SignalSink
from runtime.adapters.emit_validation import EmitValidator
from runtime.adapters.in_process_hub import InProcessJobRegistry
from runtime.adapters.in_process_job_client import InProcessControlChannel, InProcessJobClient
from runtime.application.capture_host import CaptureHost
from runtime.domain.hub import EventHub
from runtime.domain.models import JobEnded, UnknownJob
from runtime.ports.jobs import IRecorderProcess


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeRecorder(IRecorderProcess):
    """Records the exact lifecycle calls; owns no child."""

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


class _Harness:
    def __init__(self) -> None:
        self.clock = _Clock()
        self.sink = SignalSink()
        counter = itertools.count(1)
        self.registry = InProcessJobRegistry(
            EventHub(
                epoch=1,
                clock=self.clock,
                id_factory=lambda: f"job-{next(counter)}",
                sink=self.sink,
            )
        )
        self.channel = InProcessControlChannel()
        self.emitted: list[tuple[str, str, tuple[object, ...]]] = []
        self.received: list[dict[str, object]] = []
        self.service = HubService(
            self.registry,
            EmitValidator(clock=self.clock),
            sink=self.sink,
            control_channel=self.channel,
        )
        self.service.bind_emitter(
            lambda name, signature, body: self.emitted.append((name, signature, body))
        )
        self.service.subscribe(
            "capture.state",
            lambda _topic, payload: self.received.append(dict(payload)),
        )
        self.recorder = _FakeRecorder()
        self.client = InProcessJobClient(self.registry, self.service, control=self.channel)
        self.host = CaptureHost(
            self.client, self.recorder, clock=self.clock, ttl=1000.0, cadence=1.0
        )

    def signal_names(self) -> list[str]:
        return [name for name, _, _ in self.emitted]


class TestHostLifecycle:
    def test_begin_control_pause_resume_stop_end_job(self) -> None:
        h = _Harness()
        job_id = h.host.start()

        # Begin: hub lifecycle + domain transition, recorder owned.
        assert h.signal_names() == ["JobsCleared", "JobStarted", "DomainEvent"]
        assert h.recorder.calls == ["start"]
        assert h.host.state == "recording"
        assert h.received[-1] == {
            "state": "recording",
            "elapsed_seconds": 0,
            "job_id": job_id,
        }

        # Cadence: >= 1 emit per second while recording.
        h.clock.advance(0.5)
        assert h.host.tick() is False
        h.clock.advance(0.5)
        assert h.host.tick() is True
        assert h.received[-1]["state"] == "recording"

        # Hub Control reaches the real controller: pause -> resume -> stop.
        h.service.dispatch("Control", (job_id, "pause"))
        assert h.host.state == "paused"
        assert h.recorder.calls == ["start", "pause"]
        h.service.dispatch("Control", (job_id, "resume"))
        assert h.host.state == "recording"
        h.service.dispatch("Control", (job_id, "stop"))
        assert h.host.state == "idle"
        assert h.recorder.calls == ["start", "pause", "resume", "stop"]

        # A Control stop finalizes the hub job and asks the host to exit.
        h.service.flush()
        assert ("JobFinished", "siu", (job_id, 0, 1)) in h.emitted
        assert h.host.stop_requested() is True

    def test_paused_time_is_excluded_from_elapsed(self) -> None:
        h = _Harness()
        job_id = h.host.start()
        h.clock.advance(4.0)
        h.service.dispatch("Control", (job_id, "pause"))
        h.clock.advance(100.0)
        h.service.dispatch("Control", (job_id, "resume"))
        h.clock.advance(1.0)
        h.host.tick()
        assert h.received[-1]["elapsed_seconds"] == 5


class TestN1ControlReachesRealConsumer:
    def test_control_succeeds_only_after_the_action_is_applied(self) -> None:
        h = _Harness()
        job_id = h.host.start()
        assert h.host.state == "recording"
        # Synchronous request/response: a successful dispatch means the
        # controller already transitioned, never a silent no-op.
        h.service.dispatch("Control", (job_id, "pause"))
        assert h.host.state == "paused"

    def test_host_stop_is_idempotent_and_ends_job_once(self) -> None:
        h = _Harness()
        job_id = h.host.start()
        h.host.stop()
        h.host.stop()  # no active job: no second EndJob
        h.service.flush()
        assert h.host.state == "idle"
        assert h.recorder.calls == ["start", "stop"]
        assert h.signal_names().count("JobFinished") == 1
        assert ("JobFinished", "siu", (job_id, 0, 1)) in h.emitted


class TestN2PreservedWithoutHost:
    def test_control_without_a_serving_endpoint_still_fails_loud(self) -> None:
        """No host/handler bound ⇒ the hub raises UnknownJob, never success."""
        h = _Harness()
        # Allocate a job directly in the registry with no client/handler.
        _, (job_id,) = h.service.dispatch("BeginJob", ("capture", 60))
        with pytest.raises(UnknownJob):
            h.service.dispatch("Control", (job_id, "pause"))

    def test_control_after_host_stop_fails_loud(self) -> None:
        h = _Harness()
        job_id = h.host.start()
        h.service.dispatch("Control", (job_id, "stop"))
        with pytest.raises(JobEnded):
            h.service.dispatch("Control", (job_id, "pause"))


class TestRunCaptureHostHelper:
    def test_runs_injected_client_through_the_cli_helper(self) -> None:
        """The composition-root runner owns the same lifecycle end to end."""
        from runtime.cli.main import _run_capture_host

        h = _Harness()
        stop = threading.Event()
        stop.set()  # non-D-Bus arm blocks on the stop event: exits immediately
        code = _run_capture_host(
            command="gpu-screen-recorder -o out.mp4",
            recorder=h.recorder,
            client=h.client,
            clock=h.clock,
            stop_event=stop,
        )
        h.service.flush()
        assert code == 0
        assert h.recorder.calls == ["start", "stop"]
        names = [name for name, _, _ in h.emitted]
        assert "JobStarted" in names and "JobFinished" in names
        assert h.registry.active_jobs() == {}
