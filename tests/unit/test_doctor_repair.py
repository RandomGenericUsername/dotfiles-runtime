"""Unit tests for Story 2.2 doctor repair (DoctorRepairUseCase.repair).

Warm state is produced by the REAL ApplyWallpaperUseCase with
contract-honest fake tools (echo output_dir.name as entry hash — the
test_reconcile.py pattern), then broken per class and repaired through the
REAL pipeline/seeder/reconcile with fake reloaders. Covers Story 2.2 ACs:
quarantine-never-delete, one repair path, single `doctor` history line,
idempotency, no spine writes. CLI shape via monkeypatched composition.
"""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.adapters.cache import quarantine_entry
from runtime.application.doctor import (
    DoctorRepairUseCase,
    DoctorUseCase,
    RepairResult,
)
from runtime.cli.main import app

runner = CliRunner()
TS = "2026-09-10T00:00:00Z"


@pytest.fixture(autouse=True)
def _quiet_seed_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _Csg:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import PaletteEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        artifacts = {
            "colors.yaml": b"yaml",
            "colors.conf": b"conf",
            "colors.gtk.css": b"gtk",
            "colors.adw.css": b"adw",
            "colors.sequences": b"seq",
            "colors.rasi": b"rasi",
        }
        hashes: dict[str, str] = {}
        for name, content in artifacts.items():
            (output_dir / name).write_bytes(content)
            hashes[name.replace(".", "_").replace("-", "_")] = _h(content)
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash=_h(wallpaper_path.read_bytes()),
            input_template_hash="t" * 64,
            artifact_hashes=hashes,  # type: ignore[typeddict-item]
            generated_at=_now_z(),
        )


class _Weg:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import EffectsEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=output_dir.name,
            source_wallpaper_hash=_h(wallpaper_path.read_bytes()),
            input_catalog_hash="c" * 64,
            artifact_hashes={"effect.png": _h(b"\x89PNG\r\n\x1a\n")},
            generated_at=_now_z(),
        )


class _Itr:
    def __init__(self) -> None:
        self.calls = 0

    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> Any:
        from runtime.domain.models import IconsEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_bytes(b"<svg/>")
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=output_dir.name,
            source_palette_hash=palette_hash,
            input_templates_hash="i" * 64,
            input_mappings_hash="m" * 64,
            artifact_hashes={"icon.svg": _h(b"<svg/>")},
            generated_at=_now_z(),
        )


class _Mutex:
    @contextmanager
    def hold(self, blocking: bool = False) -> Any:
        yield None


