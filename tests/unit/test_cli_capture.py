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
