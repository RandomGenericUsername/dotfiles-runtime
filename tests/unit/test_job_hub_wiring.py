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

from runtime.adapters.dbus_event_bus import HubService, SignalSink
from runtime.adapters.emit_validation import EmitValidator
from runtime.adapters.in_process_hub import InProcessJobRegistry
from runtime.adapters.in_process_job_client import InProcessJobClient
from runtime.application.capture import CaptureController
from runtime.domain.hub import EventHub
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
) -> tuple[HubService, InProcessJobRegistry, list[tuple[str, str, tuple[object, ...]]]]:
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
    service = HubService(registry, EmitValidator(clock=clock), sink=sink)
    emitted: list[tuple[str, str, tuple[object, ...]]] = []
    service.bind_emitter(lambda name, signature, body: emitted.append((name, signature, body)))
    return service, registry, emitted


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
        service, registry, emitted = _service(clock)
        recorder = _FakeRecorder()
        controller = CaptureController(
            InProcessJobClient(registry, service), recorder, clock=clock
        )
        service.register_control("capture", controller.control)
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


class TestHubControlRegistration:
    def test_register_control_validates_arguments(self) -> None:
        import pytest

        clock = _Clock()
        service, _, _ = _service(clock)
        with pytest.raises(ValueError, match="kind"):
            service.register_control("", lambda job_id, action: None)
        with pytest.raises(ValueError, match="callable"):
            service.register_control("capture", "nope")  # type: ignore[arg-type]

    def test_raising_control_handler_is_contained(self) -> None:
        clock = _Clock()
        service, _registry, _ = _service(clock)
        user_calls: list[tuple[str, str]] = []

        def _boom(job_id: str, action: str) -> None:
            user_calls.append((job_id, action))
            raise RuntimeError("job handler failed")

        service.register_control("capture", _boom)
        _, (job,) = service.dispatch("BeginJob", ("capture", 60))
        service.dispatch("Control", (job, "pause"))  # must not raise
        assert user_calls == [(job, "pause")]
        # The hub is still healthy after a handler error.
        assert service.dispatch("GetActiveJobs", ())[1] == ({job: "capture"},)
