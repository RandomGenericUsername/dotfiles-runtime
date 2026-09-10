"""Unit tests for Story 2.3 torn-history-tail healing (AD-23).

Real CacheSeeder heals/append; real InspectHistoryUseCase reads; fakes stand
in for DoctorUseCase/Reconcile where the repair orchestration is under test.
Covers Story 2.3 ACs: reader tolerance, repair heal + forensic quarantine,
read-only commands never mutate, safe append over a torn tail.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.seeder import CacheSeeder
from runtime.application.doctor import DoctorRepairUseCase
from runtime.application.inspect import InspectHistoryUseCase

WALL = "a" * 64


def _line(trigger: str = "reconcile") -> str:
    return json.dumps(
        {
            "ts": "2026-09-10T00:00:00Z",
            "trigger": trigger,
            "wallpaper": WALL,
            "palette": None,
            "effects": None,
            "icons": None,
            "source_path": "",
        }
    )


def _write_torn(state_root: Path, complete: int = 2) -> bytes:
    state_root.mkdir(parents=True, exist_ok=True)
    data = ("\n".join(_line() for _ in range(complete)) + "\n" + '{"ts": "2026-09').encode()
    (state_root / "history.jsonl").write_bytes(data)
    return data


class TestHealTornTail:
    def test_torn_tail_healed_with_forensic_quarantine(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(tmp_path)
        original = _write_torn(tmp_path)
        target = seeder.heal_torn_history_tail()
        assert target is not None and target.is_file()
        assert target.read_bytes() == original  # raw bytes preserved
        healed = (tmp_path / "history.jsonl").read_bytes()
        assert healed.endswith(b"\n")
        assert healed == b"\n".join(_line().encode() for _ in range(2)) + b"\n"
        assert seeder.heal_torn_history_tail() is None  # idempotent

    @pytest.mark.parametrize("payload", [b"", b"\n", _line().encode() + b"\n"])
    def test_non_torn_untouched(self, tmp_path: Path, payload: bytes) -> None:
        seeder = CacheSeeder(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "history.jsonl").write_bytes(payload)
        assert seeder.heal_torn_history_tail() is None
        assert (tmp_path / "history.jsonl").read_bytes() == payload

    def test_absent_file_is_noop(self, tmp_path: Path) -> None:
        assert CacheSeeder(tmp_path).heal_torn_history_tail() is None

    def test_append_over_complete_but_unterminated(self, tmp_path: Path) -> None:
        """A complete record missing only its newline is terminated, not merged."""
        seeder = CacheSeeder(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "history.jsonl").write_bytes(_line().encode())  # no final newline
        seeder.append_history(trigger="set", wallpaper_hash=WALL)
        lines = (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)  # both parseable — no concatenation
        assert json.loads(lines[0])["trigger"] == "reconcile"
        assert json.loads(lines[1])["trigger"] == "set"


class TestAppendOverTornTail:
    def test_append_heals_then_writes_parseable_log(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(tmp_path)
        _write_torn(tmp_path, complete=2)
        seeder.append_history(trigger="set", wallpaper_hash=WALL)
        lines = (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        for line in lines:
            json.loads(line)  # every line parseable — no middle corruption
        assert json.loads(lines[-1])["trigger"] == "set"


class _DoctorClean:
    def check(self) -> Any:
        from runtime.application.doctor import DoctorReport

        return DoctorReport(items=(), clean=True)


class _DoctorDrift:
    def check(self) -> Any:
        from runtime.application.doctor import DoctorReport, DriftItem

        item = DriftItem(
            "cache/palettes/aa",
            "entry",
            "diverged",
            "boom",
            layer="palettes",
            entry_hash="a" * 64,
        )
        return DoctorReport(items=(item,), clean=False)


class _Reconcile:
    def __init__(self) -> None:
        self.triggers: list[str] = []

    def run(self, trigger: str = "reconcile") -> Any:
        from runtime.application.reconcile import ReconcileResult

        self.triggers.append(trigger)
        return ReconcileResult(
            repointed=[],
            skipped=[],
            state=object(),  # type: ignore[arg-type]
            cache_regenerated=["palette"],
            reload_failures=[],
            consumer_symlinks=[],
        )


class TestRepairHealsTail:
    def test_clean_state_tail_only_heals_without_reconcile(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(tmp_path)
        _write_torn(tmp_path)
        reconcile = _Reconcile()
        doctor = _DoctorClean()
        use_case = DoctorRepairUseCase(
            doctor=doctor,  # type: ignore[arg-type]
            quarantine=lambda layer, entry_hash: None,
            reconcile=reconcile,  # type: ignore[arg-type]
            state_root=tmp_path,
            heal_history_tail=seeder.heal_torn_history_tail,
        )
        result = use_case.repair()
        assert result.history_tail_quarantined is not None
        assert result.history_trigger is None
        assert reconcile.triggers == []  # no drift → no reconcile
        assert (tmp_path / "history.jsonl").read_bytes().endswith(b"\n")

    def test_drift_plus_tail_heals_and_runs_one_reconcile(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(tmp_path)
        _write_torn(tmp_path)
        reconcile = _Reconcile()
        use_case = DoctorRepairUseCase(
            doctor=_DoctorDrift(),  # type: ignore[arg-type]
            quarantine=lambda layer, entry_hash: None,
            reconcile=reconcile,  # type: ignore[arg-type]
            state_root=tmp_path,
            heal_history_tail=seeder.heal_torn_history_tail,
        )
        result = use_case.repair()
        assert result.history_tail_quarantined is not None
        assert result.history_trigger == "doctor"
        assert reconcile.triggers == ["doctor"]


class TestReadCommandsNeverMutate:
    def test_inspect_history_tolerates_torn_tail_read_only(self, tmp_path: Path) -> None:
        _write_torn(tmp_path, complete=2)
        before = (tmp_path / "history.jsonl").read_bytes()
        records = InspectHistoryUseCase(state_root=tmp_path).run(limit=0)
        assert len(records) == 2  # torn tail excluded
        assert (tmp_path / "history.jsonl").read_bytes() == before  # untouched

    def test_plain_doctor_check_never_mutates_history(self, tmp_path: Path) -> None:
        from runtime.application.doctor import DoctorUseCase
        from runtime.domain.models import DesktopState, WallpaperEntry

        _write_torn(tmp_path, complete=2)
        before = (tmp_path / "history.jsonl").read_bytes()

        class _Repo:
            def load_current(self) -> DesktopState:
                return DesktopState(
                    schema_version=2,
                    wallpaper=WallpaperEntry(
                        hash_algorithm="sha256",
                        kind="wallpaper",
                        content_hash=WALL,
                        source_path="/img/w.png",
                        imported_at="2026-09-10T00:00:00Z",
                    ),
                    monitors={},
                    palette=None,
                    effects=None,
                    icons=None,
                    applied_at="2026-09-10T00:00:00Z",
                )

            def save(self, state: object) -> None:
                raise AssertionError("check must never save")

        DoctorUseCase(state_repo=_Repo(), state_root=tmp_path).check()  # type: ignore[arg-type]
        assert (tmp_path / "history.jsonl").read_bytes() == before
        assert not (tmp_path / "cache" / ".quarantine").exists()

    def test_inspect_history_after_heal_reads_full(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(tmp_path)
        _write_torn(tmp_path, complete=2)
        seeder.heal_torn_history_tail()
        seeder.append_history(trigger="doctor", wallpaper_hash=WALL)
        records = InspectHistoryUseCase(state_root=tmp_path).run(limit=0)
        assert len(records) == 3


class TestDriftPlusTailFullLog:
    def test_drift_repair_heals_tail_then_appends_parseable_doctor_line(
        self, tmp_path: Path
    ) -> None:
        seeder = CacheSeeder(tmp_path)
        _write_torn(tmp_path, complete=2)

        class _ReconcileAppends:
            def run(self, trigger: str = "reconcile") -> Any:
                from runtime.application.reconcile import ReconcileResult

                seeder.append_history(trigger=trigger, wallpaper_hash=WALL)

                return ReconcileResult(
                    repointed=[],
                    skipped=[],
                    state=object(),  # type: ignore[arg-type]
                    cache_regenerated=["palette"],
                    reload_failures=[],
                    consumer_symlinks=[],
                )

        use_case = DoctorRepairUseCase(
            doctor=_DoctorDrift(),  # type: ignore[arg-type]
            quarantine=lambda layer, entry_hash: None,
            reconcile=_ReconcileAppends(),  # type: ignore[arg-type]
            state_root=tmp_path,
            heal_history_tail=seeder.heal_torn_history_tail,
        )
        result = use_case.repair()
        assert result.history_tail_quarantined is not None
        assert result.history_trigger == "doctor"
        lines = (tmp_path / "history.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3  # 2 complete + 1 doctor
        for line in lines:
            json.loads(line)
        assert json.loads(lines[-1])["trigger"] == "doctor"


class TestCliTailRender:
    def test_tail_only_renders_healed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        import runtime.cli.main as cli_main
        from runtime.application.doctor import RepairResult
        from runtime.cli.main import app

        monkeypatch.setattr(
            cli_main,
            "_run_doctor_repair",
            lambda: RepairResult(
                (), (), None, (), history_tail_quarantined=Path("/q/history.jsonl")
            ),
        )
        out = CliRunner().invoke(app, ["doctor", "--repair"])
        assert out.exit_code == 0
        assert "healed torn history tail" in out.output
