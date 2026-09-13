"""Unit tests for the backend-aware subprocess recorder (Phase 5, 5-4, AD-37).

No recorder is spawned: the spawn callable, signaler, and sleep are fakes.
Coverage: spawn/startup contract, backend signal resolution (gpu-screen-recorder
vs wf-recorder), graceful stop + timeout escalation, and error containment.
"""

from __future__ import annotations

import signal
import subprocess

import pytest

from runtime.adapters.subprocess_recorder import (
    DEFAULT_BACKEND,
    DEFAULT_STARTUP_GRACE,
    RECORDER_BACKENDS,
    SubprocessRecorder,
)


class _FakeProc:
    def __init__(
        self,
        pid: int = 4321,
        *,
        alive: bool = True,
        wait_exc: Exception | None = None,
    ) -> None:
        self.pid = pid
        self.alive = alive
        self.waits: list[float | None] = []
        self.terminated = 0
        self._wait_exc = wait_exc

    def poll(self) -> int | None:
        return None if self.alive else 0

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self._wait_exc is not None:
            self.alive = False
            raise self._wait_exc
        self.alive = False
        return 0

    def terminate(self) -> None:
        self.terminated += 1
        self.alive = False


class _Signaler:
    def __init__(self, error: OSError | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self._error = error

    def __call__(self, pid: int, signum: int) -> None:
        self.calls.append((pid, signum))
        if self._error is not None:
            raise self._error


def _harness(
    command: list[str],
    *,
    backend: str | None = None,
    procs: list[_FakeProc] | None = None,
    signaler: _Signaler | None = None,
    startup_grace: float = DEFAULT_STARTUP_GRACE,
) -> tuple[SubprocessRecorder, list[tuple[list[str], dict[str, object]]], _Signaler, list[float]]:
    pending = list(procs) if procs is not None else [_FakeProc()]
    spawn_calls: list[tuple[list[str], dict[str, object]]] = []
    sleeps: list[float] = []
    signaler = signaler if signaler is not None else _Signaler()

    def spawn(cmd: list[str], **kwargs: object) -> _FakeProc:
        spawn_calls.append((list(cmd), dict(kwargs)))
        return pending.pop(0)

    recorder = SubprocessRecorder(
        command,
        backend=backend,
        spawn=spawn,
        signaler=signaler,
        sleep=sleeps.append,
        startup_grace=startup_grace,
    )
    return recorder, spawn_calls, signaler, sleeps


class TestStartup:
    def test_start_spawns_command_in_new_session_after_grace(self) -> None:
        proc = _FakeProc(pid=777)
        recorder, spawn_calls, _, sleeps = _harness(
            ["gpu-screen-recorder", "-f", "out.mp4"], procs=[proc], startup_grace=0.01
        )
        recorder.start()
        assert spawn_calls == [
            (["gpu-screen-recorder", "-f", "out.mp4"], {"start_new_session": True})
        ]
        assert sleeps == [0.01]
        assert recorder.is_running() is True

    def test_empty_command_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            SubprocessRecorder([])

    def test_start_while_running_raises(self) -> None:
        recorder, spawn_calls, _, _ = _harness(["gpu-screen-recorder"])
        recorder.start()
        with pytest.raises(RuntimeError, match="already running"):
            recorder.start()
        assert len(spawn_calls) == 1

    def test_start_that_exits_during_grace_raises(self) -> None:
        recorder, _, _, _ = _harness(["gpu-screen-recorder"], procs=[_FakeProc(alive=False)])
        with pytest.raises(RuntimeError, match="exited during startup"):
            recorder.start()
        assert recorder.is_running() is False

    def test_restart_after_child_exit_spawns_again(self) -> None:
        first, second = _FakeProc(pid=1), _FakeProc(pid=2)
        recorder, spawn_calls, _, _ = _harness(["gpu-screen-recorder"], procs=[first, second])
        recorder.start()
        first.alive = False
        recorder.start()
        assert [cmd for cmd, _ in spawn_calls] == [
            ["gpu-screen-recorder"],
            ["gpu-screen-recorder"],
        ]
        assert recorder.is_running() is True


class TestBackendSignals:
    def test_gpu_screen_recorder_signals(self) -> None:
        recorder, _, signaler, _ = _harness(["gpu-screen-recorder"], procs=[_FakeProc(pid=10)])
        recorder.start()
        recorder.pause()
        recorder.resume()
        recorder.stop()
        assert signaler.calls == [
            (10, signal.SIGUSR2),
            (10, signal.SIGUSR2),
            (10, signal.SIGINT),
        ]

    def test_wf_recorder_signals(self) -> None:
        recorder, _, signaler, _ = _harness(["wf-recorder"], procs=[_FakeProc(pid=11)])
        recorder.start()
        recorder.pause()
        recorder.resume()
        recorder.stop()
        assert signaler.calls == [
            (11, signal.SIGSTOP),
            (11, signal.SIGCONT),
            (11, signal.SIGINT),
        ]

    def test_backend_inferred_from_executable_path(self) -> None:
        recorder, _, _, _ = _harness(["/usr/bin/wf-recorder", "-f", "x"])
        assert recorder.signals == RECORDER_BACKENDS["wf-recorder"]

    def test_unknown_executable_defaults_to_gpu(self) -> None:
        recorder, _, _, _ = _harness(["/opt/tools/my-capture-wrapper"])
        assert recorder.signals == RECORDER_BACKENDS[DEFAULT_BACKEND]

    def test_explicit_backend_overrides_inference(self) -> None:
        recorder, _, signaler, _ = _harness(
            ["wf-recorder"], backend="gpu-screen-recorder", procs=[_FakeProc(pid=12)]
        )
        recorder.start()
        recorder.pause()
        assert signaler.calls == [(12, signal.SIGUSR2)]

    def test_unknown_backend_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown recorder backend"):
            SubprocessRecorder(["gpu-screen-recorder"], backend="bogus")

    def test_pause_or_resume_without_start_raises(self) -> None:
        recorder, _, _, _ = _harness(["gpu-screen-recorder"])
        with pytest.raises(RuntimeError, match="not running"):
            recorder.pause()
        with pytest.raises(RuntimeError, match="not running"):
            recorder.resume()


class TestStop:
    def test_stop_waits_for_graceful_exit(self) -> None:
        proc = _FakeProc(pid=20)
        recorder, _, signaler, _ = _harness(["gpu-screen-recorder"], procs=[proc])
        recorder.start()
        recorder.stop()
        assert signaler.calls == [(20, signal.SIGINT)]
        assert proc.waits == [5.0]
        assert proc.terminated == 0
        assert recorder.is_running() is False

    def test_stop_without_start_is_a_noop(self) -> None:
        recorder, _, signaler, _ = _harness(["gpu-screen-recorder"])
        recorder.stop()
        assert signaler.calls == []

    def test_stop_when_child_already_exited_sends_nothing(self) -> None:
        proc = _FakeProc(pid=21)
        recorder, _, signaler, _ = _harness(["gpu-screen-recorder"], procs=[proc])
        recorder.start()
        proc.alive = False
        recorder.stop()
        assert signaler.calls == []

    def test_stop_is_idempotent(self) -> None:
        recorder, _, signaler, _ = _harness(["gpu-screen-recorder"], procs=[_FakeProc(pid=22)])
        recorder.start()
        recorder.stop()
        recorder.stop()
        assert signaler.calls == [(22, signal.SIGINT)]

    def test_graceful_stop_timeout_escalates_to_terminate(self) -> None:
        proc = _FakeProc(pid=23, wait_exc=subprocess.TimeoutExpired("rec", 5.0))
        recorder, _, _, _ = _harness(["gpu-screen-recorder"], procs=[proc])
        recorder.start()
        recorder.stop()
        assert proc.terminated == 1

    def test_stop_signal_failure_is_contained_and_still_waits(self) -> None:
        proc = _FakeProc(pid=24)
        signaler = _Signaler(error=ProcessLookupError("gone"))
        recorder, _, _, _ = _harness(["gpu-screen-recorder"], procs=[proc], signaler=signaler)
        recorder.start()
        recorder.stop()  # must not raise
        assert proc.terminated == 0
        assert proc.waits == [5.0]

    def test_pause_signal_failure_is_contained(self) -> None:
        signaler = _Signaler(error=ProcessLookupError("gone"))
        recorder, _, _, _ = _harness(
            ["gpu-screen-recorder"], procs=[_FakeProc(pid=25)], signaler=signaler
        )
        recorder.start()
        recorder.pause()  # must not raise


class TestIsRunning:
    def test_false_before_start_and_after_stop(self) -> None:
        recorder, _, _, _ = _harness(["gpu-screen-recorder"], procs=[_FakeProc(pid=30)])
        assert recorder.is_running() is False
        recorder.start()
        assert recorder.is_running() is True
        recorder.stop()
        assert recorder.is_running() is False
