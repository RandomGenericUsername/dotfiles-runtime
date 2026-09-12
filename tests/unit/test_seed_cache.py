"""Unit tests for SeedCacheUseCase and CacheSeeder adapter.

Tests AC 1-5: first-run detection, cache population (including real
meta.json/staging contract), symlink repoint targets, history append,
domain purity, idempotency, single-flight mutex, and failure policy.
"""

from __future__ import annotations

import json
import logging
import os
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
    HistoryLockError,
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
    # Provisioned-machine reality (Story gt-2-2): the AGS consumer-pointer
    # parent exists; the no-mkdir parent guard requires it.
    (install_spine / "config" / "ags").mkdir(parents=True, exist_ok=True)
    # Provisioned-machine reality (add-rofi-app-launcher): the rofi
    # consumer-pointer parent exists (compositor_configs role creates it).
    (install_spine / "config" / "rofi").mkdir(parents=True, exist_ok=True)
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
                "colors.adw.css": "a" * 64,
                "colors.sequences": "b" * 64,
                "colors.rasi": "c" * 64,
            },
            generated_at="2026-01-01T00:00:00Z",
        )
        entry = seeder.load_palette_entry(tmp_path / "cache" / "palettes" / ("b" * 64))
        assert entry.entry_hash == "b" * 64
        assert entry.input_template_hash == "c" * 64
        assert entry.artifact_hashes["colors_yaml"] == "d" * 64
        assert entry.artifact_hashes["colors_conf"] == "e" * 64
        assert entry.artifact_hashes["colors_gtk_css"] == "f" * 64
        assert entry.artifact_hashes["colors_adw_css"] == "a" * 64
        assert entry.artifact_hashes["colors_sequences"] == "b" * 64
        assert entry.artifact_hashes["colors_rasi"] == "c" * 64

    def test_load_palette_entry_raises_on_pre_growth_meta(self, tmp_path: Path) -> None:
        """Defense-in-depth: a pre-growth 3-key meta must NEVER load through
        this path — the schema raises loud (the derive hit-validation
        of AC 8 guarantees old entries never reach it)."""
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
        with pytest.raises(ValueError, match="invalid cache meta"):
            seeder.load_palette_entry(tmp_path / "cache" / "palettes" / ("b" * 64))

    def test_load_palette_entry_names_missing_artifact_field(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        entry_dir = tmp_path / "cache" / "palettes" / ("b" * 64)
        entry_dir.mkdir(parents=True)
        (entry_dir / "meta.json").write_text(
            json.dumps(
                {
                    "hash_algorithm": "sha256",
                    "kind": "palette",
                    "entry_hash": "b" * 64,
                    "source_wallpaper_hash": "a" * 64,
                    "input_template_hash": "c" * 64,
                    "artifact_hashes": {"colors.yaml": "d" * 64},
                    "generated_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="colors.conf"):
            seeder.load_palette_entry(entry_dir)

    def test_load_palette_entry_rejects_cross_kind_meta(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(state_root=tmp_path)
        entry_dir = tmp_path / "cache" / "palettes" / ("b" * 64)
        entry_dir.mkdir(parents=True)
        (entry_dir / "meta.json").write_text(
            json.dumps(
                {
                    "hash_algorithm": "sha256",
                    "kind": "wallpaper",
                    "content_hash": "a" * 64,
                    "source_path": "/img/w.png",
                    "imported_at": "2026-01-01T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="expected kind 'palette'"):
            seeder.load_palette_entry(entry_dir)


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
        with pytest.raises(HistoryLockError):
            seeder.append_history(trigger="seed", wallpaper_hash="a" * 64)

    def test_append_rejects_non_finite_details(self, tmp_path: Path) -> None:
        """R-1: allow_nan=False — never write invalid JSON (NaN/Infinity)."""
        seeder = CacheSeeder(state_root=tmp_path)
        with pytest.raises(ValueError):
            seeder.append_history(
                trigger="prune",
                wallpaper_hash="a" * 64,
                details={"removed": float("nan")},
            )
        # nothing was written
        assert not (tmp_path / "history.jsonl").exists()

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
        """Spec Task 4: cache/palettes/<ph>/ has all 6 artifacts + real-hash meta."""
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()

        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        state = repo.saved[0]
        assert state.palette is not None
        peh = state.palette.entry_hash
        palette_dir = tmp_path / "cache" / "palettes" / peh
        for name in (
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        ):
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
        for name in (
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        ):
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
        for name in (
            "colors.conf",
            "colors.gtk.css",
            "colors.yaml",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        ):
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
    """AD-11 + Epic 4 R2 exception: nothing written under install_spine
    EXCEPT the spec'd consumer pointer paths (gt-2-2: the pointer class
    replaces the single-symlink prose — parents are NEVER created)."""

    def test_install_spine_unmodified_except_r2_symlink(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)

        # Record install_spine state before
        before_files = set(install_spine.rglob("*"))

        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        # Only the ags and rofi pointer symlinks may appear under install_spine —
        # no created parent dirs; the gtk pointers' parents are absent
        # (pre-gt-3-1) so those pointers must not be created either.
        after_files = set(install_spine.rglob("*"))
        new_files = after_files - before_files
        allowed = {
            install_spine / "config" / "ags" / "colors.css",
            install_spine / "config" / "rofi" / "colors.rasi",
        }
        assert new_files == allowed, (
            f"only the spec'd consumer pointer symlink may be written under "
            f"install_spine; got {new_files}"
        )


class TestR2ConsumerSymlink:
    """Epic 4 R2: the seeder points the spine consumer path at current/."""

    def test_seed_creates_r2_symlink_to_current(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        link = install_spine / "config" / "ags" / "colors.css"
        assert link.is_symlink(), "R2 consumer symlink must be created by seed"
        assert os.readlink(link) == str(tmp_path / "current" / "colors.gtk.css"), (
            "R2 symlink must target current/colors.gtk.css"
        )
        rofi_link = install_spine / "config" / "rofi" / "colors.rasi"
        assert rofi_link.is_symlink(), "rofi consumer symlink must be created by seed"
        assert os.readlink(rofi_link) == str(tmp_path / "current" / "colors.rasi"), (
            "rofi symlink must target current/colors.rasi"
        )

    def test_seed_replaces_stale_copy_with_r2_symlink(self, tmp_path: Path) -> None:
        """Upgraded machines (pre-Epic-4 provisioning copies) migrate on
        the next seed: the stale copy is REPLACED, not kept."""
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        stale = install_spine / "config" / "ags" / "colors.css"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("@define-color stale #000000;\n")
        repo = _FakeStateRepo()
        use_case = _make_use_case(tmp_path, repo, install_spine)
        use_case.run()

        assert stale.is_symlink(), "stale copy must be replaced with the R2 symlink"
        assert os.readlink(stale) == str(tmp_path / "current" / "colors.gtk.css")


class TestConsumerPointerSpec:
    """gt-2-2: the generic spec-driven loop (StaticConsumerPathSpec table)
    implements each investigation §3 rule exactly once."""

    AGS = "config/ags/colors.css"
    GTK3 = "config/gtk-3.0/colors.css"
    GTK4 = "config/gtk-4.0/colors.css"
    ROFI = "config/rofi/colors.rasi"

    @staticmethod
    def _provision_current(
        state_root: Path,
        artifacts: tuple[str, ...] = ("colors.gtk.css", "colors.adw.css", "colors.rasi"),
    ) -> None:
        current = state_root / "current"
        current.mkdir(parents=True, exist_ok=True)
        for name in artifacts:
            (current / name).write_text(f"/* {name} */\n")

    def test_all_four_pointers_created_when_parents_exist(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        for d in ("config/ags", "config/gtk-3.0", "config/gtk-4.0", "config/rofi"):
            (install_spine / d).mkdir(parents=True)
        self._provision_current(tmp_path)
        seeder = CacheSeeder(tmp_path)

        created = seeder.repoint_consumer_symlinks(install_spine, "p" * 64)

        current = tmp_path / "current"
        assert [p.name for p in created] == ["colors.css"] * 3 + ["colors.rasi"]
        assert os.readlink(install_spine / self.AGS) == str(current / "colors.gtk.css")
        assert os.readlink(install_spine / self.GTK3) == str(current / "colors.gtk.css")
        assert os.readlink(install_spine / self.GTK4) == str(current / "colors.adw.css")
        assert os.readlink(install_spine / self.ROFI) == str(current / "colors.rasi")

    def test_gtk_pointers_skip_with_warning_when_parents_absent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Pre-gt-3-1: gtk spine dirs don't exist — skip + warn, ags still
        created, no crash, no dirs created (never mkdir into the spine)."""
        install_spine = tmp_path / "install"
        (install_spine / "config" / "ags").mkdir(parents=True)
        self._provision_current(tmp_path)
        seeder = CacheSeeder(tmp_path)

        with caplog.at_level(logging.WARNING, logger="runtime.adapters.seeder"):
            created = seeder.repoint_consumer_symlinks(install_spine, "p" * 64)

        assert created == [install_spine / self.AGS]
        assert os.readlink(install_spine / self.AGS) == str(
            tmp_path / "current" / "colors.gtk.css"
        )
        assert not (install_spine / "config" / "gtk-3.0").exists()
        assert not (install_spine / "config" / "gtk-4.0").exists()
        assert not (install_spine / "config" / "rofi").exists()
        parent_warnings = [r for r in caplog.records if "destination parent missing" in r.message]
        assert len(parent_warnings) == 3

    def test_regular_file_at_dest_replaced_with_symlink(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        (install_spine / "config" / "gtk-3.0").mkdir(parents=True)
        self._provision_current(tmp_path)
        stale = install_spine / self.GTK3
        stale.write_text("@define-color stale #000000;\n")
        seeder = CacheSeeder(tmp_path)

        created = seeder.repoint_consumer_symlinks(install_spine, "p" * 64)

        assert created == [stale]
        assert stale.is_symlink()
        assert os.readlink(stale) == str(tmp_path / "current" / "colors.gtk.css")

    def test_missing_target_skips_only_that_pointer(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Per-pointer guard (not all-or-nothing): missing colors.adw.css
        skips only the gtk-4.0 pointer — ags + gtk-3.0 still created."""
        install_spine = tmp_path / "install"
        for d in ("config/ags", "config/gtk-3.0", "config/gtk-4.0", "config/rofi"):
            (install_spine / d).mkdir(parents=True)
        self._provision_current(tmp_path, artifacts=("colors.gtk.css",))
        seeder = CacheSeeder(tmp_path)

        with caplog.at_level(logging.WARNING, logger="runtime.adapters.seeder"):
            created = seeder.repoint_consumer_symlinks(install_spine, "p" * 64)

        assert created == [install_spine / self.AGS, install_spine / self.GTK3]
        gtk4 = install_spine / self.GTK4
        assert not gtk4.exists() and not gtk4.is_symlink()
        rofi = install_spine / self.ROFI
        assert not rofi.exists() and not rofi.is_symlink()
        assert any("colors.adw.css missing" in r.message for r in caplog.records)
        assert any("colors.rasi missing" in r.message for r in caplog.records)

    def test_null_palette_removes_all_existing_pointers(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        install_spine = tmp_path / "install"
        for d in ("config/ags", "config/gtk-3.0", "config/gtk-4.0", "config/rofi"):
            (install_spine / d).mkdir(parents=True)
        self._provision_current(tmp_path)
        seeder = CacheSeeder(tmp_path)
        seeder.repoint_consumer_symlinks(install_spine, "p" * 64)

        with caplog.at_level(logging.WARNING, logger="runtime.adapters.seeder"):
            created = seeder.repoint_consumer_symlinks(install_spine, None)

        assert created == []
        for rel in (self.AGS, self.GTK3, self.GTK4, self.ROFI):
            dest = install_spine / rel
            assert not dest.exists() and not dest.is_symlink()
        removals = [r for r in caplog.records if "palette layer is null" in r.message]
        assert len(removals) == 4

    def test_null_palette_missing_ok_no_crash(self, tmp_path: Path) -> None:
        """missing_ok: a null palette with NO existing pointers is clean."""
        install_spine = tmp_path / "install"
        (install_spine / "config" / "ags").mkdir(parents=True)
        seeder = CacheSeeder(tmp_path)

        created = seeder.repoint_consumer_symlinks(install_spine, None)

        assert created == []

    def test_rerun_idempotent_no_divergence(self, tmp_path: Path) -> None:
        install_spine = tmp_path / "install"
        for d in ("config/ags", "config/gtk-3.0", "config/gtk-4.0", "config/rofi"):
            (install_spine / d).mkdir(parents=True)
        self._provision_current(tmp_path)
        seeder = CacheSeeder(tmp_path)
        first = seeder.repoint_consumer_symlinks(install_spine, "p" * 64)

        second = seeder.repoint_consumer_symlinks(install_spine, "p" * 64)

        assert second == first
        for rel, target in (
            (self.AGS, "colors.gtk.css"),
            (self.GTK3, "colors.gtk.css"),
            (self.GTK4, "colors.adw.css"),
            (self.ROFI, "colors.rasi"),
        ):
            assert os.readlink(install_spine / rel) == str(tmp_path / "current" / target)

    def test_custom_spec_is_consumed_generically(self, tmp_path: Path) -> None:
        """Adding a consumer = one spec line — the loop consumes ANY spec."""
        from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
        from runtime.domain.models import ConsumerPointer

        class _ExtendedSpec(StaticConsumerPathSpec):
            def consumer_pointers(self) -> tuple[ConsumerPointer, ...]:
                return (
                    ConsumerPointer(path="config/rofi/colors.rasi", target="colors.gtk.css"),
                )

        install_spine = tmp_path / "install"
        (install_spine / "config" / "rofi").mkdir(parents=True)
        self._provision_current(tmp_path)

        created = CacheSeeder(tmp_path, consumer_spec=_ExtendedSpec()).repoint_consumer_symlinks(
            install_spine, "p" * 64
        )

        assert created == [install_spine / "config" / "rofi" / "colors.rasi"]
        assert os.readlink(created[0]) == str(tmp_path / "current" / "colors.gtk.css")


# ═══════════════════════════════════════════════════════════════════
# AC 8 — pre-growth palette entry migration (incomplete-entry eviction)
# ═══════════════════════════════════════════════════════════════════


class TestPreGrowthPaletteEntryMigration:
    """Story gt-2-1 AC 8: an on-disk cache entry holding only the 3 legacy
    artifacts + old-shape meta.json is a cache MISS — evicted (logged) and
    regenerated in full (5 artifacts + 5-key meta) through the normal
    staging pipeline at the SAME <ph> (cache keys unchanged, NFR-4).
    Idempotent: the second pass is a clean hit — no re-eviction."""

    @staticmethod
    def _populate_pre_growth_entry(state_root: Path, install_spine: Path) -> Path:
        """Create a pre-growth partial palette entry at the correct <ph>."""
        from runtime.adapters.hashing import canonical_hash_dir, palette_entry_hash

        templates = install_spine / "config" / "color-scheme-generator" / "templates"
        wh = _make_wallpaper_hash()
        template_hash = canonical_hash_dir(templates)
        ph = palette_entry_hash(wh, template_hash)
        entry = state_root / "cache" / "palettes" / ph
        entry.mkdir(parents=True)
        (entry / "colors.yaml").write_text("colors: []")
        (entry / "colors.conf").write_text("colors {}")
        (entry / "colors.gtk.css").write_text("colors {}")
        CacheSeeder(state_root).write_palette_meta_in(
            entry,
            entry_hash=ph,
            source_wallpaper_hash=wh,
            input_template_hash=template_hash,
            artifact_hashes={  # old shape: 3 keys only (pre-growth)
                "colors.yaml": "d" * 64,
                "colors.conf": "e" * 64,
                "colors.gtk.css": "f" * 64,
            },
            generated_at="2026-01-01T00:00:00Z",
        )
        return entry

    def test_seed_migrates_pre_growth_entry(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """First post-upgrade run: incomplete entry evicted + regenerated;
        current/ gains the two new symlinks on that same run."""
        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        entry = self._populate_pre_growth_entry(tmp_path, install_spine)
        repo = _FakeStateRepo()  # no current.json → seed runs

        use_case = _make_use_case(tmp_path, repo, install_spine)
        with caplog.at_level(logging.INFO, logger="runtime.application.derive"):
            use_case.run()

        assert any("evicting" in r.message for r in caplog.records), (
            "incomplete pre-growth entry eviction must be logged"
        )
        state = repo.saved[0]
        assert state.palette is not None
        peh = state.palette.entry_hash
        assert entry.name == peh  # SAME <ph> — cache keys unchanged (NFR-4)
        palette_dir = tmp_path / "cache" / "palettes" / peh
        meta = json.loads((palette_dir / "meta.json").read_text())
        assert set(meta["artifact_hashes"]) == {
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        }
        for name in meta["artifact_hashes"]:
            assert (palette_dir / name).is_file(), f"missing regenerated artifact {name}"
        # current/ carries the new symlinks
        current_dir = tmp_path / "current"
        palette_dir_resolved = palette_dir.resolve()
        for name in ("colors.adw.css", "colors.sequences", "colors.rasi"):
            link = current_dir / name
            assert link.is_symlink(), f"missing symlink {name}"
            assert link.resolve() == (palette_dir_resolved / name)

    def test_pipeline_migration_is_idempotent(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DerivationPipeline.ensure_palette: incomplete → miss+evict+regen;
        second pass on the regenerated entry → clean hit, no re-eviction."""
        from runtime.application.derive import DerivationPipeline

        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        entry = self._populate_pre_growth_entry(tmp_path, install_spine)
        pipeline = DerivationPipeline(
            state_root=tmp_path,
            seeder=CacheSeeder(tmp_path),
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            install_spine=install_spine,
        )
        with caplog.at_level(logging.INFO, logger="runtime.application.derive"):
            entry1, hit1 = pipeline.ensure_palette(WALLPAPER_PNG, _make_wallpaper_hash())
        assert hit1 is False
        assert entry1.entry_hash == entry.name
        meta1 = json.loads((entry / "meta.json").read_text())
        assert set(meta1["artifact_hashes"]) == {
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        }
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="runtime.application.derive"):
            entry2, hit2 = pipeline.ensure_palette(WALLPAPER_PNG, _make_wallpaper_hash())
        assert hit2 is True
        assert not any("evicting" in r.message for r in caplog.records)
        assert json.loads((entry / "meta.json").read_text()) == meta1  # untouched
        assert entry2.entry_hash == entry1.entry_hash

    def test_incomplete_entry_eviction_never_leaves_half_evicted_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rmtree with ignore_errors=False: an OSError during eviction
        propagates (caller failure policy applies) — never a silent partial."""
        from runtime.application.derive import DerivationPipeline, ensure_palette_entry_complete

        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        entry = self._populate_pre_growth_entry(tmp_path, install_spine)
        pipeline = DerivationPipeline(
            state_root=tmp_path,
            seeder=CacheSeeder(tmp_path),
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            install_spine=install_spine,
        )

        def _boom(target: object, **kwargs: object) -> None:
            raise OSError("eviction failed")

        monkeypatch.setattr(
            "runtime.application.derive.shutil.rmtree",
            _boom,
        )
        with pytest.raises(OSError, match="eviction failed"):
            pipeline.ensure_palette(WALLPAPER_PNG, _make_wallpaper_hash())
        # The incomplete entry dir still exists (half-evicted state prevented)
        assert entry.exists()
        # And the guard itself surfaces the same failure loudly
        with pytest.raises(OSError, match="eviction failed"):
            ensure_palette_entry_complete(entry, CacheSeeder(tmp_path))

    def test_five_artifact_entry_is_migrated_to_six(self, tmp_path: Path) -> None:
        """add-rofi-app-launcher migration: an entry carrying the five
        legacy artifacts + five-key meta (the gt-2-1 growth state) is
        treated as incomplete — evicted once, regenerated as six through
        the normal staging path at the SAME <ph>."""
        from runtime.adapters.hashing import canonical_hash_dir, palette_entry_hash
        from runtime.application.derive import (
            DerivationPipeline,
            ensure_palette_entry_complete,
        )

        install_spine = tmp_path / "install"
        _setup_install_spine(install_spine)
        templates = install_spine / "config" / "color-scheme-generator" / "templates"
        wh = _make_wallpaper_hash()
        template_hash = canonical_hash_dir(templates)
        ph = palette_entry_hash(wh, template_hash)
        entry = tmp_path / "cache" / "palettes" / ph
        entry.mkdir(parents=True)
        for name in (
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
        ):
            (entry / name).write_text(name)
        CacheSeeder(tmp_path).write_palette_meta_in(
            entry,
            entry_hash=ph,
            source_wallpaper_hash=wh,
            input_template_hash=template_hash,
            artifact_hashes={
                "colors.yaml": "a" * 64,
                "colors.conf": "b" * 64,
                "colors.gtk.css": "c" * 64,
                "colors.adw.css": "d" * 64,
                "colors.sequences": "e" * 64,
            },
            generated_at="2026-01-01T00:00:00Z",
        )

        assert ensure_palette_entry_complete(entry, CacheSeeder(tmp_path)) is False
        assert not entry.exists(), "five-artifact legacy entry must be evicted"

        entry2, hit = DerivationPipeline(
            state_root=tmp_path,
            seeder=CacheSeeder(tmp_path),
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            install_spine=install_spine,
        ).ensure_palette(WALLPAPER_PNG, wh)
        assert hit is False
        assert entry2.entry_hash == ph
        assert (entry / "colors.rasi").is_file()
        meta = json.loads((entry / "meta.json").read_text())
        assert set(meta["artifact_hashes"]) == {
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        }
        # Second pass is a clean hit — no re-eviction.
        entry3, hit3 = DerivationPipeline(
            state_root=tmp_path,
            seeder=CacheSeeder(tmp_path),
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            install_spine=install_spine,
        ).ensure_palette(WALLPAPER_PNG, wh)
        assert hit3 is True
        assert entry3.entry_hash == ph


class TestRepointCurrentSymlinksPaletteArtifactSkip:
    """Seeder-level defense-in-depth (gt-2-1): per-artifact exists-or-symlink
    check with skip+warn per missing artifact — now over the 6-name set."""

    def test_missing_palette_artifacts_skip_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        seeder = CacheSeeder(tmp_path)
        ph = "b" * 64
        entry = tmp_path / "cache" / "palettes" / ph
        entry.mkdir(parents=True)
        (entry / "colors.conf").write_text("conf only")
        with caplog.at_level(logging.WARNING, logger="runtime.adapters.seeder"):
            created = seeder.repoint_current_symlinks(
                wallpaper_target=tmp_path / "wall.png",
                monitor_names=["DP-1"],
                palette_entry_hash=ph,
            )
        names = [p.name for p in created]
        assert "colors.conf" in names
        for missing in (
            "colors.gtk.css",
            "colors.yaml",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        ):
            assert missing not in names, f"{missing} must be skipped, never dangling"
        assert sum("palette artifact missing" in r.message for r in caplog.records) == 5

    def test_all_six_artifacts_repointed(self, tmp_path: Path) -> None:
        seeder = CacheSeeder(tmp_path)
        ph = "b" * 64
        entry = tmp_path / "cache" / "palettes" / ph
        entry.mkdir(parents=True)
        for name in (
            "colors.conf",
            "colors.gtk.css",
            "colors.yaml",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        ):
            (entry / name).write_text(name)
        created = seeder.repoint_current_symlinks(
            wallpaper_target=tmp_path / "wall.png",
            monitor_names=["DP-1"],
            palette_entry_hash=ph,
        )
        names = {p.name for p in created}
        assert names == {
            "wallpaper-DP-1.png",
            "wallpaper.png",
            "colors.conf",
            "colors.gtk.css",
            "colors.yaml",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
        }
