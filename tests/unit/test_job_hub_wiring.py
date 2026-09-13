"""End-to-end hub wiring for lifetime jobs (Phase 5, 5-4, AD-37/AD-38).

The controller reports through the hub's methods and publishes DOMAIN
events through the hub's validated ``Emit``; the hub — not the job — emits
``JobStarted``/``JobFinished`` and ``DomainEvent``. ``Control`` reaches the
co-hosted job via the hub's control sink, so the hub is the single control
path. No live bus: a real ``EventHub`` + ``HubService`` with fake
clock/recorder and a signal recorder.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from runtime.adapters.dbus_event_bus import HubService, SignalSink
from runtime.adapters.emit_validation import EmitValidator
from runtime.adapters.in_process_hub import InProcessJobRegistry
from runtime.adapters.in_process_job_client import InProcessControlChannel, InProcessJobClient
from runtime.application.capture import CaptureController
from runtime.domain.hub import EventHub
from runtime.domain.models import JobEnded, UnknownJob
from runtime.ports.jobs import IRecorderProcess

_APPLICATION_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "runtime" / "src" / "runtime" / "application"
)


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeRecorder(IRecorderProcess):
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


def _service(
    clock: _Clock,
) -> tuple[
    HubService,
    InProcessJobRegistry,
    list[tuple[str, str, tuple[object, ...]]],
    InProcessControlChannel,
]:
    sink = SignalSink()
    counter = itertools.count(1)
    registry = InProcessJobRegistry(
        EventHub(
            epoch=1,
            clock=clock,
            id_factory=lambda: f"job-{next(counter)}",
            sink=sink,
        )
    )
    channel = InProcessControlChannel()
    service = HubService(
        registry, EmitValidator(clock=clock), sink=sink, control_channel=channel
    )
    emitted: list[tuple[str, str, tuple[object, ...]]] = []
    service.bind_emitter(lambda name, signature, body: emitted.append((name, signature, body)))
    return service, registry, emitted, channel


class TestControlWiring:
    def _setup(
        self,
    ) -> tuple[
        HubService,
        CaptureController,
        _FakeRecorder,
        _Clock,
        list[tuple[str, str, tuple[object, ...]]],
        list[dict[str, object]],
    ]:
        clock = _Clock()
        service, registry, emitted, channel = _service(clock)
        recorder = _FakeRecorder()
        client = InProcessJobClient(registry, service, control=channel)
        controller = CaptureController(client, recorder, clock=clock)
        client.set_control_handler(controller.control)
        received: list[dict[str, object]] = []
        service.subscribe(
            "capture.state",
            lambda topic, payload: received.append(dict(payload)),
        )
        return service, controller, recorder, clock, emitted, received

    def test_controller_drives_hub_methods_and_domain_events(self) -> None:
        service, controller, recorder, _, emitted, received = self._setup()
        job_id = controller.start()
        names = [name for name, _, _ in emitted]
        assert names == ["JobsCleared", "JobStarted", "DomainEvent"]
        assert emitted[1] == ("JobStarted", "ssu", (job_id, "capture", 1))
        domain = emitted[2]
        assert domain[0] == "DomainEvent"
        assert domain[2][0] == "capture.state"
        assert domain[2][4]["state"] == ("s", "recording")
        assert domain[2][4]["job_id"] == ("s", job_id)
        assert received == [{"state": "recording", "elapsed_seconds": 0, "job_id": job_id}]
        assert recorder.calls == ["start"]

    def test_control_action_reaches_job_and_emits_transition(self) -> None:
        service, controller, _recorder, _, _emitted, received = self._setup()
        job_id = controller.start()
        service.dispatch("Control", (job_id, "pause"))
        assert controller.state == "paused"
        assert received[-1] == {"state": "paused", "elapsed_seconds": 0, "job_id": job_id}
        service.dispatch("Control", (job_id, "resume"))
        assert controller.state == "recording"
        assert [payload["state"] for payload in received] == ["recording", "paused", "recording"]

    def test_control_stop_ends_job_and_flushes_finish(self) -> None:
        service, controller, recorder, _, emitted, _ = self._setup()
        job_id = controller.start()
        service.dispatch("Control", (job_id, "stop"))
        service.flush()
        assert controller.state == "idle"
        assert recorder.calls == ["start", "stop"]
        names = [name for name, _, _ in emitted]
        assert names.count("JobFinished") == 1
        assert ("JobFinished", "siu", (job_id, 0, 1)) in emitted

    def test_denied_action_never_reaches_job(self) -> None:
        from runtime.domain.models import NotControllable

        service, controller, recorder, _, _, _ = self._setup()
        job_id = controller.start()
        try:
            service.dispatch("Control", (job_id, "explode"))
        except NotControllable:
            pass
        else:  # pragma: no cover
            raise AssertionError("explode must be NotControllable")
        assert controller.state == "recording"
        assert recorder.calls == ["start"]

    def test_ui_indicator_is_driven_by_domain_events_only(self) -> None:
        """A domain subscriber never sees JobStarted/JobFinished (AD-37)."""
        service, controller, _, _, emitted, received = self._setup()
        controller.start()
        controller.stop()
        service.flush()
        names = [name for name, _, _ in emitted]
        assert "JobStarted" in names and "JobFinished" in names
        # The capture.state subscriber observed only domain transitions.
        assert received, "domain events must flow to subscribers"
        assert all(set(payload) <= {"state", "elapsed_seconds", "job_id"} for payload in received)


class TestDomainEventsFlowThroughHub:
    def test_application_modules_never_touch_the_bus(self) -> None:
        offenders: list[str] = []
        for path in sorted(_APPLICATION_DIR.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name.split(".")[0] in {"jeepney", "dbus", "gi"}:
                        offenders.append(f"{path.name}:{node.lineno}: {name}")
        assert offenders == []

    def test_capture_state_job_id_is_an_additive_validated_field(self) -> None:
        validator = EmitValidator()
        validator.validate(
            "capture.state",
            {"state": "recording", "elapsed_seconds": 5, "job_id": "job-1"},
            "(local)",
        )


class TestInProcessControlChannel:
    """The in-process channel is the parity arm of the hub→job port (5-4)."""

    def test_unknown_job_fails_loud(self) -> None:
        channel = InProcessControlChannel()
        with pytest.raises(UnknownJob):
            channel.send_control("job-nope", "pause")

    def test_live_job_without_endpoint_fails_loud(self) -> None:
        """N2: the domain knows the job but no endpoint is bound."""
        clock = _Clock()
        service, _registry, _emitted, _channel = _service(clock)
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        with pytest.raises(UnknownJob):
            service.dispatch("Control", (job, "pause"))

    def test_bound_handler_receives_action_and_unbind_removes_it(self) -> None:
        channel = InProcessControlChannel()
        calls: list[tuple[str, str]] = []
        channel.bind("job-1", lambda job_id, action: calls.append((job_id, action)))
        channel.send_control("job-1", "pause")
        assert calls == [("job-1", "pause")]
        channel.unbind("job-1")
        with pytest.raises(UnknownJob):
            channel.send_control("job-1", "pause")

    def test_job_client_binds_and_unbinds_on_lifecycle(self) -> None:
        clock = _Clock()
        service, registry, _emitted, channel = _service(clock)
        recorder = _FakeRecorder()
        client = InProcessJobClient(registry, service, control=channel)
        controller = CaptureController(client, recorder, clock=clock)
        client.set_control_handler(controller.control)

        job_id = controller.start()
        service.dispatch("Control", (job_id, "pause"))
        assert controller.state == "paused"
        service.dispatch("Control", (job_id, "stop"))
        assert controller.state == "idle"
        # EndJob ended the job: a second Control fails loud.
        with pytest.raises(JobEnded):
            service.dispatch("Control", (job_id, "resume"))

    def test_job_handler_failure_is_surfaced_not_reported_as_success(self) -> None:
        service, controller, _recorder, _clock, _emitted, _received = TestControlWiring()._setup()
        job_id = controller.start()
        # resume is invalid while recording: the job raises, the hub must not
        # turn that into a positive ack.
        with pytest.raises(RuntimeError, match="cannot resume"):
            service.dispatch("Control", (job_id, "resume"))
        # The hub stays healthy after the failed delivery.
        assert service.dispatch("GetActiveJobs", ())[1] == ({job_id: "capture"},)
