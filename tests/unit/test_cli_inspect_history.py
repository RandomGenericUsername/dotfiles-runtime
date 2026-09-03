"""Unit tests for the ``inspect history`` CLI command (Story 3.3).

Covers: exit codes, plain per-entry text, --format json object shape
(entries/count/total/truncated/limit), --limit/-n wiring (incl. 0 = all,
negative → exit 1), empty history → exit 0 + clean message (NOT ErrorView),
corrupt middle → exit 1 + ErrorView, and the auto-seed guard (inspect never
seeds). Mirrors test_cli_inspect_status.py — monkeypatch the composition
helper, not the whole app.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from cli_output.domain.enums import OutputFormat
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.application.inspect import HistoryRecord
from runtime.cli.main import app

runner = CliRunner()


def _record(
    *,
    ts: str = "2026-01-02T00:00:00Z",
    trigger: str = "set",
    wallpaper: str = "a" * 64,
    source_path: str = "/img/wall.png",
) -> HistoryRecord:
    return HistoryRecord(
        ts=ts,
        trigger=trigger,
        wallpaper=wallpaper,
        palette="b" * 64,
        effects=None,
        icons=None,
        source_path=source_path,
    )


@pytest.fixture(autouse=True)
def _quiet_seed_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Point first-run seeding at an empty spine so it skips quietly."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _fake_composition(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> None:
    def _run(limit: int = 20) -> Any:
        return behavior(limit)

    monkeypatch.setattr("runtime.cli.main._run_inspect_history", _run)


class TestInspectHistoryCliSuccess:
    def test_success_exits_zero_and_renders_entries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda limit: ([_record()], 1))
        result = runner.invoke(app, ["inspect", "history"])

        assert result.exit_code == 0
        assert "2026-01-02T00:00:00Z" in result.output
        assert "set" in result.output
        assert ("a" * 12) in result.output

    def test_newest_first_order_preserved_in_plain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        older = _record(ts="2026-01-01T00:00:00Z", trigger="seed")
        newer = _record(ts="2026-01-02T00:00:00Z", trigger="set")
        _fake_composition(monkeypatch, lambda limit: ([newer, older], 2))
        result = runner.invoke(app, ["inspect", "history"])

        assert result.exit_code == 0
        assert result.output.index("2026-01-02") < result.output.index("2026-01-01")

    def test_json_format_renders_structured_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda limit: ([_record()], 1))
        result = runner.invoke(app, ["inspect", "history", "--format", "json"])

        assert result.exit_code == 0
        assert '"entries"' in result.output
        assert '"count"' in result.output
        assert '"total"' in result.output
        assert '"truncated"' in result.output
        assert '"limit"' in result.output

    def test_json_values_carry_full_hashes_not_truncations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Assert VALUES not key presence — JSON entries carry full hashes."""
        _fake_composition(monkeypatch, lambda limit: ([_record()], 1))
        result = runner.invoke(app, ["inspect", "history", "--format", "json"])

        assert ("a" * 64) in result.output
        assert ("b" * 64) in result.output
        assert "/img/wall.png" in result.output

    def test_json_truncated_flag_when_limited(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda limit: ([_record()], 25))
        result = runner.invoke(app, ["inspect", "history", "--format", "json"])

        assert result.exit_code == 0
        assert '"truncated": true' in result.output
        assert '"total": 25' in result.output


class TestInspectHistoryCliLimit:
    def test_limit_option_wired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def _behavior(limit: int) -> Any:
            seen.append(limit)
            return ([], 0)

        _fake_composition(monkeypatch, _behavior)
        result = runner.invoke(app, ["inspect", "history", "--limit", "5"])

        assert result.exit_code == 0
        assert seen == [5]

    def test_limit_short_flag_wired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def _behavior(limit: int) -> Any:
            seen.append(limit)
            return ([], 0)

        _fake_composition(monkeypatch, _behavior)
        result = runner.invoke(app, ["inspect", "history", "-n", "3"])

        assert result.exit_code == 0
        assert seen == [3]

    def test_limit_zero_means_all(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def _behavior(limit: int) -> Any:
            seen.append(limit)
            return ([_record()], 1)

        _fake_composition(monkeypatch, _behavior)
        result = runner.invoke(app, ["inspect", "history", "--limit", "0"])

        assert result.exit_code == 0
        assert seen == [0]

    def test_negative_limit_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(limit: int) -> Any:
            raise ValueError("history limit must be >= 0, got -1")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "history", "--limit", "-1"])

        assert result.exit_code == 1
        assert "limit" in result.output


class TestInspectHistoryCliEmpty:
    def test_empty_history_exits_zero_with_clean_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda limit: ([], 0))
        result = runner.invoke(app, ["inspect", "history"])

        assert result.exit_code == 0
        assert "no history recorded yet" in result.output

    def test_empty_json_payload_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_composition(monkeypatch, lambda limit: ([], 0))
        result = runner.invoke(
            app, ["inspect", "history", "--format", "json", "--limit", "5"]
        )

        assert result.exit_code == 0
        assert '"entries": []' in result.output
        assert '"count": 0' in result.output
        assert '"total": 0' in result.output
        assert '"truncated": false' in result.output
        assert '"limit": 5' in result.output


class TestInspectHistoryCliErrorMapping:
    def test_corrupt_middle_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(limit: int) -> Any:
            raise ValueError("history.jsonl line 2: not valid JSON (oops)")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "history"])

        assert result.exit_code == 1
        assert "line 2" in result.output

    def test_os_error_maps_to_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(limit: int) -> Any:
            raise OSError("permission denied")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "history"])

        assert result.exit_code == 1
        assert "permission denied" in result.output

    def test_unexpected_exception_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(limit: int) -> Any:
            raise KeyError("surprise")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["inspect", "history"])

        assert result.exit_code == 1
        assert "unexpectedly" in result.output


class TestInspectHistoryAutoSeedGuard:
    """inspect history is read-only — auto-seed must NEVER run for it."""

    def test_inspect_history_never_auto_seeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        monkeypatch.setattr(sys, "argv", ["dotfiles-runtime", "inspect", "history"])

        cli_main.main_callback(output_format=OutputFormat.PLAIN)

        assert calls == []

    def test_inspect_history_end_to_end_seed_tripwire(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """AC 4 — through the full app invocation with the REAL composition
        helper: no current.json / history.jsonl side-effects, no seeding."""
        install = tmp_path / "install"
        (install / "generated").mkdir(parents=True)
        (install / "generated" / "default.png").write_bytes(b"png")
        state_home = tmp_path / "state-home"
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install))
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))
        monkeypatch.setattr(sys, "argv", ["dotfiles-runtime", "inspect", "history"])

        result = runner.invoke(app, ["inspect", "history"])

        state_root = state_home / "dotfiles"
        assert result.exit_code == 0
        assert "no history recorded yet" in result.output
        assert calls == []
        assert not (state_root / "current.json").exists()
        assert not (state_root / "history.jsonl").exists()
