"""Integration tests for ApplyWallpaperUseCase — real filesystem, real JsonStateRepository.

Mirrors test_seed_cache_integration.py: fake adapters (no real tools), tmp
state_root + fake install spine. Seeds first via SeedCacheUseCase (proves
the seeded-state Given of AC 1), then applies wallpapers and asserts
filesystem outcomes end-to-end.
"""

from __future__ import annotations

import json
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
        (output_dir / "colors.adw.css").write_text("colors {}")
        (output_dir / "colors.sequences").write_bytes(b"\x1b]4;0;#000\x1b\\")
        (output_dir / "colors.rasi").write_text("* { background: #000; }")
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
                colors_rasi=hash_file(output_dir / "colors.rasi"),
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


class TestApplyWallpaperIntegration:
    """End-to-end: seed → apply → cache hits, real FS outcomes."""

    def _setup(self, tmp_path: Path) -> tuple[JsonStateRepository, Path, Any, _FakeCsg, _FakeWeg, _FakeItr]:
        install_spine = tmp_path / "install"
        # The assets role (Story 2-6 AC 2) deploys wallpapers to
        # <install>/wallpapers/ including default.png. The runtime's
        # seed looks for the wallpaper there.
        wallpapers = install_spine / "wallpapers"
        wallpapers.mkdir(parents=True)
        (wallpapers / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())
        # Templates/catalog/icon assets so palette/effects/icons derive fully
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
        (install_spine / "icon-mappings" / "icons.yaml").write_text(
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

        use_case = ApplyWallpaperUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        )
        # Seeding consumed one derivation per layer; counters count APPLY
        # invocations only.
        csg.calls = weg.calls = itr.calls = 0
        return state_repo, state_root, use_case, csg, weg, itr

    def _new_image(self, tmp_path: Path, name: str, content: bytes) -> Path:
        img = tmp_path / name
        img.write_bytes(content)
        return img

    def test_apply_new_wallpaper_end_to_end(self, tmp_path: Path) -> None:
        state_repo, state_root, use_case, csg, weg, itr = self._setup(tmp_path)
        seeded = state_repo.load_current()
        assert seeded is not None

        img = self._new_image(tmp_path, "wall.png", b"user wallpaper bytes")
        result = use_case.run(img)

        # Adapter invoked exactly once per layer on the miss path
        assert (csg.calls, weg.calls, itr.calls) == (1, 1, 1)

        # current.json reflects the new wallpaper hash + absolute source_path
        loaded = state_repo.load_current()
        assert loaded is not None
        wh = hash_file(img)
        assert loaded.wallpaper.content_hash == wh
        assert loaded.wallpaper.source_path == str(img)
        assert loaded.palette is not None and loaded.palette.entry_hash == result.palette.entry_hash
        assert loaded.effects is not None and loaded.effects.entry_hash == result.effects.entry_hash  # type: ignore[union-attr]
        assert loaded.icons is not None and loaded.icons.entry_hash == result.icons.entry_hash  # type: ignore[union-attr]

        # Seeded monitors preserved with updated source_hash
        assert set(loaded.monitors) == {"DP-1"}
        assert loaded.monitors["DP-1"].source_hash == wh
        assert loaded.monitors["DP-1"].backend == seeded.monitors["DP-1"].backend

        # Cache entries exist with meta.json
        for layer, entry_hash in (
            ("palettes", result.palette.entry_hash),
            ("effects", result.effects.entry_hash),  # type: ignore[union-attr]
            ("icons", result.icons.entry_hash),  # type: ignore[union-attr]
        ):
            entry_dir = state_root / "cache" / layer / entry_hash
            assert entry_dir.is_dir(), f"missing cache entry: {entry_dir}"
            meta = json.loads((entry_dir / "meta.json").read_text())
            assert meta["entry_hash"] == entry_hash
        assert (state_root / "cache" / "wallpapers" / wh / "meta.json").is_file()

        # current.json round-trips real hashes (no sentinels persisted)
        raw = json.loads((state_root / "current.json").read_text())
        assert raw["palette"]["hash"] == result.palette.entry_hash
        assert raw["wallpaper"]["hash"] == wh

    def test_second_apply_of_same_image_is_zero_invocation(self, tmp_path: Path) -> None:
        state_repo, state_root, use_case, csg, weg, itr = self._setup(tmp_path)
        img = self._new_image(tmp_path, "wall.png", b"repeat me bytes")

        use_case.run(img)
        assert (csg.calls, weg.calls, itr.calls) == (1, 1, 1)

        csg.calls = weg.calls = itr.calls = 0
        result = use_case.run(img)

        assert (csg.calls, weg.calls, itr.calls) == (0, 0, 0)
        assert result.cache_hit_palette and result.cache_hit_effects and result.cache_hit_icons
        loaded = state_repo.load_current()
        assert loaded is not None
        assert loaded.wallpaper.content_hash == hash_file(img)

    def test_reapply_previously_used_wallpaper_restores_entries(
        self, tmp_path: Path
    ) -> None:
        state_repo, state_root, use_case, csg, weg, itr = self._setup(tmp_path)
        img_a = self._new_image(tmp_path, "a.png", b"wallpaper a")
        img_b = self._new_image(tmp_path, "b.png", b"wallpaper b")

        first = use_case.run(img_a)
        use_case.run(img_b)
        csg.calls = weg.calls = itr.calls = 0

        restored = use_case.run(img_a)

        assert (csg.calls, weg.calls, itr.calls) == (0, 0, 0)
        assert restored.state.wallpaper.content_hash == hash_file(img_a)
        assert restored.palette.entry_hash == first.palette.entry_hash

        loaded = state_repo.load_current()
        assert loaded is not None
        assert loaded.wallpaper.source_path == str(img_a)
        assert loaded.palette is not None
        assert loaded.palette.entry_hash == first.palette.entry_hash

    def test_apply_does_not_touch_current_or_history(self, tmp_path: Path) -> None:
        """AC 6: no symlink repoint, no history append from apply."""
        state_repo, state_root, use_case, *_ = self._setup(tmp_path)
        seeded_current = state_root / "current"
        assert seeded_current.is_dir()
        before = {p.name: p.readlink() for p in seeded_current.iterdir() if p.is_symlink()}
        history_before = (state_root / "history.jsonl").read_text()

        img = self._new_image(tmp_path, "wall.png", b"scope bytes")
        use_case.run(img)

        after = {p.name: p.readlink() for p in seeded_current.iterdir() if p.is_symlink()}
        assert after == before  # symlinks untouched
        assert (state_root / "history.jsonl").read_text() == history_before

