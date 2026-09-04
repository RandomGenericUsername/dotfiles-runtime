"""Unit tests for SeedCacheUseCase and CacheSeeder adapter.

Tests AC 1-5: first-run detection, cache population (including real
meta.json/staging contract), symlink repoint targets, history append,
domain purity, idempotency, single-flight mutex, and failure policy.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.flock_seed_mutex import FlockSeedMutex
from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    PaletteEntry,
    WallpaperEntry,
)
from runtime.ports.seed_mutex import SeedLockedError

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
    """Contract-honest fake: echoes the output dir's name as entry hash.

    The real CsgAdapter validates ``output_dir.name == entry_hash`` and
    returns a PaletteEntry with real artifact hashes; fakes that ignore
    the name encode the old contract violation. Echoing the name keeps
    the fake consistent with the use case's hash verification.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def generate(self, wallpaper_path: Path, output_dir: Path) -> PaletteEntry:
        from runtime.domain.models import PaletteArtifacts

        if self.fail:
            raise RuntimeError("csg exploded")
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
            generated_at=_now_z(),
        )


class _FakeWeg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

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

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> Any:
        from runtime.domain.models import IconsArtifacts, IconsEntry

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


class _FakeFactory:
    """Fake IWallpaperBackendFactory for testing."""

    def create_static(self, backend_type: object) -> object:
        raise NotImplementedError

    def create_video(self, backend_type: object) -> object:
        raise NotImplementedError

    def auto_detect(self, source_path: str) -> None:
        return None


def _make_use_case(
    tmp_path: Path,
    repo: _FakeStateRepo,
    install_spine: Path,
    *,
    csg: _FakeCsg | None = None,
    weg: _FakeWeg | None = None,
    itr: _FakeItr | None = None,
    monitor_source: Any | None = None,
) -> Any:
    """Construct SeedCacheUseCase with real seeder + flock mutex."""
    from runtime.application.seed_cache import SeedCacheUseCase

    state_root = tmp_path
    return SeedCacheUseCase(
        state_repo=repo,
        csg=csg or _FakeCsg(),
        weg=weg or _FakeWeg(),
        itr=itr or _FakeItr(),
        factory=_FakeFactory(),
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=FlockSeedMutex(state_root / ".seed.lock"),
        monitor_source=monitor_source,
    )


def _setup_install_spine(
    install_spine: Path,
    *,
    with_templates: bool = True,
) -> None:
    """Create provisioning output incl. templates/catalog/icon assets."""
    # The assets role (Story 2-6 AC 2) unpacks wallpapers.tar.gz to
    # <install>/wallpapers/ including default.png. The runtime's seed
    # looks for the wallpaper there (not under generated/).
    wallpapers = install_spine / "wallpapers"
    wallpapers.mkdir(parents=True)
    (wallpapers / "default.png").write_bytes(WALLPAPER_PNG.read_bytes())
    if with_templates:
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

    def test_hardlink_is_idempotent(self, tmp_path: Path) -> None:
        """Crashed-run recovery: existing entry with matching hash is reused."""
        seeder = CacheSeeder(state_root=tmp_path)
        wh = _make_wallpaper_hash()
        first = seeder.hardlink_wallpaper(WALLPAPER_PNG, wh)
        second = seeder.hardlink_wallpaper(WALLPAPER_PNG, wh)
        assert first == second
        assert second.is_file()

    def test_hardlink_rejects_hash_mismatch(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        wh = _make_wallpaper_hash()
        dst = tmp_path / "cache" / "wallpapers" / wh / "wallpaper.png"
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"corrupted content")
        with pytest.raises(RuntimeError, match="does not match"):
            seeder.hardlink_wallpaper(WALLPAPER_PNG, wh)


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

    def test_load_palette_entry_roundtrip(self, tmp_path: Path) -> None:
        """Meta reader rebuilds a PaletteEntry with the real stored hashes."""
        seeder = CacheSeeder(state_root=tmp_path)
        seeder.write_palette_meta(
            entry_hash="b" * 64,
            source_wallpaper_hash="a" * 64,
            input_template_hash="c" * 64,
            artifact_hashes={
                "colors.yaml": "d" * 64,
                "colors.conf": "e" * 64,
                "colors.gtk.css": "f" * 64,
            },
            generated_at="2026-01-01T00:00:00Z",
        )
        entry = seeder.load_palette_entry(tmp_path / "cache" / "palettes" / ("b" * 64))
        assert entry.entry_hash == "b" * 64
        assert entry.input_template_hash == "c" * 64
        assert entry.artifact_hashes["colors_yaml"] == "d" * 64
        assert entry.artifact_hashes["colors_conf"] == "e" * 64
        assert entry.artifact_hashes["colors_gtk_css"] == "f" * 64


