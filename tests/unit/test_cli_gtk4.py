"""Unit tests for the ``gtk4 restart`` escape-hatch command (section 5).

Covers command registration, the no-auto-seed skip, success reporting, and
the non-zero exit on any restart failure. The composition function is faked
at the module boundary; the real primitive is exercised with an injected
discovery that returns fakes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _quiet_seed_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


class TestGtk4RestartRegistration:
    def test_command_is_registered(self) -> None:
        result = runner.invoke(app, ["gtk4", "--help"])

        assert result.exit_code == 0
        assert "restart" in result.output

    def test_restart_never_triggers_auto_seed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> None:
            raise AssertionError("gtk4 commands must not auto-seed")

        monkeypatch.setattr(cli_main, "_run_seed_if_needed", _boom)
        monkeypatch.setattr(cli_main, "_run_gtk4_restart", lambda: [])
        result = runner.invoke(app, ["gtk4", "restart"])

        assert result.exit_code == 0


class TestGtk4RestartOutput:
    def test_success_reports_restarted_apps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_main, "_run_gtk4_restart", lambda: ["power-options-gtk", "hyprmod"])

        result = runner.invoke(app, ["gtk4", "restart"])

        assert result.exit_code == 0
        assert "power-options-gtk" in result.output
        assert "hyprmod" in result.output

    def test_no_apps_is_vacuous_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_main, "_run_gtk4_restart", lambda: [])

        result = runner.invoke(app, ["gtk4", "restart"])

        assert result.exit_code == 0
        assert "nothing to restart" in result.output

    def test_failure_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> list[str]:
            raise RuntimeError("restart failed for: hyprmod")

        monkeypatch.setattr(cli_main, "_run_gtk4_restart", _boom)

        result = runner.invoke(app, ["gtk4", "restart"])

        assert result.exit_code == 1
        assert "hyprmod" in result.output


class TestGtk4RestartPrimitive:
    def test_reports_discovered_apps_and_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from runtime.adapters import gtk4_app_reloader as gr
        from runtime.adapters.gtk4_app_reloader import Gtk4AppProcess

        process = Gtk4AppProcess(pid=1234, argv=("hyprmod",), app_name="hyprmod")
        monkeypatch.setattr(gr, "_discover_gtk4_apps", lambda: [process])

        class _PassingReloader:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def reload(self) -> bool:
                return True

        monkeypatch.setattr(gr, "Gtk4AppReloader", _PassingReloader)

        result = runner.invoke(app, ["gtk4", "restart"])

        assert result.exit_code == 0
        assert "hyprmod" in result.output

    def test_restart_failure_exits_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from runtime.adapters import gtk4_app_reloader as gr
        from runtime.adapters.gtk4_app_reloader import Gtk4AppProcess

        process = Gtk4AppProcess(pid=1234, argv=("hyprmod",), app_name="hyprmod")
        monkeypatch.setattr(gr, "_discover_gtk4_apps", lambda: [process])

        class _FailingReloader:
            def __init__(self, **_kwargs: object) -> None:
                pass

            def reload(self) -> bool:
                return False

        monkeypatch.setattr(gr, "Gtk4AppReloader", _FailingReloader)

        result = runner.invoke(app, ["gtk4", "restart"])

        assert result.exit_code == 1
        assert "hyprmod" in result.output

    def test_no_apps_running_is_vacuous_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from runtime.adapters import gtk4_app_reloader as gr

        monkeypatch.setattr(gr, "_discover_gtk4_apps", lambda: [])

        result = runner.invoke(app, ["gtk4", "restart"])

        assert result.exit_code == 0
        assert "nothing to restart" in result.output
