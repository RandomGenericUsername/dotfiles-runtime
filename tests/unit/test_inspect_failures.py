"""Unit tests for the runtime-owned failure log (BUG-001).

``history.jsonl`` rejects extra fields / the ``set-failed`` trigger
(additionalProperties: false, closed enum), so failed palette sets are
recorded in ``failures.jsonl`` and surfaced by ``inspect failures``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.seeder import CacheSeeder
from runtime.application.inspect import InspectFailuresUseCase


def _record(
    *,
    ts: str = "2026-10-26T00:00:00Z",
    wallpaper_path: str = "/img/wall.png",
    wallpaper_hash: str = "a" * 64,
    stage: str = "palette",
    tool: str = "csg",
    returncode: int | None = 7,
    stderr: str = "csg exploded",
) -> dict[str, object]:
    return {
        "ts": ts,
        "trigger": "set-failed",
        "wallpaper_path": wallpaper_path,
        "wallpaper_hash": wallpaper_hash,
        "stage": stage,
        "tool": tool,
        "returncode": returncode,
        "stderr": stderr,
        "hint": "re-set the wallpaper / check csg",
        "error": "palette unavailable",
    }


def _write(state_root: Path, records: list[dict[str, object]]) -> Path:
    path = state_root / "failures.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


class TestFailureLogAppend:
    def test_append_history_schema_rejects_set_failed(self, tmp_path: Path) -> None:
        """The fallback is justified: current.json is untouched and history
        must NOT receive the set-failed line."""
        seeder = CacheSeeder(tmp_path)
        seeder.append_failure(
            wallpaper_path="/img/wall.png",
            wallpaper_hash="a" * 64,
            stage="palette",
            tool="csg",
            returncode=7,
            stderr="csg exploded",
            hint="re-set the wallpaper / check csg",
            error="palette unavailable",
        )

        assert not (tmp_path / "history.jsonl").exists()
        record = json.loads((tmp_path / "failures.jsonl").read_text(encoding="utf-8"))
        assert record["trigger"] == "set-failed"
        assert record["returncode"] == 7
        assert record["stderr"] == "csg exploded"

    def test_append_failure_carries_subprocess_diagnostics(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(tmp_path)
        seeder.append_failure(
            wallpaper_path="/img/wall.png",
            wallpaper_hash="a" * 64,
            stage="palette",
            tool="tpr",
            returncode=0,
            stderr="tpr stderr",
            hint="re-set the wallpaper / check tpr",
            error="palette unavailable",
            stdout="tpr stdout",
            elapsed_seconds=1.25,
            timed_out=True,
            termination="SIGINT",
            missing_artifacts=("colors.yaml", "colors.kitty"),
        )

        record = json.loads((tmp_path / "failures.jsonl").read_text(encoding="utf-8"))
        assert record["tool"] == "tpr"
        assert record["stdout"] == "tpr stdout"
        assert record["elapsed_seconds"] == 1.25
        assert record["timed_out"] is True
        assert record["termination"] == "SIGINT"
        assert record["missing_artifacts"] == ["colors.yaml", "colors.kitty"]


class TestInspectFailures:
    def test_reads_newest_first(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            [
                _record(ts="2026-10-26T00:00:00Z", wallpaper_hash="a" * 64),
                _record(ts="2026-10-26T01:00:00Z", wallpaper_hash="b" * 64),
            ],
        )
        records = InspectFailuresUseCase(tmp_path).run()
        assert [r.ts for r in records] == [
            "2026-10-26T01:00:00Z",
            "2026-10-26T00:00:00Z",
        ]

    def test_absent_is_clean(self, tmp_path: Path) -> None:
        assert InspectFailuresUseCase(tmp_path).run() == []

    def test_old_record_without_optional_fields_is_readable(self, tmp_path: Path) -> None:
        _write(tmp_path, [_record(tool="tpr")])
        record = InspectFailuresUseCase(tmp_path).run()[0]
        assert record.tool == "tpr"
        assert record.stdout == ""
        assert record.elapsed_seconds is None
        assert record.timed_out is False
        assert record.termination is None
        assert record.missing_artifacts == ()

    def test_new_record_with_optional_fields_is_readable(self, tmp_path: Path) -> None:
        payload = _record(tool="tpr", returncode=0, stderr="")
        payload.update(
            {
                "stdout": "stdout text",
                "elapsed_seconds": 2.5,
                "timed_out": True,
                "termination": "SIGKILL",
                "missing_artifacts": ["colors.yaml"],
            }
        )
        _write(tmp_path, [payload])
        record = InspectFailuresUseCase(tmp_path).run()[0]
        assert record.stdout == "stdout text"
        assert record.elapsed_seconds == 2.5
        assert record.timed_out is True
        assert record.termination == "SIGKILL"
        assert record.missing_artifacts == ("colors.yaml",)
        assert record.to_dict()["missing_artifacts"] == ["colors.yaml"]

    def test_middle_corruption_is_loud(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_record()])
        path.write_text(
            path.read_text(encoding="utf-8") + "NOT JSON\n" + json.dumps(_record()) + "\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="line 2"):
            InspectFailuresUseCase(tmp_path).run()

    def test_read_only(self, tmp_path: Path) -> None:
        path = _write(tmp_path, [_record()])
        before = path.read_bytes()
        InspectFailuresUseCase(tmp_path).run(limit=0)
        assert path.read_bytes() == before