class TestCacheSeederSymlinkRepoint:
    """CacheSeeder.repoint_current_symlink tests."""

    def _make_target(self, tmp_path: Path) -> Path:
        target = tmp_path / "cache" / "wallpapers" / ("a" * 64) / "wallpaper.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"test")
        return target

    def test_repoint_creates_symlink(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        target = self._make_target(tmp_path)
        result = seeder.repoint_current_symlink("wallpaper-DP-1.png", target)
        assert result.is_symlink()
        assert result.readlink() == target

    def test_repoint_leaves_no_tmp_files(self, tmp_path: Path) -> None:
        """Atomic repoint: no .tmp.* orphans after success."""
        seeder = CacheSeeder(state_root=tmp_path)
        target = self._make_target(tmp_path)
        seeder.repoint_current_symlink("wallpaper-DP-1.png", target)
        seeder.repoint_current_symlink("wallpaper-DP-1.png", target)  # re-repoint
        assert list(tmp_path.glob("current/*.tmp.*")) == []
        assert (tmp_path / "current" / "wallpaper-DP-1.png").readlink() == target

    def test_repoint_cleans_tmp_on_failure(self, tmp_path: Path) -> None:
        """Failed os.replace (target exists as real directory) must not leak tmp."""
        seeder = CacheSeeder(state_root=tmp_path)
        current_dir = tmp_path / "current"
        current_dir.mkdir()
        (current_dir / "effects").mkdir()  # real directory blocks os.replace
        target = tmp_path / "cache" / "effects" / ("a" * 64)
        target.mkdir(parents=True)
        with pytest.raises(OSError):
            seeder.repoint_current_symlink("effects", target)
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
        """Append-only: sequential writes accumulate, nothing truncated."""
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

    def test_append_refuses_symlinked_history(self, tmp_path: Path) -> None:
        """O_NOFOLLOW: history.jsonl must not be followed through a symlink."""
        seeder = CacheSeeder(state_root=tmp_path)
        outside = tmp_path / "outside.jsonl"
        outside.write_text("")
        link = tmp_path / "history.jsonl"
        link.symlink_to(outside)
        with pytest.raises(OSError):
            seeder.append_history(trigger="seed", wallpaper_hash="a" * 64)

    def test_append_line_has_exactly_seven_fields_no_schema_version(self, tmp_path: Path) -> None:
        """AR-9 pinned schema: exactly 7 fields, NO ``schema_version`` — the
        version field exists only in ``current.json`` (shared-data-contract)."""
        seeder = CacheSeeder(state_root=tmp_path)
        seeder.append_history(
            trigger="reconcile",
            wallpaper_hash="a" * 64,
            palette_hash="b" * 64,
            effects_hash="c" * 64,
            icons_hash="d" * 64,
            source_path="/tmp/wall.png",
        )
        data = json.loads((tmp_path / "history.jsonl").read_text().strip())
        assert set(data) == {
            "ts",
            "trigger",
            "wallpaper",
            "palette",
            "effects",
            "icons",
            "source_path",
        }
        assert "schema_version" not in data


# ═══════════════════════════════════════════════════════════════════
# SeedCacheUseCase tests
# ═══════════════════════════════════════════════════════════════════


class TestSeedCacheUseCaseSkipsOnExisting:
    """AC 4: Seeding skipped when current.json exists."""

    def test_seeding_skipped_when_current_exists(self, tmp_path: Path) -> None:
        state = _make_state()
        repo = _FakeStateRepo(state)
        use_case = _make_use_case(tmp_path, repo, install_spine=tmp_path / "install")
        use_case.run()
        # No cache writes, no history append
        assert not (tmp_path / "history.jsonl").exists()
        assert list(tmp_path.glob("cache/**/*")) == []


class TestSeedCacheUseCaseRunsOnFirstRun:
    """AC 1, 2, 3: Seeding runs when current is absent."""

    def test_seeding_creates_cache_and_history(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()  # No state = first run

        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        # current.json was saved
        assert len(repo.saved) == 1
        saved_state = repo.saved[0]
        assert saved_state.schema_version == 2
        assert saved_state.wallpaper.content_hash == _make_wallpaper_hash()
        # Seed writes no machine path into the state (spec: source_path "")
        assert saved_state.wallpaper.source_path == ""

        # history.jsonl has seed line
        history_path = tmp_path / "history.jsonl"
        assert history_path.is_file()
        data = json.loads(history_path.read_text().strip())
        assert data["trigger"] == "seed"
        assert data["wallpaper"] == _make_wallpaper_hash()

    def test_seeding_populates_palette_cache(self, tmp_path: Path) -> None:
        """Spec Task 4: cache/palettes/<ph>/ has all 3 artifacts + real-hash meta."""
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()

        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        state = repo.saved[0]
        assert state.palette is not None
        peh = state.palette.entry_hash
        palette_dir = tmp_path / "cache" / "palettes" / peh
        for name in ("colors.yaml", "colors.conf", "colors.gtk.css"):
            assert (palette_dir / name).is_file(), f"missing {name}"
        meta = json.loads((palette_dir / "meta.json").read_text())
        assert meta["hash_algorithm"] == "sha256"
        assert meta["entry_hash"] == peh
        # Real hashes — state must agree with meta.json (no sentinels)
        yaml_hash = meta["artifact_hashes"]["colors.yaml"]
        assert state.palette.artifact_hashes["colors_yaml"] == yaml_hash
        assert state.palette.input_template_hash == meta["input_template_hash"]
        assert not set(state.palette.input_template_hash) == {"0"}
        # meta hashes match on-disk content
        for name in ("colors.yaml", "colors.conf", "colors.gtk.css"):
            assert meta["artifact_hashes"][name] == hash_file(palette_dir / name)
        # effects/icons likewise carry real hashes
        assert state.effects is not None
        assert state.effects.input_catalog_hash != "0" * 64
        assert state.icons is not None
        assert state.icons.source_palette_hash == peh

    def test_seeding_creates_wallpaper_cache(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()

        use_case = _make_use_case(tmp_path, repo, install_spine)
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
        """All consumer symlinks created with correct targets (AC 2)."""
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()

        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        current_dir = tmp_path / "current"
        assert current_dir.is_dir()
        state = repo.saved[0]
        assert state.palette is not None
        assert state.effects is not None
        assert state.icons is not None

        wp_link = current_dir / "wallpaper-DP-1.png"
        assert wp_link.is_symlink()
        assert (
            wp_link.resolve()
            == (
                tmp_path / "cache" / "wallpapers" / _make_wallpaper_hash() / "wallpaper.png"
            ).resolve()
        )

        palette_dir = tmp_path / "cache" / "palettes" / state.palette.entry_hash
        for name in ("colors.conf", "colors.gtk.css", "colors.yaml"):
            link = current_dir / name
            assert link.is_symlink(), f"missing symlink {name}"
            assert link.resolve() == (palette_dir / name).resolve()
        assert (current_dir / "effects").resolve() == (
            tmp_path / "cache" / "effects" / state.effects.entry_hash
        ).resolve()
        assert (current_dir / "icons").resolve() == (
            tmp_path / "cache" / "icons" / state.icons.entry_hash
        ).resolve()

    def test_seeding_raises_when_provisioning_output_missing(self, tmp_path: Path) -> None:
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine=tmp_path / "nonexistent")
        with pytest.raises(RuntimeError, match="default wallpaper not found"):
            use_case.run()

    def test_seeding_raises_when_default_png_missing(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        (install_spine / "wallpapers").mkdir(parents=True)
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)
        with pytest.raises(RuntimeError, match="default wallpaper not found"):
            use_case.run()


class TestSeedMonitorDetection:
    """Monitor names come from the injected IMonitorSource (real detection);
    the legacy DEFAULT_MONITOR is the fallback when unavailable."""

    def test_seeding_uses_injected_monitor_source(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()

        class _FakeMonitorSource:
            def detect_monitors(self) -> list[str]:
                return ["eDP-1", "HDMI-A-1"]

        use_case = _make_use_case(
            tmp_path, repo, install_spine, monitor_source=_FakeMonitorSource()
        )
        use_case.run()

        saved_state = repo.saved[0]
        assert set(saved_state.monitors) == {"eDP-1", "HDMI-A-1"}
        current_dir = tmp_path / "current"
        for name in ("wallpaper-eDP-1.png", "wallpaper-HDMI-A-1.png"):
            link = current_dir / name
            assert link.is_symlink(), f"missing symlink {name}"
            assert (
                link.resolve()
                == (
                    tmp_path / "cache" / "wallpapers" / _make_wallpaper_hash() / "wallpaper.png"
                ).resolve()
            )

    def test_seeding_falls_back_to_dp1_when_source_empty(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()

        class _EmptyMonitorSource:
            def detect_monitors(self) -> list[str]:
                return []

        use_case = _make_use_case(
            tmp_path, repo, install_spine, monitor_source=_EmptyMonitorSource()
        )
        use_case.run()

        saved_state = repo.saved[0]
        assert set(saved_state.monitors) == {"DP-1"}


class TestSeedFailurePolicy:
    """Palette is a hard dependency; effects/icons degrade gracefully."""

    def test_palette_failure_aborts_seeding(self, tmp_path: Path) -> None:
        """No templates → palette cannot seed → loud failure, no state written."""
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine, with_templates=False)
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)
        with pytest.raises(RuntimeError, match="palette seeding failed"):
            use_case.run()
        assert repo.saved == []
        assert not (tmp_path / "history.jsonl").exists()

    def test_csg_crash_aborts_seeding(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine, csg=_FakeCsg(fail=True))
        with pytest.raises(RuntimeError, match="palette seeding failed"):
            use_case.run()
        assert repo.saved == []

    def test_effects_failure_degrades_gracefully(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine, weg=_FakeWeg(fail=True))
        with caplog.at_level(logging.WARNING, logger="runtime.application.seed_cache"):
            use_case.run()
        state = repo.saved[0]
        assert state.palette is not None
        assert state.effects is None
        assert any("effects" in r.message.lower() for r in caplog.records)

    def test_icons_failure_degrades_gracefully(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine, itr=_FakeItr(fail=True))
        with caplog.at_level(logging.WARNING, logger="runtime.application.seed_cache"):
            use_case.run()
        state = repo.saved[0]
        assert state.palette is not None
        assert state.icons is None
        assert any("icon" in r.message.lower() for r in caplog.records)


class TestSeedMutex:
    """Single-flight seeding: concurrent runs serialize via the mutex."""

    def test_second_process_gets_seed_locked(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)
        lock_path = tmp_path / ".seed.lock"
        with FlockSeedMutex(lock_path).hold():
            with pytest.raises(SeedLockedError):
                use_case.run()
        # After release, seeding proceeds
        use_case.run()
        assert len(repo.saved) == 1


class TestReseedAfterCrashedRun:
    """Crash between cache population and save() must not wedge seeding."""

    def test_reseed_reuses_existing_cache_entries(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        # Simulate crash: state lost, cache present
        repo._state = None
        repo.saved.clear()
        use_case.run()

        assert len(repo.saved) == 1
        state = repo.saved[0]
        assert state.palette is not None
        assert state.effects is not None
        assert state.icons is not None


class TestSeedCacheIdempotent:
    """AC 4: Second run is no-op."""

    def test_idempotent_second_run(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)

        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)

        # First run
        use_case.run()
        assert len(repo.saved) == 1

        # Second run - should be no-op
        use_case.run()
        assert len(repo.saved) == 1  # Still only one save


class TestNothingWrittenToInstallSpine:
    """AC 5: Nothing written under install_spine."""

    def test_install_spine_unmodified(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)

        # Record install_spine state before
        before_files = set(install_spine.rglob("*"))

        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        # Install spine unchanged
        after_files = set(install_spine.rglob("*"))
        assert before_files == after_files
