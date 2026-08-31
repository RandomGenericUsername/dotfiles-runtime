"""Integration tests for SeedCacheUseCase — real filesystem, real JsonStateRepository.

Tests end-to-end: absent current.json → seed.run() → verify current.json written
+ current/ symlinks resolve + history.jsonl has seed line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WALLPAPER_PNG = FIXTURES / "wallpaper.png"


def _make_wallpaper_hash() -> str:
    return hash_file(WALLPAPER_PNG)


class _FakeCsg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.adapters.hashing import canonical_hash_dir, palette_entry_hash

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")

        # Compute hashes for meta
        wh = hash_file(wallpaper_path)
        templates_dir = Path(__file__).resolve().parents[3] / (
            "src/cli-tools/color-scheme-generator/src/"
            "color_scheme_generator/defaults/templates"
        )
        if templates_dir.is_dir():
            th = canonical_hash_dir(templates_dir)
        else:
            th = "0" * 64

        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        peh = palette_entry_hash(wh, th)
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=peh,
            source_wallpaper_hash=wh,
            input_template_hash=th,
            artifact_hashes=PaletteArtifacts(
                colors_yaml=hash_file(output_dir / "colors.yaml"),
                colors_conf=hash_file(output_dir / "colors.conf"),
                colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
            ),
            generated_at="2026-01-01T00:00:00Z",
        )


class _FakeWeg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash="1" * 64,
            source_wallpaper_hash="a" * 64,
            input_catalog_hash="2" * 64,
            artifact_hashes=EffectsArtifacts(**{"effect.png": "3" * 64}),
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
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        from runtime.domain.models import IconsArtifacts, IconsEntry

        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash="4" * 64,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes=IconsArtifacts(**{"icon.svg": "7" * 64}),
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

    def _setup_install_spine(self, install_spine: Path) -> None:
        generated = install_spine / "generated"
        generated.mkdir(parents=True)
        (generated / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())

    def test_end_to_end_seeding(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        self._setup_install_spine(install_spine)
        state_root = tmp_path / "state"

        state_repo = JsonStateRepository(state_root=state_root)
        use_case = SeedCacheUseCase(
            state_repo=state_repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=state_root,
        )

        # No current.json initially
        assert state_repo.load_current() is None

        # Run seeding
        use_case.run()

        # current.json is now written
        loaded = state_repo.load_current()
        assert loaded is not None
        assert loaded.schema_version == 2
        assert loaded.wallpaper.content_hash == _make_wallpaper_hash()
        assert "DP-1" in loaded.monitors

        # current/ symlinks exist and resolve
        current_dir = state_root / "current"
        assert current_dir.is_dir()
        wp_link = current_dir / "wallpaper-DP-1.png"
        assert wp_link.is_symlink()
        assert wp_link.exists()  # symlink resolves

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
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        self._setup_install_spine(install_spine)
        state_root = tmp_path / "state"

        state_repo = JsonStateRepository(state_root=state_root)
        use_case = SeedCacheUseCase(
            state_repo=state_repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=state_root,
        )
        use_case.run()

        # All symlinks in current/ resolve
        current_dir = state_root / "current"
        for symlink in current_dir.iterdir():
            if symlink.is_symlink():
                assert symlink.exists(), f"dangling symlink: {symlink}"

    def test_install_spine_not_modified(self, tmp_path: Path) -> None:
        """AC 5: Nothing written under install_spine."""
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        self._setup_install_spine(install_spine)
        state_root = tmp_path / "state"

        # Record before
        before = set(install_spine.rglob("*"))

        state_repo = JsonStateRepository(state_root=state_root)
        use_case = SeedCacheUseCase(
            state_repo=state_repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=state_root,
        )
        use_case.run()

        # Install spine unchanged
        after = set(install_spine.rglob("*"))
        assert before == after