class _Reloader:
    def __init__(self) -> None:
        self.calls = 0

    def reload(self) -> bool:
        self.calls += 1
        return True


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.install = tmp_path / "install"
        self.state_root = tmp_path / "state"
        self.img = tmp_path / "wall.png"
        self.csg = _Csg()
        self.weg = _Weg()
        self.itr = _Itr()
        self.reloaders = [_Reloader()]

    def build_spine(self) -> None:
        templates = self.install / "config" / "color-scheme-generator" / "templates"
        templates.mkdir(parents=True)
        (templates / "a.j2").write_text("template-a", encoding="utf-8")
        catalog = self.install / "config" / "weg" / "effects.yaml"
        catalog.parent.mkdir(parents=True)
        catalog.write_text("effects: []", encoding="utf-8")
        icon_templates = self.install / "icon-templates"
        icon_templates.mkdir(parents=True)
        (icon_templates / "icon.svg").write_text("<svg/>", encoding="utf-8")
        mappings = self.install / "icon-mappings" / "icons.yaml"
        mappings.parent.mkdir(parents=True)
        mappings.write_text("mappings: {}", encoding="utf-8")
        (self.install / "config" / "ags").mkdir(parents=True, exist_ok=True)
        (self.install / "config" / "rofi").mkdir(parents=True, exist_ok=True)

    def apply(self) -> None:
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.adapters.seeder import CacheSeeder
        from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

        self.repo = JsonStateRepository(state_root=self.state_root)
        self.seeder = CacheSeeder(self.state_root)
        self.mutex = FlockSeedMutex(self.state_root / ".seed.lock")
        self.img.write_bytes(b"wallpaper bytes")
        ApplyWallpaperUseCase(
            state_repo=self.repo,
            csg=self.csg,
            weg=self.weg,
            itr=self.itr,
            install_spine=self.install,
            state_root=self.state_root,
            seeder=self.seeder,
            mutex=self.mutex,
        ).run(self.img)
        # Converge current/ symlinks + record baseline history so the
        # post-setup machine is CLEAN (repair's healthy baseline).
        self.reconcile().run()
        self.reset_counts()

    def reset_counts(self) -> None:
        self.csg.calls = self.weg.calls = self.itr.calls = 0

    def reconcile(self) -> Any:
        from runtime.adapters.csg_adapter import CsgAdapter
        from runtime.adapters.itr_adapter import ItrAdapter
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.adapters.seeder import CacheSeeder
        from runtime.adapters.weg_adapter import WegAdapter
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        _ = (CsgAdapter, ItrAdapter, WegAdapter)
        return ReconcileDesktopStateUseCase(
            state_repo=JsonStateRepository(state_root=self.state_root),
            csg=self.csg,
            weg=self.weg,
            itr=self.itr,
            install_spine=self.install,
            state_root=self.state_root,
            seeder=CacheSeeder(self.state_root),
            mutex=self.mutex,
            reloaders=self.reloaders,
        )

    def repair(self) -> RepairResult:
        from runtime.adapters.json_state_repository import JsonStateRepository

        use_case = DoctorRepairUseCase(
            doctor=DoctorUseCase(
                state_repo=JsonStateRepository(state_root=self.state_root),
                state_root=self.state_root,
            ),
            quarantine=lambda layer, entry_hash: quarantine_entry(
                self.state_root, layer, entry_hash
            ),
            reconcile=self.reconcile(),
            state_root=self.state_root,
        )
        return use_case.repair()

    def check_clean(self) -> bool:
        from runtime.adapters.json_state_repository import JsonStateRepository

        return (
            DoctorUseCase(
                state_repo=JsonStateRepository(state_root=self.state_root),
                state_root=self.state_root,
            )
            .check()
            .clean
        )

    def history_lines(self) -> list[dict[str, Any]]:
        path = self.state_root / "history.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _setup(tmp_path: Path) -> _Env:
    env = _Env(tmp_path)
    env.build_spine()
    env.apply()
    return env


def _entry_dir(env: _Env, state_root_key: str) -> Path:
    state = env.repo.load_current()
    assert state is not None
    layer_map = {
        "palette": ("palettes", state.palette.entry_hash if state.palette else None),
        "effects": ("effects", state.effects.entry_hash if state.effects else None),
        "icons": ("icons", state.icons.entry_hash if state.icons else None),
        "wallpaper": ("wallpapers", state.wallpaper.content_hash),
    }
    layer, entry_hash = layer_map[state_root_key]
    assert entry_hash is not None
    return env.state_root / "cache" / layer / entry_hash


class TestRepairHeals:
    def test_corrupt_meta_quarantined_and_repopulated(self, tmp_path: Path) -> None:
        env = _setup(tmp_path)
        entry = _entry_dir(env, "effects")
        original_meta = (entry / "meta.json").read_bytes()
        (entry / "meta.json").write_bytes(b"{not json")
        before = len(env.history_lines())

        result = env.repair()

        assert len(result.quarantined) == 1
        quarantined = result.quarantined[0]
        assert quarantined.parent.parent.name == ".quarantine"
        assert (quarantined / "meta.json").read_bytes() == b"{not json"  # bytes preserved
        assert "effects" in result.repopulated
        assert env.check_clean() is True
        lines = env.history_lines()
        assert len(lines) == before + 1
        assert lines[-1]["trigger"] == "doctor"
        assert env.csg.calls == env.itr.calls == 0  # effects-only; weg rebuilt it
        assert env.weg.calls == 1
        _ = original_meta

    def test_absent_artifact_quarantined_and_repopulated(self, tmp_path: Path) -> None:
        env = _setup(tmp_path)
        entry = _entry_dir(env, "icons")
        (entry / "icon.svg").unlink()
        result = env.repair()
        assert len(result.quarantined) == 1
        assert env.check_clean() is True
        assert env.itr.calls == 1

    def test_deleted_entry_repopulated_without_quarantine(self, tmp_path: Path) -> None:
        import shutil

        env = _setup(tmp_path)
        shutil.rmtree(_entry_dir(env, "palette"))
        result = env.repair()
        assert result.quarantined == ()
        assert env.check_clean() is True
        assert env.csg.calls == 1

    def test_stray_symlink_repointed_single_history_line(self, tmp_path: Path) -> None:
        env = _setup(tmp_path)
        stray = tmp_path / "stray.conf"
        stray.write_text("stray", encoding="utf-8")
        (env.state_root / "current" / "colors.conf").unlink()
        os.symlink(stray, env.state_root / "current" / "colors.conf")
        before = len(env.history_lines())

        result = env.repair()

        assert result.quarantined == ()
        assert env.check_clean() is True
        assert len(env.history_lines()) == before + 1
        assert env.history_lines()[-1]["trigger"] == "doctor"

    def test_missing_wallpaper_entry_reimported_from_source(self, tmp_path: Path) -> None:
        import shutil

        env = _setup(tmp_path)
        shutil.rmtree(env.state_root / "cache" / "wallpapers")
        result = env.repair()
        assert env.check_clean() is True
        assert "wallpaper" in result.repopulated

    def test_missing_wallpaper_and_source_gone_fails_loud(self, tmp_path: Path) -> None:
        import shutil

        env = _setup(tmp_path)
        shutil.rmtree(env.state_root / "cache" / "wallpapers")
        env.img.unlink()
        with pytest.raises(RuntimeError, match="source no longer exists"):
            env.repair()


