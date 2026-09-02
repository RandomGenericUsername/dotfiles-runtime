"""Integration tests for ReconcileDesktopStateUseCase — real filesystem,
real JsonStateRepository, real CacheSeeder, fake adapters.

Mirrors test_apply_wallpaper_integration.py: seeds first via
SeedCacheUseCase, applies a new wallpaper via ApplyWallpaperUseCase
(produces the ahead-of-symlinks current.json), then reconciles and
asserts filesystem outcomes end-to-end.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from runtime.adapters.flock_seed_mutex import FlockSeedMutex
from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WALLPAPER_PNG = FIXTURES / "wallpaper.png"


class _FakeCsg:
    """Contract-honest fake with invocation counters."""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
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
            ),
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeWeg:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        self.calls += 1
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
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeItr:
    def __init__(self) -> None:
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
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeFactory:
    def create_static(self, backend_type: object) -> object:
        raise NotImplementedError

    def create_video(self, backend_type: object) -> object:
        raise NotImplementedError

    def auto_detect(self, source_path: str) -> None:
        return None


class TestReconcileIntegration:
    """End-to-end: seed → apply → reconcile — real FS outcomes."""

    def _setup(
        self, tmp_path: Path
    ) -> tuple[JsonStateRepository, Path, Path, _FakeCsg, _FakeWeg, _FakeItr]:
        install_spine = tmp_path / "install"
        generated = install_spine / "generated"
        generated.mkdir(parents=True)
        (generated / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())
        csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
        csg_templates.mkdir(parents=True)
        (csg_templates / "default.yaml").write_text("window: {}\n")
        weg_config = install_spine / "config" / "weg"
        weg_config.mkdir(parents=True)
        (weg_config / "effects.yaml").write_text("effects: []\n")
        itr_templates = install_spine / "config" / "icon-templates-renderer" / "templates"
        itr_templates.mkdir(parents=True)
        (itr_templates / "terminal.svg").write_text("<svg/>")
        (install_spine / "config" / "icon-templates-renderer" / "icons.yaml").write_text(
            "icons: {}\n"
        )

        state_root = tmp_path / "state"
        state_repo = JsonStateRepository(state_root=state_root)
        csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()

        from runtime.application.apply_wallpaper import ApplyWallpaperUseCase
        from runtime.application.seed_cache import SeedCacheUseCase

        seeder = CacheSeeder(state_root)
        SeedCacheUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run()

        # apply a NEW wallpaper → ahead-of-symlinks current.json
        img = tmp_path / "wall.png"
        img.write_bytes(b"user wallpaper bytes")
        ApplyWallpaperUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        ).run(img)

        # Seeded + applied consumed derivations; counters count RECONCILE
        csg.calls = weg.calls = itr.calls = 0
        return state_repo, state_root, install_spine, csg, weg, itr

    def test_reconcile_repoints_to_apply_entries(self, tmp_path: Path) -> None:
        state_repo, state_root, install_spine, csg, weg, itr = self._setup(tmp_path)
        loaded = state_repo.load_current()
        assert loaded is not None
        assert loaded.palette is not None
        assert loaded.effects is not None
        assert loaded.icons is not None
        wh = loaded.wallpaper.content_hash
        peh = loaded.palette.entry_hash
        eeh = loaded.effects.entry_hash
        ieh = loaded.icons.entry_hash

        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        )
        result = use_case.run()

        # All symlinks point at the apply-produced cache entries
        current = state_root / "current"
        links = {p.name: Path(p.readlink()) for p in current.iterdir() if p.is_symlink()}
        assert links["wallpaper-DP-1.png"] == state_root / "cache" / "wallpapers" / wh / "wallpaper.png"
        assert links["colors.conf"] == state_root / "cache" / "palettes" / peh / "colors.conf"
        assert links["colors.gtk.css"] == state_root / "cache" / "palettes" / peh / "colors.gtk.css"
        assert links["colors.yaml"] == state_root / "cache" / "palettes" / peh / "colors.yaml"
        assert links["effects"] == state_root / "cache" / "effects" / eeh
        assert links["icons"] == state_root / "cache" / "icons" / ieh

        # All symlink targets resolve into cache/
        for target in links.values():
            assert str(target).startswith(str(state_root / "cache"))

        # No copied files — every entry is a symlink
        for p in current.iterdir():
            assert p.is_symlink()
        assert result.repointed
        assert result.skipped == []

    def test_reconcile_is_idempotent(self, tmp_path: Path) -> None:
        state_repo, state_root, install_spine, csg, weg, itr = self._setup(tmp_path)

        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        seeder = CacheSeeder(state_root)
        use_case = ReconcileDesktopStateUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        )

        use_case.run()
        links_before = {p.name: Path(p.readlink()) for p in (state_root / "current").iterdir() if p.is_symlink()}
        history_before = (state_root / "history.jsonl").read_text()

        result = use_case.run()

        assert (csg.calls, weg.calls, itr.calls) == (0, 0, 0)
        links_after = {p.name: Path(p.readlink()) for p in (state_root / "current").iterdir() if p.is_symlink()}
        assert links_after == links_before
        history_after = (state_root / "history.jsonl").read_text()
        assert len(history_after.splitlines()) == len(history_before.splitlines()) + 1

    def test_deleted_palette_cache_entry_is_regenerated(self, tmp_path: Path) -> None:
        state_repo, state_root, install_spine, csg, weg, itr = self._setup(tmp_path)
        loaded = state_repo.load_current()
        assert loaded is not None
        assert loaded.palette is not None
        shutil.rmtree(state_root / "cache" / "palettes" / loaded.palette.entry_hash)

        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        )
        result = use_case.run()

        assert "palette" in result.cache_regenerated
        assert csg.calls == 1
        entry_dir = state_root / "cache" / "palettes" / loaded.palette.entry_hash
        assert entry_dir.is_dir()
        assert (entry_dir / "meta.json").is_file()

    def test_deleted_wallpaper_entry_is_reimported_from_source(self, tmp_path: Path) -> None:
        state_repo, state_root, install_spine, csg, weg, itr = self._setup(tmp_path)
        loaded = state_repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        shutil.rmtree(state_root / "cache" / "wallpapers" / wh)

        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        use_case = ReconcileDesktopStateUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        )
        result = use_case.run()

        assert "wallpaper" in result.cache_regenerated
        assert csg.calls == 0  # derived layers still cache hits
        assert (state_root / "cache" / "wallpapers" / wh / "wallpaper.png").is_file()
        assert hash_file(state_root / "cache" / "wallpapers" / wh / "wallpaper.png") == wh
