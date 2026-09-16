"""CLI wiring for the production capture host command (5-4 follow-up N1).

Pins that ``dotfiles-runtime capture`` exists, passes the recorder command
through to the host runner, exits with the runner's code, and never triggers
first-run seeding (the host has no business mutating wallpaper state).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.cli.main import app
from cli_output.domain.enums import OutputFormat

runner = CliRunner()


class TestCaptureCommand:
    def test_capture_command_wired_and_passes_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _fake_run(**kwargs: object) -> int:
            captured.update(kwargs)
            return 0

        monkeypatch.setattr(cli_main, "_run_capture_host", _fake_run)
        result = runner.invoke(
            app,
            ["capture", "--command", "gpu-screen-recorder -w DP-1 -o out.mp4"],
        )
        assert result.exit_code == 0
        assert captured["command"] == "gpu-screen-recorder -w DP-1 -o out.mp4"
        assert captured["backend"] is None

    def test_capture_passes_explicit_backend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            cli_main, "_run_capture_host", lambda **kw: captured.update(kw) or 0
        )
        result = runner.invoke(
            app,
            ["capture", "--command", "wf-recorder -f out.mp4", "--backend", "wf-recorder"],
        )
        assert result.exit_code == 0
        assert captured["backend"] == "wf-recorder"

    def test_capture_failure_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(**kwargs: object) -> int:
            raise RuntimeError("no recorder")

        monkeypatch.setattr(cli_main, "_run_capture_host", _boom)
        result = runner.invoke(app, ["capture", "--command", "wf-recorder -f out.mp4"])
        assert result.exit_code == 1

    def test_capture_never_auto_seeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        cli_main.main_callback(
            ctx=SimpleNamespace(invoked_subcommand="capture"),
            output_format=OutputFormat.PLAIN,
        )
        assert calls == []


class TestCaptureOptions:
    def test_duration_defaults_to_zero_and_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            cli_main, "_run_capture_host", lambda **kw: captured.update(kw) or 0
        )
        result = runner.invoke(app, ["capture", "--command", "rec -o out.mp4"])
        assert result.exit_code == 0
        assert captured["duration"] == 0.0

        captured.clear()
        result = runner.invoke(
            app, ["capture", "--command", "rec -o out.mp4", "--duration", "10"]
        )
        assert result.exit_code == 0
        assert captured["duration"] == 10.0

    def test_gif_options_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            cli_main, "_run_capture_host", lambda **kw: captured.update(kw) or 0
        )
        result = runner.invoke(
            app,
            [
                "capture",
                "--command",
                "rec -o in.capture.mp4",
                "--gif-input",
                "in.capture.mp4",
                "--gif-output",
                "out.gif",
                "--gif-size",
                "50",
            ],
        )
        assert result.exit_code == 0
        assert captured["gif_input"] == "in.capture.mp4"
        assert captured["gif_output"] == "out.gif"
        assert captured["gif_size"] == "50"

    def test_gif_options_default_to_no_conversion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            cli_main, "_run_capture_host", lambda **kw: captured.update(kw) or 0
        )
        result = runner.invoke(app, ["capture", "--command", "rec -o out.mp4"])
        assert result.exit_code == 0
        assert captured["gif_input"] is None
        assert captured["gif_output"] is None
        assert captured["gif_size"] == "original"


class TestRunCaptureHostValidation:
    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValueError, match="duration"):
            cli_main._run_capture_host(command="rec", duration=-1)

    def test_gif_output_without_input_rejected(self) -> None:
        with pytest.raises(ValueError, match="--gif-input"):
            cli_main._run_capture_host(command="rec", gif_output="out.gif")

    def test_unknown_gif_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown GIF size"):
            cli_main._run_capture_host(
                command="rec",
                gif_input="in.capture.mp4",
                gif_output="out.gif",
                gif_size="25",
            )


class _FakeNotifyRecorder:
    """Minimal IRecorderProcess: owns no child, records lifecycle calls."""

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


class _FakeNotifyClient:
    """Minimal controllable job client (degraded arm: no bus, no serve)."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict]] = []

    def begin(self, kind: str, ttl: float) -> str:
        return "job-1"

    def renew(self, job_id: str) -> None:
        pass

    def end(self, job_id: str, code: int) -> None:
        pass

    def publish(self, topic: str, payload: dict) -> None:
        self.published.append((topic, payload))

    def set_control_handler(self, handler) -> None:
        pass


