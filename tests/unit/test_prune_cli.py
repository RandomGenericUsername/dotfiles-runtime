"""Unit + CLI tests for Story 3.3 prune (use case pinned ext, remove_entry, CLI).

Covers: dry-run removes nothing and reports; real prune removes exactly the
plan; idempotent no-op; --keep / --prune-pinned; removal validation; mutex.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.adapters.prune_source import remove_entry
from runtime.application.prune import PruneUseCase
from runtime.cli.main import app
from runtime.domain.models import (
    CacheEntryRef,
    DesktopState,
    PaletteEntry,
    WallpaperEntry,
)

runner = CliRunner()
WP = "c" * 64
ACTIVE = "a" * 64


@pytest.fixture(autouse=True)
def _state_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _h(n: int) -> str:
    return f"{n:064x}"


def _ts(n: int) -> str:
    return f"2026-09-{n:02d}T00:00:00Z"


def _write_palette(state_root: Path, entry_hash: str, ts: str) -> Path:
    d = state_root / "cache" / "palettes" / entry_hash
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "hash_algorithm": "sha256",
                "kind": "palette",
                "generated_at": ts,
                "artifact_hashes": {"colors.yaml": hashlib.sha256(b"y").hexdigest()},
            }
        ),
        encoding="utf-8",
    )
    (d / "colors.yaml").write_text("y", encoding="utf-8")
    return d


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """Active palette + 7 old palettes (days 1..7) + a seed-pin on day 1."""
    from runtime.adapters.json_state_repository import JsonStateRepository

    state_root = tmp_path / "state-home" / "dotfiles"
    _write_palette(state_root, ACTIVE, _ts(20))
    for i in range(1, 8):
        _write_palette(state_root, _h(i), _ts(i))
    JsonStateRepository(state_root=state_root).save(
        DesktopState(
            schema_version=2,
            wallpaper=WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=WP,
                source_path="/img/w.png",
                imported_at=_ts(20),
            ),
            monitors={},
            palette=PaletteEntry(
                hash_algorithm="sha256",
                kind="palette",
                entry_hash=ACTIVE,
                source_wallpaper_hash=WP,
                input_template_hash="t" * 64,
                artifact_hashes={},
                generated_at=_ts(20),
            ),
            effects=None,
            icons=None,
            applied_at=_ts(20),
        )
    )
    (state_root / "history.jsonl").write_text(
        json.dumps(
            {
                "ts": _ts(1),
                "trigger": "seed",
                "wallpaper": WP,
                "palette": _h(1),
                "effects": None,
                "icons": None,
                "source_path": "",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return state_root, state_root / "cache" / "palettes"


class _Repo:
    def __init__(self, state: DesktopState | None) -> None:
        self._state = state

    def load_current(self) -> DesktopState | None:
        return self._state

    def save(self, state: DesktopState) -> None:
        raise AssertionError("no save")


def _plan(keep: int, pins: dict[str, set[str]], prune_pinned: bool) -> object:
    return PruneUseCase(
        state_repo=_Repo(None),  # type: ignore[arg-type]
        entries_for=lambda layer: (
            [CacheEntryRef(f"{i:064x}", _ts(i)) for i in range(1, 8)] if layer == "palettes" else []
        ),
        seed_pins=lambda: pins,
        keep=keep,
    ).run(prune_pinned=prune_pinned)


class TestPinnedExtension:
    def test_pinned_protected_by_default(self) -> None:
        plan = _plan(5, {"palettes": {_h(1)}}, prune_pinned=False)
        assert _h(1) not in plan.removals["palettes"]
        assert plan.removals["palettes"] == (_h(2),)  # only day 2 beyond top-5

    def test_prune_pinned_includes_pins(self) -> None:
        plan = _plan(5, {"palettes": {_h(1)}}, prune_pinned=True)
        assert plan.removals["palettes"] == (_h(1), _h(2))


class TestRemoveEntry:
    def test_removes_valid_dir_and_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state_root = tmp_path / "state"
        d = state_root / "cache" / "palettes" / ACTIVE
        d.mkdir(parents=True)
        (d / "x").write_text("x")
        with caplog.at_level("INFO"):
            assert remove_entry(state_root, "palettes", ACTIVE) is True
        assert not d.exists()
        assert any("removed cache/palettes" in r.message for r in caplog.records)

    def test_absent_is_false(self, tmp_path: Path) -> None:
        assert remove_entry(tmp_path / "state", "palettes", ACTIVE) is False

    def test_unknown_layer_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown cache layer"):
            remove_entry(tmp_path, "bogus", ACTIVE)

    def test_bad_hash_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="64-char lowercase hex"):
            remove_entry(tmp_path, "palettes", "zzz")

    def test_symlinked_entry_rejected(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        (state_root / "cache" / "palettes").mkdir(parents=True)
        real = tmp_path / "real"
        real.mkdir()
        (state_root / "cache" / "palettes" / ACTIVE).symlink_to(real, target_is_directory=True)
        with pytest.raises(ValueError, match="symlink"):
            remove_entry(state_root, "palettes", ACTIVE)

    def test_symlinked_cache_root_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "real-cache"
        real.mkdir()
        (tmp_path / "cache").symlink_to(real, target_is_directory=True)
        with pytest.raises(ValueError, match="cache dir is a symlink"):
            remove_entry(tmp_path, "palettes", ACTIVE)

    def test_symlinked_layer_rejected(self, tmp_path: Path) -> None:
        real = tmp_path / "elsewhere"
        real.mkdir()
        (tmp_path / "cache").mkdir()
        (tmp_path / "cache" / "palettes").symlink_to(real, target_is_directory=True)
        with pytest.raises(ValueError, match="layer dir is a symlink"):
            remove_entry(tmp_path, "palettes", ACTIVE)

    def test_file_squatter_is_not_pruned(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        layer = state_root / "cache" / "palettes"
        layer.mkdir(parents=True)
        (layer / ACTIVE).write_text("squat", encoding="utf-8")
        assert remove_entry(state_root, "palettes", ACTIVE) is False
        assert (layer / ACTIVE).is_file()


def _snapshot(root: Path) -> set[str]:
    # Dry-run must be side-effect-free (no lock file: dry-run takes no mutex).
    return {p.relative_to(root).as_posix() for p in root.rglob("*")}


class TestCliPrune:
    def test_dry_run_reports_and_removes_nothing(self, tmp_path: Path) -> None:
        state_root, _ = _seed(tmp_path)
        before = _snapshot(state_root)
        result = runner.invoke(app, ["inspect", "cache", "prune", "--dry-run"])
        assert result.exit_code == 0
        # top-5 = ACTIVE + days 7,6,5,4; day 1 pinned; → days 2,3 removable
        assert "reclaimable: 2" in result.output
        assert _snapshot(state_root) == before

    def test_real_prune_removes_exactly_plan(self, tmp_path: Path) -> None:
        state_root, _ = _seed(tmp_path)
        result = runner.invoke(app, ["inspect", "cache", "prune"])
        assert result.exit_code == 0
        assert "reclaimed: 2" in result.output
        assert not (state_root / "cache" / "palettes" / _h(2)).exists()
        assert not (state_root / "cache" / "palettes" / _h(3)).exists()
        # protected survive (pinned day1, recent day4+, active)
        assert (state_root / "cache" / "palettes" / _h(1)).is_dir()
        assert (state_root / "cache" / "palettes" / _h(4)).is_dir()
        assert (state_root / "cache" / "palettes" / ACTIVE).is_dir()

    def test_second_prune_is_noop(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        runner.invoke(app, ["inspect", "cache", "prune"])
        result = runner.invoke(app, ["inspect", "cache", "prune"])
        assert result.exit_code == 0
        assert "nothing to prune" in result.output

    def test_keep_override(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        result = runner.invoke(
            app, ["inspect", "cache", "prune", "--dry-run", "--keep", "1", "--format", "json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        # keep=1 → days 5,6,7 prunable (day1 pinned, day2-4 outside keep), active protected
        assert set(payload["removals"]["palettes"]) == {_h(2), _h(3), _h(4), _h(5), _h(6), _h(7)}

    def test_prune_pinned_flag(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        result = runner.invoke(
            app,
            [
                "inspect",
                "cache",
                "prune",
                "--dry-run",
                "--prune-pinned",
                "--keep",
                "5",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        assert _h(1) in json.loads(result.output)["removals"]["palettes"]

    def test_mutex_held(self) -> None:
        source = __import__("inspect").getsource(cli_main._run_prune)
        assert "hold(blocking=True)" in source

    def test_removed_count_key(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        result = runner.invoke(app, ["inspect", "cache", "prune", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["removed_count"] == 2
        assert len(payload["removed_hashes"]) == 2

    def test_absent_state_refuses(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state-home" / "dotfiles"
        _write_palette(state_root, _h(1), _ts(1))  # cache but NO current.json
        result = runner.invoke(app, ["inspect", "cache", "prune"])
        assert result.exit_code == 1
        assert "refusing to prune" in (result.output + getattr(result, "stderr", ""))
        assert (state_root / "cache" / "palettes" / _h(1)).is_dir()  # untouched

    def test_invalid_keep_is_usage_error(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        result = runner.invoke(app, ["inspect", "cache", "prune", "--keep", "-1"])
        assert result.exit_code != 0
        assert result.exit_code == 2

    def test_removal_failure_reports_exit_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import runtime.adapters.prune_source as ps

        _seed(tmp_path)

        def _boom(*args: object, **kwargs: object) -> bool:
            raise OSError("permission denied")

        monkeypatch.setattr(ps, "remove_entry", _boom)
        result = runner.invoke(app, ["inspect", "cache", "prune"])
        assert result.exit_code == 1
        assert "failed" in (result.output + getattr(result, "stderr", ""))


class TestPruneAuditLine:
    """Story R-1 — one audit line per real prune, none on dry-run."""

    def test_real_prune_appends_one_audit_line(self, tmp_path: Path) -> None:
        from runtime.application.inspect import InspectHistoryUseCase

        state_root, _ = _seed(tmp_path)
        result = runner.invoke(app, ["inspect", "cache", "prune"])
        assert result.exit_code == 0
        records = InspectHistoryUseCase(state_root).run(limit=0)
        prune_records = [r for r in records if r.trigger == "prune"]
        assert len(prune_records) == 1
        assert prune_records[0].details == {"removed": 2, "layers": {"palettes": 2}}

    def test_dry_run_appends_nothing(self, tmp_path: Path) -> None:
        state_root, _ = _seed(tmp_path)
        before = (state_root / "history.jsonl").read_bytes()
        result = runner.invoke(app, ["inspect", "cache", "prune", "--dry-run"])
        assert result.exit_code == 0
        assert (state_root / "history.jsonl").read_bytes() == before

    def test_append_failure_is_surfaced(self, tmp_path: Path) -> None:
        state_root, _ = _seed(tmp_path)
        # Real writer failure: history.jsonl replaced by a directory → the
        # locked append cannot open it. Removals still happen; the failure is
        # surfaced and must not be silent.
        history = state_root / "history.jsonl"
        history.unlink()
        history.mkdir()
        result = runner.invoke(app, ["inspect", "cache", "prune"])
        assert result.exit_code == 1
        assert "audit line not written" in (result.output + getattr(result, "stderr", ""))
        # the prune still deleted the planned entries
        assert not (state_root / "cache" / "palettes" / _h(2)).exists()

    def test_audit_append_happens_after_seed_mutex_released(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import contextlib

        import runtime.adapters.flock_seed_mutex as fsm
        import runtime.adapters.seeder as seeder_mod

        _seed(tmp_path)
        order: list[str] = []

        def _hold(self: object, blocking: bool = False) -> object:
            @contextlib.contextmanager
            def _cm() -> object:
                order.append("lock-enter")
                try:
                    yield
                finally:
                    order.append("lock-exit")

            return _cm()

        def _append(self: object, *args: object, **kwargs: object) -> None:
            order.append("append")

        monkeypatch.setattr(fsm.FlockSeedMutex, "hold", _hold)
        monkeypatch.setattr(seeder_mod.CacheSeeder, "append_history", _append)
        result = runner.invoke(app, ["inspect", "cache", "prune"])
        assert result.exit_code == 0
        assert order.index("lock-exit") < order.index("append")

    def test_audit_line_visible_in_inspect_history_json(self, tmp_path: Path) -> None:
        _seed(tmp_path)
        runner.invoke(app, ["inspect", "cache", "prune"])
        result = runner.invoke(app, ["inspect", "history", "--format", "json"])
        assert result.exit_code == 0
        entries = json.loads(result.output)["entries"]
        prune_entries = [e for e in entries if e["trigger"] == "prune"]
        assert len(prune_entries) == 1
        assert prune_entries[0]["details"] == {"removed": 2, "layers": {"palettes": 2}}
