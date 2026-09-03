"""Unit tests for the ``inspect status`` CLI command (Story 3.2).

Covers: exit codes, plain summary text, --format json object shape,
absent-state → exit 1 + ErrorView, and the auto-seed guard (inspect never
seeds). Mirrors test_cli_reconcile.py patterns — monkeypatch the
composition helper, not the whole app.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from cli_output.domain.enums import OutputFormat
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.application.inspect import InspectStatusResult, LinkStatus
from runtime.cli.main import app

runner = CliRunner()


def _inspect_result() -> InspectStatusResult:
    return InspectStatusResult(
        wallpaper="a" * 64,
        wallpaper_source_path="/img/wall.png",
        monitors={
            "DP-1": {
                "backend": "hyprpaper",
                "source_hash": "a" * 64,
                "fit_mode": "cover",
                "mpv_options": None,
                "ipc_socket": None,
            }
        },
        palette="b" * 64,
        effects=None,
        icons=None,
        applied_at="2026-09-02T00:00:00Z",
        current_symlinks={
            "wallpaper-DP-1.png": LinkStatus(
                status="ok", target="/state/current/wallpaper-DP-1.png"
            ),
            "colors.yaml": LinkStatus(status="diverged", target="/elsewhere/colors.yaml"),
        },
    )


@pytest.fixture(autouse=True)
def _quiet_seed_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Point first-run seeding at an empty spine so it skips quietly."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _fake_composition(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> None:
    def _run() -> Any:
        return behavior()

    monkeypatch.setattr("runtime.cli.main._run_inspect_status", _run)


class TestInspectStatusCliSuccess:
    def test_success_exits_zero_and_renders_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _inspect_result())
        result = runner.invoke(app, ["inspect", "status"])

        assert result.exit_code == 0
        assert "desktop state" in result.output
        assert ("a" * 12) in result.output

    def test_summary_reports_degraded_layers_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = _inspect_result()
        res = type(res)(
            wallpaper=res.wallpaper,
            wallpaper_source_path=res.wallpaper_source_path,
            monitors=res.monitors,
            palette=None,
            effects=None,
            icons=None,
            applied_at=res.applied_at,
            current_symlinks=res.current_symlinks,
        )
        _fake_composition(monkeypatch, lambda: res)
        result = runner.invoke(app, ["inspect", "status"])

        assert result.exit_code == 0
        assert "palette absent" in result.output
        assert "effects absent" in result.output
        assert "icons absent" in result.output

    def test_json_format_renders_structured_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _inspect_result())
        result = runner.invoke(app, ["inspect", "status", "--format", "json"])

        assert result.exit_code == 0
        assert '"wallpaper"' in result.output
        assert '"wallpaper_source_path"' in result.output
        assert '"monitors"' in result.output
        assert '"palette"' in result.output
        assert '"effects"' in result.output
        assert '"icons"' in result.output
        assert '"applied_at"' in result.output
        assert '"current_symlinks"' in result.output

    def test_json_current_symlinks_serialized_with_status_and_target(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _inspect_result())
        result = runner.invoke(app, ["inspect", "status", "--format", "json"])

        assert result.exit_code == 0
        assert '"diverged"' in result.output
        assert '"/elsewhere/colors.yaml"' in result.output
        assert '"ok"' in result.output

    def test_json_values_carry_full_hashes_not_truncations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert VALUES not key presence — JSON payload carries full hashes."""
        _fake_composition(monkeypatch, lambda: _inspect_result())
        result = runner.invoke(app, ["inspect", "status", "--format", "json"])

        assert ("a" * 64) in result.output
        assert ("b" * 64) in result.output


class TestInspectStatusCliErrorMapping:
    def test_absent_state_maps_to_exit_1_with_seed_hint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise RuntimeError(
                "no state recorded — run `dotfiles-provision apply` / "
                "`dotfiles-runtime wallpaper set <img>` to seed"
            )

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "status"])

        assert result.exit_code == 1
        assert "no state recorded" in result.output
        assert "wallpaper set" in result.output

    def test_value_error_maps_to_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Any:
            raise ValueError("current.json is not valid JSON")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "status"])

        assert result.exit_code == 1
        assert "not valid JSON" in result.output

    def test_os_error_maps_to_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Any:
            raise OSError("permission denied")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "status"])

        assert result.exit_code == 1
        assert "permission denied" in result.output

    def test_unexpected_exception_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise KeyError("surprise")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "status"])

        assert result.exit_code == 1
        assert "unexpectedly" in result.output


class TestAutoSeedGuard:
    """inspect is read-only — auto-seed must NEVER run for inspect commands."""

    def test_inspect_never_auto_seeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        monkeypatch.setattr(sys, "argv", ["dotfiles-runtime", "inspect", "status"])

        cli_main.main_callback(output_format=OutputFormat.PLAIN)

        assert calls == []

    def test_reconcile_still_skips_auto_seed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        monkeypatch.setattr(sys, "argv", ["dotfiles-runtime", "reconcile"])

        cli_main.main_callback(output_format=OutputFormat.PLAIN)

        assert calls == []

    def test_other_commands_still_auto_seed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        monkeypatch.setattr(sys, "argv", ["dotfiles-runtime", "version"])

        cli_main.main_callback(output_format=OutputFormat.PLAIN)

        assert calls == ["seed"]

    def test_absent_state_error_reachable_end_to_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 3 — through the full app invocation, the absent-state error is
        reachable (guard skips seeding; no fabricated state masks it)."""
        calls: list[str] = []
        monkeypatch.setattr(
            cli_main, "_run_seed_if_needed", lambda: calls.append("seed")  # pragma: no cover
        )
        monkeypatch.setattr(sys, "argv", ["dotfiles-runtime", "inspect", "status"])

        def _boom() -> Any:
            raise RuntimeError("no state recorded — run `dotfiles-runtime wallpaper set <img>`")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "status"])

        assert calls == []
        assert result.exit_code == 1
        assert "no state recorded" in result.output
