"""Integration tests for ``inspect history`` — real writer + fresh reader
(Story 3.3).

rt-3.1 lesson: integration realism — lines are written through the real
``CacheSeeder.append_history`` (O_APPEND + fsync), then read back by a
FRESH ``InspectHistoryUseCase`` on the same state_root, so the
restart-survival path is exercised, not a fake. Covers writer→reader
round-trip across all 4 triggers incl. None hashes (AC 1), large-history
limit + truncated flags via the real CLI composition helper (AC 2), and
current/ divergence isolation (AC 4).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runtime.adapters.seeder import CacheSeeder
from runtime.application.inspect import InspectHistoryUseCase


def _seed_lines(
    state_root: Path,
    count: int,
    triggers: tuple[str, str, str, str] = ("seed", "set", "reconcile", "force"),
) -> None:
    seeder = CacheSeeder(state_root)
    for i in range(count):
        seeder.append_history(
            trigger=triggers[i % len(triggers)],
            wallpaper_hash=f"{i:064d}",
            palette_hash=None if i % 2 else f"p{i:063d}",
            effects_hash=None,
            icons_hash=f"i{i:063d}",
            source_path="" if i % 2 else f"/img/{i}.png",
        )


class TestWriterReaderRoundTrip:
    """AC 1 — real append_history writes are projected newest-first."""

    def test_all_four_triggers_round_trip_newest_first(
        self, tmp_path: Path
    ) -> None:
        _seed_lines(tmp_path, 4)
        records = InspectHistoryUseCase(tmp_path).run(limit=0)
        assert [r.trigger for r in records] == [
            "force",
            "reconcile",
            "set",
            "seed",
        ]
        assert records[0].wallpaper == f"{3:064d}"
        assert records[-1].wallpaper == f"{0:064d}"

    def test_none_hashes_and_source_path_survive(
        self, tmp_path: Path
    ) -> None:
        seeder = CacheSeeder(tmp_path)
        seeder.append_history(
            trigger="seed",
            wallpaper_hash="a" * 64,
            palette_hash=None,
            effects_hash=None,
            icons_hash=None,
            source_path="",
        )
        (record,) = InspectHistoryUseCase(tmp_path).run()
        assert record.to_dict() == {
            "ts": record.ts,
            "trigger": "seed",
            "wallpaper": "a" * 64,
            "palette": None,
            "effects": None,
            "icons": None,
            "source_path": "",
        }
        assert "schema_version" not in record.to_dict()

    def test_fresh_reader_after_restart(self, tmp_path: Path) -> None:
        """Restart-survival realism: writer and reader share only the file."""
        _seed_lines(tmp_path, 3)
        first = InspectHistoryUseCase(tmp_path).run(limit=0)
        second = InspectHistoryUseCase(tmp_path).run(limit=0)
        assert [r.to_dict() for r in first] == [r.to_dict() for r in second]


class TestLargeHistoryLimit:
    """AC 2 — default bound + explicit limit + truncated flags end to end."""

    def test_default_limit_returns_newest_20(self, tmp_path: Path) -> None:
        _seed_lines(tmp_path, 25)
        records = InspectHistoryUseCase(tmp_path).run()
        assert len(records) == 20
        assert records[0].wallpaper == f"{24:064d}"
        assert records[-1].wallpaper == f"{5:064d}"

    def test_helper_reports_truncated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _seed_lines(tmp_path, 25)
        state_root = tmp_path
        # Point the helper at this state_root via XDG_STATE_HOME layout.
        import runtime.cli.main as cli_main

        monkeypatch.setattr(
            cli_main, "_resolve_state_root", lambda: state_root
        )
        entries, total = cli_main._run_inspect_history(20)
        assert len(entries) == 20
        assert total == 25
        assert total > len(entries)

    def test_helper_limit_zero_returns_all_untruncated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_lines(tmp_path, 25)
        import runtime.cli.main as cli_main

        monkeypatch.setattr(cli_main, "_resolve_state_root", lambda: tmp_path)
        entries, total = cli_main._run_inspect_history(0)
        assert len(entries) == 25
        assert total == 25


class TestHistoryIsolation:
    """AC 4 — live current/ divergence never affects history output."""

    def test_diverged_current_tree_ignored(self, tmp_path: Path) -> None:
        _seed_lines(tmp_path, 2)
        current = tmp_path / "current"
        current.mkdir()
        os.symlink("/elsewhere/wallpaper.png", current / "wallpaper-DP-1.png")
        (tmp_path / "current.json").write_text(json.dumps({"stale": True}))
        records = InspectHistoryUseCase(tmp_path).run(limit=0)
        assert len(records) == 2
        assert records[0].wallpaper == f"{1:064d}"
