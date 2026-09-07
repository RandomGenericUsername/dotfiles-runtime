"""Integration tests for SeedCacheUseCase — real filesystem, real JsonStateRepository.

Tests end-to-end: absent current.json → seed.run() → verify current.json written
+ current/ symlinks resolve + history.jsonl has seed line. Also covers corrupt
state surfacing and reseed after a crashed prior run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.flock_seed_mutex import FlockSeedMutex
from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WALLPAPER_PNG = FIXTURES / "wallpaper.png"


def _make_wallpaper_hash() -> str:
    return hash_file(WALLPAPER_PNG)


class _FakeCsg:
    """Contract-honest fake: output dir name is the entry hash identity."""

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

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
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeWeg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

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
    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> object:
        from runtime.domain.models import IconsArtifacts, IconsEntry

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


class TestSeedCacheIntegration:
    """End-to-end integration: absent current.json → seed → verify."""

    def _setup(self, tmp_path: Path) -> tuple[Path, Path, JsonStateRepository, Any]:
        install_spine = tmp_path / "install"
        # The assets role (Story 2-6 AC 2) deploys wallpapers to
        # <install>/wallpapers/ including default.png. The runtime's
        # seed looks for the wallpaper there.
        wallpapers = install_spine / "wallpapers"
        wallpapers.mkdir(parents=True)
        (wallpapers / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())
        # Templates/catalog/icon assets so palette/effects/icons seed fully
        csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
        csg_templates.mkdir(parents=True)
        (csg_templates / "default.yaml").write_text("window: {}\n")
        weg_config = install_spine / "config" / "weg"
        weg_config.mkdir(parents=True)
        (weg_config / "effects.yaml").write_text("effects: []\n")
        # Provisioned-machine reality (Story gt-2-2): the AGS consumer-pointer
        # parent exists; the no-mkdir parent guard requires it.
        (install_spine / "config" / "ags").mkdir(parents=True, exist_ok=True)
        itr_templates = install_spine / "icon-templates"
        itr_templates.mkdir(parents=True)
        (itr_templates / "terminal.svg").write_text("<svg/>")
        (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
        (install_spine / "icon-mappings" / "icons.yaml").write_text(
            "icons: {}\n"
        )

        state_root = tmp_path / "state"
        state_repo = JsonStateRepository(state_root=state_root)

        from runtime.application.seed_cache import SeedCacheUseCase

        use_case = SeedCacheUseCase(
            state_repo=state_repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
        )
        return install_spine, state_root, state_repo, use_case

    def test_end_to_end_seeding(self, tmp_path: Path) -> None:
        install_spine, state_root, state_repo, use_case = self._setup(tmp_path)

        # No current.json initially
        assert state_repo.load_current() is None

        # Run seeding
        use_case.run()

        # current.json is now written
        loaded = state_repo.load_current()
        assert loaded is not None
        assert loaded.schema_version == 2
        assert loaded.wallpaper.content_hash == _make_wallpaper_hash()
        assert loaded.wallpaper.source_path == ""
        assert "DP-1" in loaded.monitors

        # Seeded entries carry real hashes — meta.json agrees with on-disk content.
        # (current.json's minimal Story 1.10 projection stores {hash, generated_at}
        # only and reconstructs with sentinels on load; hydration is deferred.)
        assert loaded.palette is not None
        peh = loaded.palette.entry_hash
        meta = json.loads(
            (state_root / "cache" / "palettes" / peh / "meta.json").read_text()
        )
        assert meta["entry_hash"] == peh
        assert meta["input_template_hash"] != "0" * 64
        assert meta["artifact_hashes"]["colors.yaml"] == hash_file(
            state_root / "cache" / "palettes" / peh / "colors.yaml"
        )
        assert meta["artifact_hashes"]["colors.conf"] == hash_file(
            state_root / "cache" / "palettes" / peh / "colors.conf"
        )
        assert meta["artifact_hashes"]["colors.gtk.css"] == hash_file(
            state_root / "cache" / "palettes" / peh / "colors.gtk.css"
        )
        assert loaded.effects is not None
        assert loaded.icons is not None

        # current/ symlinks exist and resolve
        current_dir = state_root / "current"
        assert current_dir.is_dir()
        wp_link = current_dir / "wallpaper-DP-1.png"
        assert wp_link.is_symlink()
        assert wp_link.exists()  # symlink resolves
        for name in (
            "colors.conf",
            "colors.gtk.css",
            "colors.yaml",
            "colors.adw.css",
            "colors.sequences",
        ):
            assert (current_dir / name).is_symlink()
            assert (current_dir / name).exists()
        assert (current_dir / "effects").is_symlink()
        assert (current_dir / "effects").exists()
        assert (current_dir / "icons").is_symlink()
        assert (current_dir / "icons").exists()

        # history.jsonl has seed line
        history_path = state_root / "history.jsonl"
        assert history_path.is_file()
        lines = history_path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["trigger"] == "seed"
        assert data["wallpaper"] == _make_wallpaper_hash()
        assert data["ts"].endswith("Z")

    def test_no_dangling_symlinks(self, tmp_path: Path) -> None:
        """AC 1: No dangling symlinks on fresh machine."""
        _, state_root, _, use_case = self._setup(tmp_path)
        use_case.run()

        # All symlinks in current/ resolve
        current_dir = state_root / "current"
        for symlink in current_dir.iterdir():
            if symlink.is_symlink():
                assert symlink.exists(), f"dangling symlink: {symlink}"

    def test_install_spine_unmodified_except_r2_symlink(self, tmp_path: Path) -> None:
        """AD-11 + Epic 4 R2 exception: nothing written under install_spine
        EXCEPT the spec'd consumer pointer paths (gt-2-2 pointer class —
        parents are NEVER created; gtk pointers skip on absent parents)."""
        install_spine, _, _, use_case = self._setup(tmp_path)

        # Record before
        before = set(install_spine.rglob("*"))
        use_case.run()

        # Only the ags pointer symlink may appear — no created parent dirs,
        # no gtk pointer files (their parents are absent pre-gt-3-1).
        after = set(install_spine.rglob("*"))
        new_files = after - before
        allowed = {
            install_spine / "config" / "ags" / "colors.css",
        }
        assert new_files == allowed, (
            f"only the spec'd consumer pointer symlink may be written under "
            f"install_spine; got {new_files}"
        )

    def test_seed_creates_all_consumer_pointers_when_gtk_dirs_provisioned(
        self, tmp_path: Path
    ) -> None:
        """gt-2-2: with provisioned gtk spine dirs the seed path creates all
        three pointers (ags + gtk-3.0 → colors.gtk.css; gtk-4.0 → colors.adw.css)."""
        install_spine, state_root, _, use_case = self._setup(tmp_path)
        (install_spine / "config" / "gtk-3.0").mkdir()
        (install_spine / "config" / "gtk-4.0").mkdir()

        use_case.run()

        current = state_root / "current"
        assert os.readlink(install_spine / "config" / "ags" / "colors.css") == str(
            current / "colors.gtk.css"
        )
        assert os.readlink(install_spine / "config" / "gtk-3.0" / "colors.css") == str(
            current / "colors.gtk.css"
        )
        assert os.readlink(install_spine / "config" / "gtk-4.0" / "colors.css") == str(
            current / "colors.adw.css"
        )

    def test_corrupt_current_json_fails_loudly(self, tmp_path: Path) -> None:
        """Corrupt state must surface as a loud error, not a silent reseed."""
        _, state_root, state_repo, use_case = self._setup(tmp_path)
        state_root.mkdir(parents=True)
        (state_root / "current.json").write_text("{not valid json")
        with pytest.raises(ValueError):
            use_case.run()
        # State untouched, no seeding performed
        assert (state_root / "current.json").read_text() == "{not valid json"
        assert not (state_root / "history.jsonl").exists()

    def test_reseed_after_crashed_run(self, tmp_path: Path) -> None:
        """Crash between cache population and save: reseed reuses cache entries."""
        _, state_root, state_repo, use_case = self._setup(tmp_path)
        use_case.run()
        first = state_repo.load_current()
        assert first is not None

        # Simulate crash: wipe state, keep cache + current/ symlinks
        (state_root / "current.json").unlink()
        (state_root / "history.jsonl").unlink()

        use_case.run()

        second = state_repo.load_current()
        assert second is not None
        assert second.palette is not None
        assert first.palette is not None
        assert second.palette.entry_hash == first.palette.entry_hash
        assert second.effects is not None
        assert second.icons is not None
        lines = (state_root / "history.jsonl").read_text().strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0])["trigger"] == "seed"