class _SequenceClock:
    """Monotonic fake returning a preset sequence, then holding the last."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)

    def __call__(self) -> float:
        if len(self._values) > 1:
            return self._values.pop(0)
        return self._values[0]


class TestCaptureFinalizeNotification:
    """Recording finalize routes a 'Recording saved' toast through the
    capture-tool backend (add-ags-notifd-notifications, design.md §2)."""

    def test_output_path_option_passes_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            cli_main, "_run_capture_host", lambda **kw: captured.update(kw) or 0
        )
        result = runner.invoke(
            app,
            ["capture", "--command", "rec -o out.mp4", "--output-path", "out.mp4"],
        )
        assert result.exit_code == 0
        assert captured["output_path"] == "out.mp4"

    def test_output_path_defaults_to_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}
        monkeypatch.setattr(
            cli_main, "_run_capture_host", lambda **kw: captured.update(kw) or 0
        )
        result = runner.invoke(app, ["capture", "--command", "rec -o out.mp4"])
        assert result.exit_code == 0
        assert captured["output_path"] is None

    def test_finalize_notifies_with_path_and_duration(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import threading

        spawned: list[list[str]] = []
        monkeypatch.setattr(
            cli_main,
            "_spawn_capture_notification",
            lambda argv: spawned.append(list(argv)),
        )
        target = tmp_path / "rec.mp4"
        target.write_bytes(b"x" * 2048)
        stop = threading.Event()
        stop.set()  # degraded arm exits immediately after start
        code = cli_main._run_capture_host(
            command="rec -o out.mp4",
            recorder=_FakeNotifyRecorder(),
            client=_FakeNotifyClient(),
            clock=_SequenceClock([1000.0, 1001.0, 1042.0, 1042.0]),
            stop_event=stop,
            output_path=str(target),
        )
        assert code == 0
        assert len(spawned) == 1
        argv = spawned[0]
        assert argv[:3] == ["notify", "--kind", "recording-saved"]
        assert "--path" in argv and str(target) in argv
        duration = argv[argv.index("--duration") + 1]
        assert duration == "42.0"
        assert "--size" in argv and "2048" in argv

    def test_gif_finalize_reports_the_converted_gif(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        import threading

        spawned: list[list[str]] = []
        monkeypatch.setattr(
            cli_main,
            "_spawn_capture_notification",
            lambda argv: spawned.append(list(argv)),
        )
        final = tmp_path / "rec.gif"
        final.write_bytes(b"gif")
        stop = threading.Event()
        stop.set()
        code = cli_main._run_capture_host(
            command="rec -o in.capture.mp4",
            recorder=_FakeNotifyRecorder(),
            client=_FakeNotifyClient(),
            clock=_SequenceClock([1000.0, 1000.0]),
            stop_event=stop,
            gif_input=str(tmp_path / "in.capture.mp4"),
            gif_output=str(final),
            gif_convert=lambda *args: None,
        )
        assert code == 0
        assert spawned and spawned[0][:3] == [
            "notify",
            "--kind",
            "recording-saved",
        ]
        assert str(final) in spawned[0]

    def test_no_output_path_stays_silent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        spawned: list[list[str]] = []
        monkeypatch.setattr(
            cli_main,
            "_spawn_capture_notification",
            lambda argv: spawned.append(list(argv)),
        )
        stop = threading.Event()
        stop.set()
        code = cli_main._run_capture_host(
            command="rec -o out.mp4",
            recorder=_FakeNotifyRecorder(),
            client=_FakeNotifyClient(),
            clock=_SequenceClock([1000.0]),
            stop_event=stop,
        )
        assert code == 0
        assert spawned == []

    def test_host_failure_notifies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spawned: list[list[str]] = []
        monkeypatch.setattr(
            cli_main,
            "_spawn_capture_notification",
            lambda argv: spawned.append(list(argv)),
        )

        def _boom(**kwargs: object) -> int:
            raise RuntimeError("no recorder")

        monkeypatch.setattr(cli_main, "_run_capture_host", _boom)
        result = runner.invoke(app, ["capture", "--command", "rec -o out.mp4"])
        assert result.exit_code == 1
        assert spawned and spawned[0][:3] == ["notify", "--kind", "failed"]
        assert "no recorder" in " ".join(spawned[0])
