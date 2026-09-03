"""Unit tests for InspectHistoryUseCase (Story 3.3, rt-3-3).

Covers: newest-first ordering from a multi-line fixture (AC 1, AR-9),
limit newest-N + 0 = all (AC 2), absent/empty history exiting cleanly
(AC 3), the corrupt-line policy incl. torn-tail tolerance (AC 5), and the
read-only invariant (AC 4 — no mutation of any kind).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from runtime.application.inspect import HistoryRecord, InspectHistoryUseCase


def _line(
    *,
    ts: str = "2026-01-01T00:00:00Z",
    trigger: str = "seed",
    wallpaper: str = "a" * 64,
    palette: str | None = "b" * 64,
    effects: str | None = "c" * 64,
    icons: str | None = "d" * 64,
    source_path: str = "",
) -> str:
    return json.dumps(
        {
            "ts": ts,
            "trigger": trigger,
            "wallpaper": wallpaper,
            "palette": palette,
            "effects": effects,
            "icons": icons,
            "source_path": source_path,
        }
    )


def _write_history(state_root: Path, lines: list[str]) -> Path:
    path = state_root / "history.jsonl"
    path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    return path


class TestInspectHistoryOrdering:
    """AC 1 — newest-first projection of the pinned 7-field schema."""

    def test_multi_line_fixture_reads_newest_first(self, tmp_path: Path) -> None:
        _write_history(
            tmp_path,
            [
                _line(ts="2026-01-01T00:00:00Z", trigger="seed", wallpaper="a" * 64),
                _line(ts="2026-01-02T00:00:00Z", trigger="set", wallpaper="b" * 64),
                _line(
                    ts="2026-01-03T00:00:00Z", trigger="reconcile", wallpaper="c" * 64
                ),
            ],
        )
        records = InspectHistoryUseCase(tmp_path).run()
        assert [r.ts for r in records] == [
            "2026-01-03T00:00:00Z",
            "2026-01-02T00:00:00Z",
            "2026-01-01T00:00:00Z",
        ]
        assert [r.trigger for r in records] == ["reconcile", "set", "seed"]

    def test_seven_field_shape_pinned_no_schema_version(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [_line()])
        (record,) = InspectHistoryUseCase(tmp_path).run()
        assert record.to_dict() == {
            "ts": "2026-01-01T00:00:00Z",
            "trigger": "seed",
            "wallpaper": "a" * 64,
            "palette": "b" * 64,
            "effects": "c" * 64,
            "icons": "d" * 64,
            "source_path": "",
        }
        assert "schema_version" not in record.to_dict()

    def test_ts_and_hashes_surfaced_verbatim(self, tmp_path: Path) -> None:
        ts = "2026-07-04T02:59:28.282Z"
        wallpaper = "A" * 64
        _write_history(tmp_path, [_line(ts=ts, wallpaper=wallpaper)])
        (record,) = InspectHistoryUseCase(tmp_path).run()
        assert record.ts == ts
        assert record.wallpaper == wallpaper

    def test_nullable_hashes_round_trip(self, tmp_path: Path) -> None:
        _write_history(
            tmp_path, [_line(palette=None, effects=None, icons=None)]
        )
        (record,) = InspectHistoryUseCase(tmp_path).run()
        assert record.palette is None
        assert record.effects is None
        assert record.icons is None

    def test_blank_lines_skipped(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [_line(trigger="seed"), "", "   ", _line(trigger="set")])
        records = InspectHistoryUseCase(tmp_path).run()
        assert [r.trigger for r in records] == ["set", "seed"]


class TestInspectHistoryLimit:
    """AC 2 — bounded default, newest-N limit, 0 = all."""

    def _many_lines(self, count: int) -> list[str]:
        return [
            _line(
                ts=f"2026-01-{(i // 24) + 1:02d}T{(i % 24):02d}:00:00Z",
                trigger="seed",
                wallpaper=f"{i:064d}",
            )
            for i in range(count)
        ]

    def test_default_limit_returns_newest_20(self, tmp_path: Path) -> None:
        _write_history(tmp_path, self._many_lines(25))
        records = InspectHistoryUseCase(tmp_path).run()
        assert len(records) == 20
        assert records[0].wallpaper == f"{24:064d}"
        assert records[-1].wallpaper == f"{5:064d}"

    def test_limit_returns_newest_n(self, tmp_path: Path) -> None:
        _write_history(tmp_path, self._many_lines(5))
        records = InspectHistoryUseCase(tmp_path).run(limit=2)
        assert [r.wallpaper for r in records] == [f"{4:064d}", f"{3:064d}"]

    def test_limit_zero_returns_all(self, tmp_path: Path) -> None:
        _write_history(tmp_path, self._many_lines(25))
        assert len(InspectHistoryUseCase(tmp_path).run(limit=0)) == 25

    def test_negative_limit_raises(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [_line()])
        with pytest.raises(ValueError, match="limit"):
            InspectHistoryUseCase(tmp_path).run(limit=-1)


class TestInspectHistoryEmpty:
    """AC 3 — absent/empty history is clean (exit 0 upstream), never an error."""

    def test_absent_file_returns_empty(self, tmp_path: Path) -> None:
        assert InspectHistoryUseCase(tmp_path).run() == []

    def test_absent_file_needs_no_current_json(self, tmp_path: Path) -> None:
        assert not (tmp_path / "current.json").exists()
        assert InspectHistoryUseCase(tmp_path).run(limit=0) == []

    def test_history_readable_without_current_json(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [_line(trigger="set")])
        assert not (tmp_path / "current.json").exists()
        (record,) = InspectHistoryUseCase(tmp_path).run()
        assert record.trigger == "set"

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [])
        assert InspectHistoryUseCase(tmp_path).run() == []

    def test_blank_lines_only_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "history.jsonl").write_text("\n   \n\n", encoding="utf-8")
        assert InspectHistoryUseCase(tmp_path).run() == []


class TestInspectHistoryCorrupt:
    """AC 5 — middle corruption is loud; torn tail is tolerated."""

    def test_middle_non_json_line_raises_with_line_number(
        self, tmp_path: Path
    ) -> None:
        _write_history(tmp_path, [_line(trigger="seed"), "NOT JSON{{{", _line()])
        with pytest.raises(ValueError, match="line 2"):
            InspectHistoryUseCase(tmp_path).run()

    def test_middle_schema_violation_raises_with_line_number(
        self, tmp_path: Path
    ) -> None:
        bad = json.dumps({"ts": "x", "trigger": "seed"})
        _write_history(tmp_path, [_line(), bad, _line()])
        with pytest.raises(ValueError, match="line 2"):
            InspectHistoryUseCase(tmp_path).run()

    def test_invalid_trigger_raises(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [_line(trigger="apply")])
        with pytest.raises(ValueError, match="line 1.*trigger"):
            InspectHistoryUseCase(tmp_path).run()

    @pytest.mark.parametrize("trigger", ["seed", "set", "reconcile", "force"])
    def test_all_valid_triggers_accepted(
        self, tmp_path: Path, trigger: str
    ) -> None:
        _write_history(tmp_path, [_line(trigger=trigger)])
        (record,) = InspectHistoryUseCase(tmp_path).run()
        assert record.trigger == trigger

    def test_extra_key_schema_version_raises(self, tmp_path: Path) -> None:
        obj = json.loads(_line())
        obj["schema_version"] = 2
        _write_history(tmp_path, [json.dumps(obj)])
        with pytest.raises(ValueError, match="line 1"):
            InspectHistoryUseCase(tmp_path).run()

    def test_torn_tail_tolerated_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _write_history(tmp_path, [_line(trigger="seed"), _line(trigger="set")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"ts": "2026-01-03T00:00:00Z", "trig')
        with caplog.at_level(logging.WARNING):
            records = InspectHistoryUseCase(tmp_path).run(limit=0)
        assert [r.trigger for r in records] == ["set", "seed"]
        assert any("torn trailing line 3" in m for m in caplog.messages)

    def test_torn_tail_excluded_from_counts(self, tmp_path: Path) -> None:
        path = _write_history(tmp_path, [_line(trigger="seed")])
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"partial": ')
        assert len(InspectHistoryUseCase(tmp_path).run(limit=0)) == 1

    def test_trailing_schema_violation_still_raises(
        self, tmp_path: Path
    ) -> None:
        bad = json.dumps({"ts": "x"})
        _write_history(tmp_path, [_line(trigger="seed"), bad])
        with pytest.raises(ValueError, match="line 2"):
            InspectHistoryUseCase(tmp_path).run()

    def test_symlinked_history_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "real-history.jsonl"
        target.write_text(_line() + "\n", encoding="utf-8")
        os.symlink(target, tmp_path / "history.jsonl")
        with pytest.raises(ValueError, match="[Ss]ymlink"):
            InspectHistoryUseCase(tmp_path).run()


class TestInspectHistoryReadOnly:
    """AC 4 — the reader mutates nothing (mirror rt-3.2 read-only style)."""

    def test_absent_file_creates_nothing(self, tmp_path: Path) -> None:
        before = {p.name for p in tmp_path.iterdir()}
        assert InspectHistoryUseCase(tmp_path).run() == []
        assert {p.name for p in tmp_path.iterdir()} == before
        assert not (tmp_path / "history.jsonl").exists()
        assert not (tmp_path / "current.json").exists()

    def test_existing_file_bytes_identical(self, tmp_path: Path) -> None:
        path = _write_history(tmp_path, [_line(), _line(trigger="set")])
        before = path.read_bytes()
        InspectHistoryUseCase(tmp_path).run()
        InspectHistoryUseCase(tmp_path).run(limit=0)
        assert path.read_bytes() == before
        assert not (tmp_path / "current.json").exists()
        assert not (tmp_path / "current").exists()

    def test_record_is_frozen(self, tmp_path: Path) -> None:
        _write_history(tmp_path, [_line()])
        (record,) = InspectHistoryUseCase(tmp_path).run()
        assert isinstance(record, HistoryRecord)
        with pytest.raises(AttributeError):
            record.ts = "mutated"  # type: ignore[misc]
