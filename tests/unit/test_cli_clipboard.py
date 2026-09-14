"""CLI wiring for ``dotfiles-runtime clipboard`` (D1).

Pins that the command exists, passes injectables through to the host runner,
exits with the runner's code, and never triggers first-run seeding (the
watcher has no business mutating wallpaper state).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from cli_output.domain.enums import OutputFormat
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.cli.main import app

runner = CliRunner()


class TestClipboardCommand:
    def test_command_wired_and_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(cli_main, "_run_clipboard_host", lambda **kw: calls.append(1) or 0)
        result = runner.invoke(app, ["clipboard"])
        assert result.exit_code == 0
        assert calls == [1]

    def test_failure_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(**kwargs: object) -> int:
            raise RuntimeError("no wl-paste")

        monkeypatch.setattr(cli_main, "_run_clipboard_host", _boom)
        result = runner.invoke(app, ["clipboard"])
        assert result.exit_code == 1

    def test_never_auto_seeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        cli_main.main_callback(
            ctx=SimpleNamespace(invoked_subcommand="clipboard"),
            output_format=OutputFormat.PLAIN,
        )
        assert calls == []

    def test_build_client_degrades_without_hub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from runtime.adapters import daemon_status
        from runtime.adapters.local_job_client import LocalJobClient

        class Snapshot:
            name_owned = False
            detail = "no owner"

        monkeypatch.setattr(daemon_status, "probe_session_bus", lambda: Snapshot())
        client = cli_main._build_clipboard_client()
        assert isinstance(client, LocalJobClient)