class TestDigestAndCanonical:
    def test_tampered_bytes_quarantined_and_repopulated(self, tmp_path: Path) -> None:
        """AC2 headline: artifact bytes tampered in place, meta unchanged."""
        env = _setup(tmp_path)
        entry = _entry_dir(env, "palette")
        (entry / "colors.conf").write_bytes(b"tampered!!")
        result = env.repair()
        assert len(result.quarantined) == 1
        assert env.check_clean() is True
        assert env.csg.calls == 1

    def test_incomplete_palette_meta_quarantined_not_rmtree(self, tmp_path: Path) -> None:
        """Canonical key missing from meta: doctor flags it so repair
        quarantines (preserving bytes) instead of reconcile evicting."""
        env = _setup(tmp_path)
        entry = _entry_dir(env, "palette")
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        meta["artifact_hashes"].pop("colors.rasi")
        (entry / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        result = env.repair()
        assert len(result.quarantined) == 1
        assert (result.quarantined[0] / "colors.rasi").is_file()  # bytes moved, not deleted
        assert env.check_clean() is True

    def test_squat_file_entry_healed(self, tmp_path: Path) -> None:
        import shutil

        env = _setup(tmp_path)
        entry = _entry_dir(env, "effects")
        shutil.rmtree(entry)
        entry.write_text("squatter", encoding="utf-8")
        result = env.repair()
        assert len(result.quarantined) == 1
        assert env.check_clean() is True
        assert env.weg.calls == 1

    def test_rollback_when_reconcile_fails(self, tmp_path: Path) -> None:
        """Seeded-style failure: no wallpaper source + missing entry →
        quarantined entries are restored, state not worse."""
        import dataclasses
        import shutil

        env = _setup(tmp_path)
        state = env.repo.load_current()
        assert state is not None
        env.repo.save(
            dataclasses.replace(
                state,
                wallpaper=dataclasses.replace(state.wallpaper, source_path=""),
            )
        )
        icons_entry = _entry_dir(env, "icons")
        (icons_entry / "meta.json").write_bytes(b"{bad")
        shutil.rmtree(env.state_root / "cache" / "wallpapers")
        with pytest.raises(RuntimeError, match="no source_path"):
            env.repair()
        assert icons_entry.is_dir()  # restored, not stranded in quarantine


class TestQuarantineBytes:
    def test_quarantined_entry_is_byte_identical(self, tmp_path: Path) -> None:
        env = _setup(tmp_path)
        entry = _entry_dir(env, "effects")
        before = _snapshot(entry)
        (entry / "meta.json").write_bytes(b"{bad")
        result = env.repair()
        assert len(result.quarantined) == 1
        after = _snapshot(result.quarantined[0])
        # meta.json changed (it was corrupted), everything else preserved byte-for-byte.
        assert {k: v for k, v in after.items() if k != "meta.json"} == {
            k: v for k, v in before.items() if k != "meta.json"
        }

    def test_multi_entry_corruption_single_history_line(self, tmp_path: Path) -> None:
        import shutil

        env = _setup(tmp_path)
        (_entry_dir(env, "effects") / "meta.json").write_bytes(b"{bad")
        shutil.rmtree(_entry_dir(env, "palette"))
        stray = tmp_path / "stray.conf"
        stray.write_text("stray", encoding="utf-8")
        (env.state_root / "current" / "colors.conf").unlink()
        os.symlink(stray, env.state_root / "current" / "colors.conf")
        before = len(env.history_lines())
        result = env.repair()
        assert env.check_clean() is True
        assert len(result.quarantined) >= 1
        assert len(env.history_lines()) == before + 1
        assert env.history_lines()[-1]["trigger"] == "doctor"


class TestIdempotencyAndHistory:
    def test_second_repair_is_noop(self, tmp_path: Path) -> None:
        env = _setup(tmp_path)
        entry = _entry_dir(env, "effects")
        (entry / "meta.json").write_bytes(b"{bad")
        env.repair()
        env.reset_counts()
        before = len(env.history_lines())

        second = env.repair()

        assert second == RepairResult((), (), None, ())
        assert len(env.history_lines()) == before
        assert env.csg.calls == env.weg.calls == env.itr.calls == 0

    def test_clean_repair_appends_no_history(self, tmp_path: Path) -> None:
        env = _setup(tmp_path)
        before = len(env.history_lines())
        result = env.repair()
        assert result.history_trigger is None
        assert len(env.history_lines()) == before


def _snapshot(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        try:
            st = path.stat(follow_symlinks=False)
        except OSError:
            snapshot[rel] = "vanished"
            continue
        tag = f"{st.st_mode:o}:{st.st_mtime_ns}"
        if path.is_symlink():
            snapshot[rel] = f"link->{os.readlink(path)}:{tag}"
        elif path.is_file():
            snapshot[rel] = f"file:{hashlib.sha256(path.read_bytes()).hexdigest()}:{tag}"
        elif path.is_dir():
            snapshot[rel + "/"] = f"dir:{tag}"
    return snapshot


class TestNoSpineWrites:
    def test_repair_only_touches_state_root(self, tmp_path: Path) -> None:
        env = _setup(tmp_path)
        (env.state_root / "current" / "colors.conf").unlink()
        stray = tmp_path / "stray.conf"
        stray.write_text("stray", encoding="utf-8")
        os.symlink(stray, env.state_root / "current" / "colors.conf")
        spine_before = _snapshot(env.install)
        env.repair()
        after = _snapshot(env.install)
        # Additions must be R2 consumer symlinks; nothing may be removed.
        added = set(after) - set(spine_before)
        removed = set(spine_before) - set(after)
        assert removed == set()
        assert all(after[p].startswith("link->") for p in added), added
        # Changed existing entries must be only R2 symlinks; directory mtimes
        # may shift as symlinks are created/repointed inside them.
        changed = {k for k in set(after) & set(spine_before) if after[k] != spine_before[k]}
        bad = {
            k
            for k in changed
            if not after[k].startswith("link->") and not after[k].startswith("dir:")
        }
        assert bad == set(), bad


class TestCliShape:
    def _result(self, trigger: str | None = "doctor") -> RepairResult:
        return RepairResult(
            quarantined=(Path("/state/cache/.quarantine/effects/aa-1-2"),) if trigger else (),
            repopulated=("effects",) if trigger else (),
            history_trigger=trigger,
            reload_failures=(),
        )

    def test_repair_renders_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_main, "_run_doctor_repair", lambda: self._result())
        result = runner.invoke(app, ["doctor", "--repair"])
        assert result.exit_code == 0
        assert "repaired" in result.output

    def test_clean_repair_renders_noop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_main, "_run_doctor_repair", lambda: self._result(None))
        result = runner.invoke(app, ["doctor", "--repair"])
        assert result.exit_code == 0
        assert "already clean" in result.output

    def test_reload_failure_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        failed = RepairResult(
            quarantined=(),
            repopulated=(),
            history_trigger="doctor",
            reload_failures=("hyprland",),
        )
        monkeypatch.setattr(cli_main, "_run_doctor_repair", lambda: failed)
        result = runner.invoke(app, ["doctor", "--repair"])
        assert result.exit_code == 1
        assert "hyprland" in result.output

    def test_absent_state_exits_one(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
        result = runner.invoke(app, ["doctor", "--repair"])
        assert result.exit_code == 1

    def test_check_remains_default_read_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        repair_called: list[None] = []
        monkeypatch.setattr(
            cli_main,
            "_run_doctor_repair",
            lambda: (repair_called.append(None), self._result())[1],
        )
        from runtime.application.doctor import DoctorReport

        monkeypatch.setattr(
            cli_main, "_run_doctor_check", lambda: DoctorReport(items=(), clean=True)
        )
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert repair_called == []
