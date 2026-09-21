"""Unit tests for the resilient segmenting recorder (AD-37).

The recorder child is faked: a killable fake per segment, an injected concat
callable, and no real time. Coverage: transparent restart on unexpected
death, the restart budget, segment naming, output-flag remapping, pause
propagation across a restart, and finalize (single-segment rename vs concat).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.adapters.segmenting_recorder import (
    SegmentingRecorder,
    replace_output,
    segment_path,
)
from runtime.ports.jobs import IRecorderProcess


class _FakeChild(IRecorderProcess):
    def __init__(self, command: list[str], sink: list[_FakeChild]) -> None:
        self.command = command
        self.calls: list[str] = []
        self._running = False
        sink.append(self)

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
        self._running = False


class _Factory:
    def __init__(self) -> None:
        self.children: list[_FakeChild] = []

    def __call__(self, command) -> _FakeChild:
        return _FakeChild(list(command), self.children)


class _Concat:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[Path], Path]] = []
        self.fail = fail

    def __call__(self, segments, output) -> None:
        self.calls.append((list(segments), Path(output)))
        if self.fail:
            raise RuntimeError("concat boom")
        Path(output).write_bytes(b"joined")


class _Clock:
    """Controllable monotonic clock for deterministic retry timing."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _recorder(factory: _Factory, concat: _Concat, **kwargs) -> SegmentingRecorder:
    command = ["gpu-screen-recorder", "-w", "eDP-2", "-o", "/out/rec.mp4"]
    kwargs.setdefault("clock", _Clock())
    kwargs.setdefault("attempt_interval", 0.0)
    return SegmentingRecorder(
        "/out/rec.mp4",
        command,
        backend="gpu-screen-recorder",
        recorder_factory=factory,
        concat=concat,
        **kwargs,
    )


class TestOutputRemapping:
    def test_replace_output_gsr(self) -> None:
        assert replace_output(
            ["gpu-screen-recorder", "-o", "/a.mp4"], "gpu-screen-recorder", "/b.mp4"
        ) == ["gpu-screen-recorder", "-o", "/b.mp4"]

    def test_replace_output_wf_recorder(self) -> None:
        assert replace_output(
            ["wf-recorder", "-f", "/a.mkv"], "wf-recorder", "/b.mkv"
        ) == ["wf-recorder", "-f", "/b.mkv"]

    def test_replace_output_missing_flag_unchanged(self) -> None:
        assert replace_output(["rec"], "gpu-screen-recorder", "/b.mp4") == ["rec"]

    def test_segment_path_keeps_extension(self) -> None:
        assert segment_path("/out/rec.mp4", 1) == Path("/out/rec.part01.mp4")


class TestRestart:
    def test_start_creates_first_segment(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat)
        recorder.start()
        assert recorder.is_running() is True
        assert len(factory.children) == 1
        assert factory.children[0].command[-1].endswith("rec.part00.mp4")

    def test_alive_child_needs_no_restart(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat)
        recorder.start()
        assert recorder.ensure_alive() is True
        assert recorder.restarts == 0
        assert len(factory.children) == 1

    def test_dead_child_restarts_on_new_segment(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat)
        recorder.start()
        factory.children[0].kill()
        assert recorder.ensure_alive() is True
        assert recorder.restarts == 1
        assert len(factory.children) == 2
        assert factory.children[1].command[-1].endswith("rec.part01.mp4")
        assert factory.children[1].calls == ["start"]

    def test_restart_budget_exhausted(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat, max_restarts=2)
        recorder.start()
        for _ in range(2):
            factory.children[-1].kill()
            assert recorder.ensure_alive() is True
        factory.children[-1].kill()
        assert recorder.ensure_alive() is False

    def test_transient_spawn_failure_retries_then_succeeds(self) -> None:
        class _FlakyFactory:
            def __init__(self) -> None:
                self.attempts = 0
                self.children: list[_FakeChild] = []

            def __call__(self, command):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("cannot spawn yet")
                return _FakeChild(list(command), self.children)

        factory = _FlakyFactory()
        recorder = SegmentingRecorder(
            "/out/rec.mp4",
            ["gpu-screen-recorder", "-o", "/out/rec.mp4"],
            backend="gpu-screen-recorder",
            recorder_factory=factory,
            concat=_Concat(),
            clock=_Clock(),
            attempt_interval=0.0,
        )
        recorder._current = None  # type: ignore[assignment]
        # First failed spawn is retried, not fatal.
        assert recorder.ensure_alive() is True
        assert recorder.ensure_alive() is True
        assert factory.attempts == 2
        assert len(factory.children) == 1

    def test_persistent_failure_after_cap_returns_false(self) -> None:
        class _DeadFactory:
            def __call__(self, command):
                raise RuntimeError("cannot spawn")

        recorder = SegmentingRecorder(
            "/out/rec.mp4",
            ["gpu-screen-recorder", "-o", "/out/rec.mp4"],
            backend="gpu-screen-recorder",
            recorder_factory=_DeadFactory(),
            concat=_Concat(),
            clock=_Clock(),
            attempt_interval=0.0,
            max_restarts=1,
        )
        recorder._current = None  # type: ignore[assignment]
        assert recorder.ensure_alive() is True  # attempt 1 fails, retried
        assert recorder.ensure_alive() is False  # cap reached

    def test_grace_window_elapsed_returns_false(self) -> None:
        class _DeadFactory:
            def __call__(self, command):
                raise RuntimeError("cannot spawn")

        clock = _Clock()
        recorder = SegmentingRecorder(
            "/out/rec.mp4",
            ["gpu-screen-recorder", "-o", "/out/rec.mp4"],
            backend="gpu-screen-recorder",
            recorder_factory=_DeadFactory(),
            concat=_Concat(),
            clock=clock,
            attempt_interval=0.0,
            grace=5.0,
        )
        recorder._current = None  # type: ignore[assignment]
        assert recorder.ensure_alive() is True
        clock.advance(6.0)
        assert recorder.ensure_alive() is False

    def test_attempt_interval_spaces_retries(self) -> None:
        factory, concat = _Factory(), _Concat()
        clock = _Clock()
        recorder = _recorder(
            factory, concat, clock=clock, attempt_interval=0.5
        )
        recorder.start()
        factory.children[0].kill()
        assert recorder.ensure_alive() is True
        assert len(factory.children) == 2
        factory.children[1].kill()
        # Too soon: no new spawn, but not fatal.
        assert recorder.ensure_alive() is True
        assert len(factory.children) == 2
        clock.advance(0.6)
        assert recorder.ensure_alive() is True
        assert len(factory.children) == 3

    def test_paused_restart_repauses_child(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat)
        recorder.start()
        recorder.pause()
        factory.children[0].kill()
        recorder.ensure_alive()
        assert factory.children[1].calls == ["start", "pause"]

    def test_exit_status_delegates(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat)
        recorder.start()
        assert recorder.exit_status() is None


