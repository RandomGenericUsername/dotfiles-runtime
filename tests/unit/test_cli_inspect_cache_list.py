"""Unit tests for the ``inspect cache list`` CLI command (Story 3.4).

Covers: exit codes, plain per-layer text (canonical order, truncated
hashes), --format json object shape (layers/counts/total with FULL
hashes), empty cache → exit 0 + clean message (NOT ErrorView), error
mapping (ValueError/OSError/unexpected → exit 1), and the auto-seed
guard (inspect never seeds). Mirrors test_cli_inspect_history.py —
monkeypatch the composition helper, not the whole app.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from cli_output.domain.enums import OutputFormat
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.application.inspect import InspectCacheResult
from runtime.cli.main import app

runner = CliRunner()


def _cache_result(
    *,
    wallpapers: tuple[str, ...] = ("a" * 64, "b" * 64),
    palettes: tuple[str, ...] = ("c" * 64,),
    effects: tuple[str, ...] = (),
    icons: tuple[str, ...] = (),
) -> InspectCacheResult:
    layers = {
        "wallpapers": wallpapers,
        "palettes": palettes,
        "effects": effects,
        "icons": icons,
    }
    counts = {layer: len(entries) for layer, entries in layers.items()}
    return InspectCacheResult(
        layers=layers, counts=counts, total=sum(counts.values())
    )


def _empty_result() -> InspectCacheResult:
    layers = {"wallpapers": (), "palettes": (), "effects": (), "icons": ()}
    return InspectCacheResult(
        layers=layers,
        counts={"wallpapers": 0, "palettes": 0, "effects": 0, "icons": 0},
        total=0,
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

    monkeypatch.setattr("runtime.cli.main._run_inspect_cache_list", _run)


class TestInspectCacheListCliSuccess:
    def test_success_exits_zero_and_renders_layers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _cache_result())
        result = runner.invoke(app, ["inspect", "cache", "list"])

        assert result.exit_code == 0
        assert "wallpapers (2):" in result.output
        assert "palettes (1):" in result.output
        assert "effects (0):" in result.output
        assert "icons (0):" in result.output
        assert ("a" * 12) in result.output
        assert ("b" * 12) in result.output

    def test_plain_lists_canonical_order_not_alphabetical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _cache_result())
        result = runner.invoke(app, ["inspect", "cache", "list"])

        assert result.exit_code == 0
        assert result.output.index("wallpapers") < result.output.index("palettes")
        assert result.output.index("palettes") < result.output.index("effects")
        assert result.output.index("effects") < result.output.index("icons")

    def test_plain_hashes_truncated_indented(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _cache_result())
        result = runner.invoke(app, ["inspect", "cache", "list"])

        assert result.exit_code == 0
        assert f"\n  {'a' * 12}\n" in result.output
        # Full 64-char hashes must NOT appear in plain output.
        assert "a" * 64 not in result.output

    def test_json_format_renders_structured_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _cache_result())
        result = runner.invoke(app, ["inspect", "cache", "list", "--format", "json"])

        assert result.exit_code == 0
        assert '"layers"' in result.output
        assert '"counts"' in result.output
        assert '"total"' in result.output

    def test_json_values_carry_full_hashes_counts_total(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert VALUES not key presence — JSON carries full hashes."""
        _fake_composition(monkeypatch, lambda: _cache_result())
        result = runner.invoke(app, ["inspect", "cache", "list", "--format", "json"])

        assert ("a" * 64) in result.output
        assert ("b" * 64) in result.output
        assert ("c" * 64) in result.output
        assert '"total": 3' in result.output
        assert '"wallpapers": 2' in result.output


class TestInspectCacheListCliEmpty:
    def test_empty_cache_exits_zero_with_clean_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _empty_result())
        result = runner.invoke(app, ["inspect", "cache", "list"])

        assert result.exit_code == 0
        assert "no cache entries recorded yet" in result.output

    def test_empty_json_payload_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_composition(monkeypatch, lambda: _empty_result())
        result = runner.invoke(
            app, ["inspect", "cache", "list", "--format", "json"]
        )

        assert result.exit_code == 0
        assert '"total": 0' in result.output
        assert '"wallpapers": []' in result.output
        assert '"counts"' in result.output


class TestInspectCacheListCliErrorMapping:
    def test_value_error_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise ValueError("cache dir is a symlink (refusing to follow): /x")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "cache", "list"])

        assert result.exit_code == 1
        assert "symlink" in result.output

    def test_os_error_maps_to_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Any:
            raise OSError("permission denied")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "cache", "list"])

        assert result.exit_code == 1
        assert "permission denied" in result.output

    def test_unexpected_exception_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise KeyError("surprise")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "cache", "list"])

        assert result.exit_code == 1
        assert "unexpectedly" in result.output


class TestInspectCacheListAutoSeedGuard:
    """inspect cache list is read-only — auto-seed must NEVER run for it."""

    def test_inspect_cache_list_never_auto_seeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        monkeypatch.setattr(
            sys, "argv", ["dotfiles-runtime", "inspect", "cache", "list"]
        )

        cli_main.main_callback(output_format=OutputFormat.PLAIN)

        assert calls == []

    def test_inspect_cache_list_end_to_end_seed_tripwire(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """AC 4 — through the full app invocation with the REAL composition
        helper: no current.json / history.jsonl / cache side-effects."""
        install = tmp_path / "install"
        (install / "generated").mkdir(parents=True)
        (install / "generated" / "default.png").write_bytes(b"png")
        state_home = tmp_path / "state-home"
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install))
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        monkeypatch.setattr(
            sys, "argv", ["dotfiles-runtime", "inspect", "cache", "list"]
        )

        result = runner.invoke(app, ["inspect", "cache", "list"])

        state_root = state_home / "dotfiles"
        assert result.exit_code == 0
        assert "no cache entries recorded yet" in result.output
        assert calls == []
        assert not (state_root / "current.json").exists()
        assert not (state_root / "history.jsonl").exists()
        assert not (state_root / "cache").exists()
