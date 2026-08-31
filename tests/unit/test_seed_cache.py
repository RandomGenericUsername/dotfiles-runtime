"""Unit tests for SeedCacheUseCase and CacheSeeder adapter.

Tests AC 1-5: first-run detection, cache population, symlink repoint,
history append, domain purity, and idempotency.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from runtime.adapters.hashing import HASH_ALGORITHM, hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    PaletteEntry,
    WallpaperEntry,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WALLPAPER_PNG = FIXTURES / "wallpaper.png"


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _make_wallpaper_hash() -> str:
    return hash_file(WALLPAPER_PNG)


def _make_state(*, wallpaper_hash: str | None = None) -> DesktopState:
    wh = wallpaper_hash or _make_wallpaper_hash()
    now = _now_z()
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wh,
            source_path=str(WALLPAPER_PNG),
            imported_at=now,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wh,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            ),
        },
        palette=None,
        effects=None,
        icons=None,
        applied_at=now,
    )


class _FakeStateRepo:
    """Fake IStateRepository for testing."""

    def __init__(self, state: DesktopState | None = None) -> None:
        self._state = state
        self.saved: list[DesktopState] = []

    def load_current(self) -> DesktopState | None:
        return self._state

    def save(self, state: DesktopState) -> None:
        self._state = state
        self.saved.append(state)


class _FakeCsg:
    """Fake IColorSchemeGenerator for testing."""

    def generate(self, wallpaper_path: Path, output_dir: Path) -> PaletteEntry:
        from runtime.domain.models import PaletteArtifacts

        output_dir.mkdir(parents=True, exist_ok=True)
        # Write minimal artifacts
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash="b" * 64,
            source_wallpaper_hash="a" * 64,
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml="d" * 64,
                colors_conf="e" * 64,
                colors_gtk_css="f" * 64,
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    """Fake IEffectsGenerator for testing."""

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        # Write a minimal PNG artifact
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash="1" * 64,
            source_wallpaper_hash="a" * 64,
            input_catalog_hash="2" * 64,
            artifact_hashes=EffectsArtifacts(**{"effect.png": "3" * 64}),
            generated_at=_now_z(),
        )


class _FakeItr:
    """Fake IIconRenderer for testing."""

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> Any:
        from runtime.domain.models import IconsArtifacts, IconsEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        # Write a minimal SVG artifact
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash="4" * 64,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes=IconsArtifacts(**{"icon.svg": "7" * 64}),
            generated_at=_now_z(),
        )


class _FakeFactory:
    """Fake IWallpaperBackendFactory for testing."""

    def create_static(self, backend_type: object) -> object:
        raise NotImplementedError

    def create_video(self, backend_type: object) -> object:
        raise NotImplementedError

    def auto_detect(self, source_path: str) -> None:
        return None


# ═══════════════════════════════════════════════════════════════════
# CacheSeeder adapter tests
# ═══════════════════════════════════════════════════════════════════


class TestCacheSeederHardlinkWallpaper:
    """CacheSeeder.hardlink_wallpaper tests."""

    def test_hardlink_creates_cache_entry(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        wh = _make_wallpaper_hash()
        result = seeder.hardlink_wallpaper(WALLPAPER_PNG, wh)
        assert result.is_file()
        assert result == tmp_path / "cache" / "wallpapers" / wh / "wallpaper.png"

    def test_hardlink_preserves_content(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        wh = _make_wallpaper_hash()
        result = seeder.hardlink_wallpaper(WALLPAPER_PNG, wh)
        assert result.read_bytes() == WALLPAPER_PNG.read_bytes()

    def test_hardlink_raises_on_non_file(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        with pytest.raises(ValueError, match="regular file"):
            seeder.hardlink_wallpaper(tmp_path / "nonexistent", "a" * 64)


class TestCacheSeederWriteMeta:
    """CacheSeeder write_*_meta tests."""

    def test_write_wallpaper_meta(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        wh = _make_wallpaper_hash()
        seeder.write_wallpaper_meta(wh, "/test/wp.png")
        meta_path = tmp_path / "cache" / "wallpapers" / wh / "meta.json"
        assert meta_path.is_file()
        data = json.loads(meta_path.read_text())
        assert data["hash_algorithm"] == "sha256"
        assert data["kind"] == "wallpaper"
        assert data["content_hash"] == wh
        assert data["source_path"] == "/test/wp.png"
        assert data["imported_at"].endswith("Z")

    def test_write_palette_meta(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        seeder.write_palette_meta(
            entry_hash="b" * 64,
            source_wallpaper_hash="a" * 64,
            input_template_hash="c" * 64,
            artifact_hashes={"colors.yaml": "d" * 64},
        )
        meta_path = tmp_path / "cache" / "palettes" / ("b" * 64) / "meta.json"
        assert meta_path.is_file()
        data = json.loads(meta_path.read_text())
        assert data["kind"] == "palette"
        assert data["entry_hash"] == "b" * 64


class TestCacheSeederSymlinkRepoint:
    """CacheSeeder.repoint_current_symlink tests."""

    def test_repoint_creates_symlink(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        target = tmp_path / "cache" / "wallpapers" / ("a" * 64) / "wallpaper.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"test")
        result = seeder.repoint_current_symlink("wallpaper-DP-1.png", target)
        assert result.is_symlink()
        assert result.readlink() == target

    def test_repoint_is_atomic(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        target = tmp_path / "cache" / "wallpapers" / ("a" * 64) / "wallpaper.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"test")
        result = seeder.repoint_current_symlink("wallpaper-DP-1.png", target)
        # No tmp files left
        assert list(tmp_path.glob("current/*.tmp.*")) == []


class TestCacheSeederAppendHistory:
    """CacheSeeder.append_history tests (AD-4)."""

    def test_append_creates_history_jsonl(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        seeder.append_history(
            trigger="seed",
            wallpaper_hash="a" * 64,
            palette_hash="b" * 64,
        )
        history_path = tmp_path / "history.jsonl"
        assert history_path.is_file()
        lines = history_path.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["trigger"] == "seed"
        assert data["wallpaper"] == "a" * 64
        assert data["palette"] == "b" * 64
        assert data["effects"] is None
        assert data["icons"] is None

    def test_append_is_atomic(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        seeder.append_history(trigger="seed", wallpaper_hash="a" * 64)
        seeder.append_history(trigger="apply", wallpaper_hash="b" * 64)
        history_path = tmp_path / "history.jsonl"
        lines = history_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["trigger"] == "seed"
        assert json.loads(lines[1])["trigger"] == "apply"

    def test_append_has_valid_json(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        seeder.append_history(trigger="seed", wallpaper_hash="a" * 64)
        history_path = tmp_path / "history.jsonl"
        data = json.loads(history_path.read_text().strip())
        assert data["ts"].endswith("Z")
        assert data["trigger"] == "seed"
        assert data["source_path"] == ""


# ═══════════════════════════════════════════════════════════════════
# SeedCacheUseCase tests
# ═══════════════════════════════════════════════════════════════════


class TestSeedCacheUseCaseSkipsOnExisting:
    """AC 4: Seeding skipped when current.json exists."""

    def test_seeding_skipped_when_current_exists(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        state = _make_state()
        repo = _FakeStateRepo(state)
        use_case = SeedCacheUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=tmp_path / "install",
            state_root=tmp_path,
        )
        use_case.run()
        # No cache writes, no history append
        assert not (tmp_path / "history.jsonl").exists()
        assert list(tmp_path.glob("cache/**/*")) == []


class TestSeedCacheUseCaseRunsOnFirstRun:
    """AC 1, 2, 3: Seeding runs when current is absent."""

    def _setup_install_spine(self, install_spine: Path) -> None:
        """Create provisioning output."""
        generated = install_spine / "generated"
        generated.mkdir(parents=True)
        # Copy fixture wallpaper as default.png
        (generated / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())

    def test_seeding_creates_cache_and_history(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        self._setup_install_spine(install_spine)
        repo = _FakeStateRepo()  # No state = first run

        use_case = SeedCacheUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=tmp_path,
        )
        use_case.run()

        # current.json was saved
        assert len(repo.saved) == 1
        saved_state = repo.saved[0]
        assert saved_state.schema_version == 2
        assert saved_state.wallpaper.content_hash == _make_wallpaper_hash()

        # history.jsonl has seed line
        history_path = tmp_path / "history.jsonl"
        assert history_path.is_file()
        data = json.loads(history_path.read_text().strip())
        assert data["trigger"] == "seed"
        assert data["wallpaper"] == _make_wallpaper_hash()

    def test_seeding_creates_wallpaper_cache(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        self._setup_install_spine(install_spine)
        repo = _FakeStateRepo()

        use_case = SeedCacheUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=tmp_path,
        )
        use_case.run()

        wh = _make_wallpaper_hash()
        wallpaper_cache = tmp_path / "cache" / "wallpapers" / wh / "wallpaper.png"
        assert wallpaper_cache.is_file()
        assert wallpaper_cache.read_bytes() == WALLPAPER_PNG.read_bytes()

        meta_path = tmp_path / "cache" / "wallpapers" / wh / "meta.json"
        assert meta_path.is_file()
        meta = json.loads(meta_path.read_text())
        assert meta["hash_algorithm"] == "sha256"

    def test_seeding_creates_current_symlinks(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        self._setup_install_spine(install_spine)
        repo = _FakeStateRepo()

        use_case = SeedCacheUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=tmp_path,
        )
        use_case.run()

        # current/ directory exists
        current_dir = tmp_path / "current"
        assert current_dir.is_dir()

        # Wallpaper symlink per monitor
        wp_link = current_dir / "wallpaper-DP-1.png"
        assert wp_link.is_symlink()

    def test_seeding_raises_when_provisioning_output_missing(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        repo = _FakeStateRepo()
        use_case = SeedCacheUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=tmp_path / "nonexistent",
            state_root=tmp_path,
        )
        with pytest.raises(RuntimeError, match="provisioning output not found"):
            use_case.run()

    def test_seeding_raises_when_default_png_missing(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        (install_spine / "generated").mkdir(parents=True)
        # No default.png
        repo = _FakeStateRepo()
        use_case = SeedCacheUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=tmp_path,
        )
        with pytest.raises(RuntimeError, match="default wallpaper not found"):
            use_case.run()


class TestSeedCacheIdempotent:
    """AC 4: Second run is no-op."""

    def test_idempotent_second_run(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        generated = install_spine / "generated"
        generated.mkdir(parents=True)
        (generated / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())

        repo = _FakeStateRepo()
        use_case = SeedCacheUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=tmp_path,
        )

        # First run
        use_case.run()
        assert len(repo.saved) == 1

        # Second run - should be no-op
        use_case.run()
        assert len(repo.saved) == 1  # Still only one save


class TestNothingWrittenToInstallSpine:
    """AC 5: Nothing written under install_spine."""

    def test_install_spine_unmodified(self, tmp_path: Path) -> None:
        from runtime.application.seed_cache import SeedCacheUseCase

        install_spine = tmp_path / "install"
        generated = install_spine / "generated"
        generated.mkdir(parents=True)
        (generated / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())

        # Record install_spine state before
        before_files = set(install_spine.rglob("*"))

        repo = _FakeStateRepo()
        use_case = SeedCacheUseCase(
            state_repo=repo,
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            factory=_FakeFactory(),
            install_spine=install_spine,
            state_root=tmp_path,
        )
        use_case.run()

        # Install spine unchanged
        after_files = set(install_spine.rglob("*"))
        assert before_files == after_files