class TestFinalize:
    def test_single_segment_is_renamed(self, tmp_path: Path) -> None:
        out = tmp_path / "rec.mp4"
        part = segment_path(str(out), 0)
        part.write_bytes(b"one")
        factory, concat = _Factory(), _Concat()
        recorder = SegmentingRecorder(
            str(out),
            ["gpu-screen-recorder", "-o", str(out)],
            backend="gpu-screen-recorder",
            recorder_factory=factory,
            concat=concat,
        )
        recorder.start()
        recorder._segments = [part]
        recorder.stop()
        assert out.read_bytes() == b"one"
        assert concat.calls == []
        assert recorder.concat_ok is True

    def test_multiple_segments_are_concatenated(self, tmp_path: Path) -> None:
        out = tmp_path / "rec.mp4"
        parts = [segment_path(str(out), i) for i in range(2)]
        for part in parts:
            part.write_bytes(b"x")
        factory, concat = _Factory(), _Concat()
        recorder = SegmentingRecorder(
            str(out),
            ["gpu-screen-recorder", "-o", str(out)],
            backend="gpu-screen-recorder",
            recorder_factory=factory,
            concat=concat,
        )
        recorder.start()
        recorder._segments = parts
        recorder.stop()
        assert len(concat.calls) == 1
        assert concat.calls[0][0] == parts
        assert recorder.concat_ok is True
        assert all(not part.exists() for part in parts)

    def test_concat_failure_keeps_segments(self, tmp_path: Path) -> None:
        out = tmp_path / "rec.mp4"
        parts = [segment_path(str(out), i) for i in range(2)]
        for part in parts:
            part.write_bytes(b"x")
        factory, concat = _Factory(), _Concat(fail=True)
        recorder = SegmentingRecorder(
            str(out),
            ["gpu-screen-recorder", "-o", str(out)],
            backend="gpu-screen-recorder",
            recorder_factory=factory,
            concat=concat,
        )
        recorder.start()
        recorder._segments = parts
        recorder.stop()
        assert recorder.concat_ok is False
        assert all(part.exists() for part in parts)

    def test_stop_is_idempotent(self, tmp_path: Path) -> None:
        out = tmp_path / "rec.mp4"
        part = segment_path(str(out), 0)
        part.write_bytes(b"one")
        factory, concat = _Factory(), _Concat()
        recorder = SegmentingRecorder(
            str(out),
            ["gpu-screen-recorder", "-o", str(out)],
            backend="gpu-screen-recorder",
            recorder_factory=factory,
            concat=concat,
        )
        recorder.start()
        recorder._segments = [part]
        recorder.stop()
        recorder.stop()
        assert recorder.concat_ok is True

    def test_stop_without_segments_is_not_ok(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat)
        recorder._stopped = False
        recorder._segments = []
        recorder._current = None
        recorder.stop()
        assert recorder.concat_ok is False

    def test_ensure_alive_after_stop_is_true(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat)
        recorder.start()
        recorder.stop()
        assert recorder.ensure_alive() is True


class TestConstruction:
    def test_empty_command_rejected(self) -> None:
        with pytest.raises(ValueError, match="command must be non-empty"):
            SegmentingRecorder("/out/rec.mp4", [], concat=_Concat())

    def test_double_start_rejected(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = _recorder(factory, concat)
        recorder.start()
        with pytest.raises(RuntimeError, match="already started"):
            recorder.start()

    def test_backend_inferred_from_command(self) -> None:
        factory, concat = _Factory(), _Concat()
        recorder = SegmentingRecorder(
            "/out/rec.mkv",
            ["wf-recorder", "-f", "/out/rec.mkv"],
            recorder_factory=factory,
            concat=concat,
        )
        recorder.start()
        assert factory.children[0].command[2].endswith("rec.part00.mkv")
