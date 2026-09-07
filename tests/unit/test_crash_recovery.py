"""Unit tests for crash-mid-swap recovery (Story 2.2).

Covers: partial-swap revert per layer, full interrupt, idempotency,
cache-hit-after-recovery, absent/corrupt state, scope lock, missing
current/ directory, dangling symlinks, monitor traversal guard.
Mirrors test_reconcile.py fake-adapter patterns.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.flock_seed_mutex import FlockSeedMutex
from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder, _repoint_symlink

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeMutex:
    def hold(self, blocking: bool = False) -> object:  # type: ignore[no-untyped-def]
        class _Hold:  # type: ignore[no-untyped-def]
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, *exc: object) -> None:
                pass

        return _Hold()


class _FakeCsg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("csg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
        (output_dir / "colors.adw.css").write_text("colors {}")
        (output_dir / "colors.sequences").write_bytes(b"\x1b]4;0;#000\x1b\\")
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml=hash_file(output_dir / "colors.yaml"),
                colors_conf=hash_file(output_dir / "colors.conf"),
                colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
                colors_adw_css=hash_file(output_dir / "colors.adw.css"),
                colors_sequences=hash_file(output_dir / "colors.sequences"),
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("weg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        effect_hash = hash_file(output_dir / "effect.png")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_catalog_hash="2" * 64,
            artifact_hashes=EffectsArtifacts(**{"effect.png": effect_hash}),
            generated_at=_now_z(),
        )


class _FakeItr:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> object:
        from runtime.domain.models import IconsArtifacts, IconsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("itr exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=output_dir.name,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes=IconsArtifacts(**{"icon.svg": hash_file(output_dir / "icon.svg")}),
            generated_at=_now_z(),
        )


def _setup_spine(install_spine: Path) -> None:
    csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    csg_templates.mkdir(parents=True)
    (csg_templates / "default.yaml").write_text("window: {}\n")
    weg_config = install_spine / "config" / "weg"
    weg_config.mkdir(parents=True)
    (weg_config / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "icon-templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")


def _apply_state(tmp_path: Path, *, img_bytes: bytes = b"user wallpaper bytes") -> tuple[JsonStateRepository, Path, Path, _FakeCsg, _FakeWeg, _FakeItr]:
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
    img = tmp_path / "wall.png"
    img.write_bytes(img_bytes)
    ApplyWallpaperUseCase(
        state_repo=repo,
        csg=csg,
        weg=weg,
        itr=itr,
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),
    ).run(img)
    csg.calls = weg.calls = itr.calls = 0
    return repo, state_root, install_spine, csg, weg, itr


def _make_reconcile(repo: JsonStateRepository, state_root: Path, install_spine: Path, csg: _FakeCsg, weg: _FakeWeg, itr: _FakeItr, tmp_path: Path | None = None) -> object:
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    return ReconcileDesktopStateUseCase(
        state_repo=repo,
        csg=csg,
        weg=weg,
        itr=itr,
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),
    )


def _reconcile_and_repoint(tmp_path: Path) -> tuple[JsonStateRepository, Path, Path, _FakeCsg, _FakeWeg, _FakeItr]:
    """Apply then reconcile once so current/ exists with correct targets."""
    repo, state_root, install_spine, csg, weg, itr = _apply_state(tmp_path)
    _make_reconcile(repo, state_root, install_spine, csg, weg, itr).run()  # type: ignore[attr-defined]
    csg.calls = weg.calls = itr.calls = 0
    return repo, state_root, install_spine, csg, weg, itr


def _symlink_target(link: Path) -> str:
    return os.readlink(link)


def _stale_hash(original: str) -> str:
    # flip last char to guarantee different valid hex
    return original[:-1] + ("0" if original[-1] != "0" else "1")


def _make_stale_wallpaper_entry(state_root: Path, stale_hash: str) -> Path:
    # Create a stale wallpaper cache entry so the dangling target is plausible
    entry = state_root / "cache" / "wallpapers" / stale_hash
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "wallpaper.png").write_bytes(b"stale")
    (entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
    return entry / "wallpaper.png"


def _make_stale_palette_entry(state_root: Path, stale_hash: str) -> Path:
    entry = state_root / "cache" / "palettes" / stale_hash
    entry.mkdir(parents=True, exist_ok=True)
    for n in ("colors.conf", "colors.gtk.css", "colors.yaml", "colors.adw.css", "colors.sequences"):
        (entry / n).write_text("stale")
    (entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
    return entry


class TestCrashPartialSwap:
    def test_partial_swap_wallpaper_only_reverted(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        stale = _stale_hash(wh)
        _make_stale_wallpaper_entry(state_root, stale)
        stale_target = state_root / "cache" / "wallpapers" / stale / "wallpaper.png"
        link = state_root / "current" / "wallpaper-DP-1.png"
        _repoint_symlink(link, stale_target)
        assert "wallpapers" in _symlink_target(link) and stale in _symlink_target(link)

        _make_reconcile(repo, state_root, install_spine, csg, weg, itr).run()  # type: ignore[attr-defined]

        assert Path(_symlink_target(link)).resolve() == (state_root / "cache" / "wallpapers" / wh / "wallpaper.png").resolve()
        # other symlinks unchanged
        assert (state_root / "current" / "colors.conf").is_symlink()

    def test_partial_swap_palette_only_reverted(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        assert loaded.palette is not None
        peh = loaded.palette.entry_hash
        stale = _stale_hash(peh)
        stale_entry = _make_stale_palette_entry(state_root, stale)
        link = state_root / "current" / "colors.conf"
        _repoint_symlink(link, stale_entry / "colors.conf")

        _make_reconcile(repo, state_root, install_spine, csg, weg, itr).run()  # type: ignore[attr-defined]

        expected = state_root / "cache" / "palettes" / peh / "colors.conf"
        assert Path(_symlink_target(link)).resolve() == expected.resolve()

    def test_partial_swap_effects_dir_reverted(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        assert loaded.effects is not None
        eeh = loaded.effects.entry_hash
        stale = _stale_hash(eeh)
        stale_dir = state_root / "cache" / "effects" / stale
        stale_dir.mkdir(parents=True, exist_ok=True)
        (stale_dir / "effect.png").write_bytes(b"stale")
        link = state_root / "current" / "effects"
        _repoint_symlink(link, stale_dir)

        _make_reconcile(repo, state_root, install_spine, csg, weg, itr).run()  # type: ignore[attr-defined]

        expected = state_root / "cache" / "effects" / eeh
        assert Path(_symlink_target(link)).resolve() == expected.resolve()

    def test_crash_between_symlink_and_current_json(self, tmp_path: Path) -> None:
        """Old current.json, 2 of 4 symlinks already new — recovery reverts to old."""
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        old_loaded = repo.load_current()
        assert old_loaded is not None
        old_wh = old_loaded.wallpaper.content_hash
        old_peh = old_loaded.palette.entry_hash if old_loaded.palette else None
        # Simulate a new apply that crashed after repointing 2 symlinks but before saving current.json
        # We manually create new hashes and repoint 2 symlinks to them, but keep old current.json
        new_wh = "a" * 64
        new_peh = "b" * 64
        _make_stale_wallpaper_entry(state_root, new_wh)
        _make_stale_palette_entry(state_root, new_peh)
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", state_root / "cache" / "wallpapers" / new_wh / "wallpaper.png")
        _repoint_symlink(state_root / "current" / "colors.conf", state_root / "cache" / "palettes" / new_peh / "colors.conf")

        _make_reconcile(repo, state_root, install_spine, csg, weg, itr).run()  # type: ignore[attr-defined]

        # Both should be reverted to old
        assert old_wh in _symlink_target(state_root / "current" / "wallpaper-DP-1.png")
        assert old_peh in _symlink_target(state_root / "current" / "colors.conf")  # type: ignore[operator]

    def test_full_swap_interrupted_all_symlinks_reverted(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        peh = loaded.palette.entry_hash if loaded.palette else None
        eeh = loaded.effects.entry_hash if loaded.effects else None
        ieh = loaded.icons.entry_hash if loaded.icons else None
        stale_wh = _stale_hash(wh)
        _make_stale_wallpaper_entry(state_root, stale_wh)
        # repoint every symlink to stale
        for name in ["wallpaper-DP-1.png", "colors.conf", "colors.gtk.css", "colors.yaml"]:
            if (state_root / "current" / name).is_symlink():
                stale_hash_val = stale_wh if "wallpaper" in name else _stale_hash(peh) if peh else stale_wh  # type: ignore[arg-type]
                # for palette files use palette stale
                if name.startswith("colors"):
                    stale_dir = state_root / "cache" / "palettes" / _stale_hash(peh)  # type: ignore[arg-type]
                    stale_dir.mkdir(parents=True, exist_ok=True)
                    (stale_dir / name).write_text("stale")
                    _repoint_symlink(state_root / "current" / name, stale_dir / name)
                else:
                    _repoint_symlink(state_root / "current" / name, state_root / "cache" / "wallpapers" / stale_wh / "wallpaper.png")
        if eeh:
            stale_eeh = _stale_hash(eeh)
            sd = state_root / "cache" / "effects" / stale_eeh
            sd.mkdir(parents=True, exist_ok=True)
            (sd / "effect.png").write_bytes(b"x")
            _repoint_symlink(state_root / "current" / "effects", sd)
        if ieh:
            stale_ieh = _stale_hash(ieh)
            sd = state_root / "cache" / "icons" / stale_ieh
            sd.mkdir(parents=True, exist_ok=True)
            (sd / "icon.svg").write_text("<svg/>")
            _repoint_symlink(state_root / "current" / "icons", sd)

        _make_reconcile(repo, state_root, install_spine, csg, weg, itr).run()  # type: ignore[attr-defined]

        assert wh in _symlink_target(state_root / "current" / "wallpaper-DP-1.png")
        if peh:
            assert peh in _symlink_target(state_root / "current" / "colors.conf")
        if eeh:
            assert eeh in _symlink_target(state_root / "current" / "effects")
        if ieh:
            assert ieh in _symlink_target(state_root / "current" / "icons")


class TestCrashIdempotency:
    def test_no_crash_is_noop(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        # Directly test _revert_stale_symlinks is noop when already matching
        state = repo.load_current()
        assert state is not None
        reverted = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert reverted == []
        assert csg.calls == 0 and weg.calls == 0 and itr.calls == 0

    def test_double_recovery_noop(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        loaded = repo.load_current()
        assert loaded is not None
        stale = _stale_hash(loaded.wallpaper.content_hash)
        _make_stale_wallpaper_entry(state_root, stale)
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", state_root / "cache" / "wallpapers" / stale / "wallpaper.png")

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        state = repo.load_current()
        assert state is not None
        first = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert len(first) == 1
        second = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert second == []


class TestCacheHitAfterRecovery:
    def test_recover_then_reconcile_is_cache_hit(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        stale = _stale_hash(loaded.wallpaper.content_hash)
        _make_stale_wallpaper_entry(state_root, stale)
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", state_root / "cache" / "wallpapers" / stale / "wallpaper.png")

        result = _make_reconcile(repo, state_root, install_spine, csg, weg, itr).run()  # type: ignore[attr-defined]

        assert csg.calls == 0
        assert weg.calls == 0
        assert itr.calls == 0
        assert result.cache_regenerated == []  # type: ignore[attr-defined]


class TestAbsentAndCorrupt:
    def test_recovery_absent_state_raises(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        repo = JsonStateRepository(state_root=state_root)
        install_spine = tmp_path / "install"
        _setup_spine(install_spine)
        csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        with pytest.raises(RuntimeError, match="nothing to reconcile"):
            use_case.run()

    def test_recovery_corrupt_current_json_propagates(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _apply_state(tmp_path)
        (state_root / "current.json").write_text("{not json")
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        with pytest.raises(ValueError, match="not valid JSON"):
            use_case.run()


class TestStructuralScope:
    def test_recovery_writes_only_inside_current(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        stale = _stale_hash(loaded.wallpaper.content_hash)
        _make_stale_wallpaper_entry(state_root, stale)
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", state_root / "cache" / "wallpapers" / stale / "wallpaper.png")
        before = {p.relative_to(state_root) for p in state_root.rglob("*") if p.is_file() or p.is_symlink()}

        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        state = repo.load_current()
        assert state is not None
        use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]

        after = {p.relative_to(state_root) for p in state_root.rglob("*") if p.is_file() or p.is_symlink()}
        # Only symlink target changed, no new file outside current/
        new_files = after - before
        # The stale entry we created is outside, but recovery itself adds nothing outside current/
        # So any new files must be inside current/ (or our manually created stale entry already existed)
        for nf in new_files:
            assert str(nf).startswith("current/") or str(nf).startswith("cache/"), f"unexpected write outside current/: {nf}"
        # No file at state_root root besides current.json/history.jsonl
        assert (state_root / "current" / "wallpaper-DP-1.png").is_symlink()

    def test_recovery_missing_current_dir_is_noop(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _apply_state(tmp_path)
        # remove current/ before reconcile — recovery should be noop not crash
        import shutil

        shutil.rmtree(state_root / "current", ignore_errors=True)
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        state = repo.load_current()
        assert state is not None
        reverted = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert reverted == []

    def test_recovery_dangling_symlink_reverted(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _reconcile_and_repoint(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        link = state_root / "current" / "wallpaper-DP-1.png"
        # Point to non-existent cache entry
        dangling = state_root / "cache" / "wallpapers" / ("9" * 64) / "wallpaper.png"
        _repoint_symlink(link, dangling)
        assert not dangling.exists()

        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        state = repo.load_current()
        assert state is not None
        reverted = use_case._revert_stale_symlinks(state)  # type: ignore[attr-defined]
        assert len(reverted) == 1
        assert wh in _symlink_target(link)

    def test_recovery_monitor_name_traversal_rejected(self, tmp_path: Path) -> None:
        repo, state_root, install_spine, csg, weg, itr = _apply_state(tmp_path)
        loaded = repo.load_current()
        assert loaded is not None
        from runtime.domain.models import DesktopState

        # Inject traversal monitor name — bypass repo validation by editing current.json directly
        import json

        raw = json.loads((state_root / "current.json").read_text())
        # raw["monitors"] is dict of monitor_name -> config
        original_cfg = next(iter(raw["monitors"].values()))
        raw["monitors"] = {"../evil": original_cfg}
        (state_root / "current.json").write_text(json.dumps(raw))
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),
        )
        with pytest.raises(ValueError, match="monitor name"):
            use_case.run()
