"""Unit tests for JsonStateRepository adapter.

Tests projection round-trip (not full equality), atomicity, schema validation,
absent handling, symlink rejection, and error propagation.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.adapters.json_state_repository import SENTINEL_HASH, JsonStateRepository
from runtime.domain.models import (
    BackendType,
    DesktopState,
    EffectsEntry,
    FitMode,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteEntry,
    WallpaperEntry,
)


def _now_z() -> str:
    """Strict ISO-8601 UTC timestamp ending with Z."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _make_wallpaper_hash() -> str:
    """Deterministic wallpaper hash for tests."""
    return "a" * 64


def _make_state(
    *,
    wallpaper_hash: str | None = None,
    monitors: dict[str, MonitorWallpaperConfig] | None = None,
    palette: PaletteEntry | None = None,
    effects: EffectsEntry | None = None,
    icons: IconsEntry | None = None,
    applied_at: str | None = None,
) -> DesktopState:
    """Create a test DesktopState with sensible defaults."""
    wh = wallpaper_hash or _make_wallpaper_hash()
    if monitors is None:
        monitors = {
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wh,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            ),
        }
    if applied_at is None:
        applied_at = _now_z()

    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wh,
            source_path="/test/wallpaper.png",
            imported_at=applied_at,
        ),
        monitors=monitors,
        palette=palette,
        effects=effects,
        icons=icons,
        applied_at=applied_at,
    )


def _make_palette(wallpaper_hash: str) -> PaletteEntry:
    return PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash="b" * 64,
        source_wallpaper_hash=wallpaper_hash,
        input_template_hash="c" * 64,
        artifact_hashes={
            "colors_yaml": "d" * 64,
            "colors_conf": "e" * 64,
            "colors_gtk_css": "f" * 64,
        },
        generated_at=_now_z(),
    )


def _make_effects(wallpaper_hash: str) -> EffectsEntry:
    return EffectsEntry(
        hash_algorithm="sha256",
        kind="effects",
        entry_hash="1" * 64,
        source_wallpaper_hash=wallpaper_hash,
        input_catalog_hash="2" * 64,
        artifact_hashes={"effect1.png": "3" * 64},
        generated_at=_now_z(),
    )


def _make_icons(palette_hash: str) -> IconsEntry:
    return IconsEntry(
        hash_algorithm="sha256",
        kind="icons",
        entry_hash="4" * 64,
        source_palette_hash=palette_hash,
        input_templates_hash="5" * 64,
        input_mappings_hash="6" * 64,
        artifact_hashes={"icon1.svg": "7" * 64},
        generated_at=_now_z(),
    )


class TestSaveAndLoadProjectionRoundtrip:
    """AC1: Projection round-trip for palette/effects/icons."""

    def test_save_and_load_projection_roundtrip(self, tmp_path: Path) -> None:
        wh = _make_wallpaper_hash()
        palette = _make_palette(wh)
        state = _make_state(
            monitors={
                "DP-1": MonitorWallpaperConfig(
                    backend=BackendType.hyprpaper,
                    source_hash=wh,
                    fit_mode=FitMode.cover,
                    mpv_options=None,
                    ipc_socket=None,
                ),
                "HDMI-1": MonitorWallpaperConfig(
                    backend=BackendType.mpvpaper,
                    source_hash=wh,
                    fit_mode=FitMode.fill,
                    mpv_options="--no-audio",
                    ipc_socket="/run/mpv.sock",
                ),
            },
            palette=palette,
        )
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)
        loaded = repo.load_current()

        assert loaded is not None
        # Wallpaper exact
        assert loaded.wallpaper.content_hash == state.wallpaper.content_hash
        assert loaded.wallpaper.source_path == state.wallpaper.source_path
        assert loaded.wallpaper.imported_at == state.wallpaper.imported_at
        # Monitors exact
        assert loaded.monitors.keys() == state.monitors.keys()
        for name in state.monitors:
            assert loaded.monitors[name].backend == state.monitors[name].backend
            assert loaded.monitors[name].source_hash == state.monitors[name].source_hash
            assert loaded.monitors[name].fit_mode == state.monitors[name].fit_mode
            assert loaded.monitors[name].mpv_options == state.monitors[name].mpv_options
            assert loaded.monitors[name].ipc_socket == state.monitors[name].ipc_socket
        # Palette projection: entry_hash + generated_at equal, sentinel for input hashes
        assert loaded.palette is not None
        assert loaded.palette.entry_hash == palette.entry_hash
        assert loaded.palette.generated_at == palette.generated_at
        assert loaded.palette.input_template_hash == SENTINEL_HASH
        # artifact_hashes are reconstructed with sentinel values (projection reconstruction)
        assert loaded.palette.artifact_hashes == {
            "colors_yaml": SENTINEL_HASH,
            "colors_conf": SENTINEL_HASH,
            "colors_gtk_css": SENTINEL_HASH,
        }

    def test_roundtrip_with_mpvpaper_monitor(self, tmp_path: Path) -> None:
        wh = _make_wallpaper_hash()
        state = _make_state(
            monitors={
                "DP-1": MonitorWallpaperConfig(
                    backend=BackendType.mpvpaper,
                    source_hash=wh,
                    fit_mode=FitMode.cover,
                    mpv_options="--no-audio",
                    ipc_socket="/run/mpv.sock",
                ),
            },
        )
        repo = JsonStateRepository(state_root=tmp_path)
        repo.save(state)
        loaded = repo.load_current()

        assert loaded is not None
        cfg = loaded.monitors["DP-1"]
        assert cfg.backend == BackendType.mpvpaper
        assert cfg.mpv_options == "--no-audio"
        assert cfg.ipc_socket == "/run/mpv.sock"


class TestAtomicity:
    """AC1: Atomic tmp + os.replace."""

    def test_save_is_atomic_tmp_replace(self, tmp_path: Path) -> None:
        state = _make_state()
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)

        # No tmp files left
        assert list(tmp_path.glob("current.json.tmp.*")) == []

        # Data is valid JSON with sort_keys and indent=2
        raw = (tmp_path / "current.json").read_text()
        assert raw.endswith("\n")
        data = json.loads(raw)
        assert data["schema_version"] == 2
        # Verify indent (first line after schema_version should have leading spaces)
        lines = raw.split("\n")
        assert lines[0] == "{"
        assert lines[1].startswith('  "applied_at"')

    def test_current_json_is_deterministic(self, tmp_path: Path) -> None:
        state = _make_state()
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)
        first = (tmp_path / "current.json").read_bytes()

        repo.save(state)
        second = (tmp_path / "current.json").read_bytes()

        assert first == second

    def test_save_creates_state_root_if_missing(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "state"
        state = _make_state()
        repo = JsonStateRepository(state_root=nested)

        repo.save(state)

        assert (nested / "current.json").exists()

    def test_save_overwrites_atomically(self, tmp_path: Path) -> None:
        state1 = _make_state(wallpaper_hash="a" * 64)
        state2 = _make_state(wallpaper_hash="b" * 64)
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state1)
        repo.save(state2)

        loaded = repo.load_current()
        assert loaded is not None
        assert loaded.wallpaper.content_hash == "b" * 64
        assert list(tmp_path.glob("current.json.tmp.*")) == []


class TestLoadAbsent:
    """AC3: Absent store handling."""

    def test_load_returns_none_on_absent(self, tmp_path: Path) -> None:
        repo = JsonStateRepository(state_root=tmp_path)
        assert repo.load_current() is None
        # No file created
        assert not (tmp_path / "current.json").exists()

    def test_load_rejects_symlink_current_json(self, tmp_path: Path) -> None:
        target = tmp_path / "real.json"
        target.write_text('{"schema_version": 2}')
        link = tmp_path / "current.json"
        link.symlink_to(target)

        repo = JsonStateRepository(state_root=tmp_path)
        with pytest.raises(ValueError, match="symlink"):
            repo.load_current()

    def test_load_distinguishes_is_directory(self, tmp_path: Path) -> None:
        # Create current.json as a directory
        (tmp_path / "current.json").mkdir()

        repo = JsonStateRepository(state_root=tmp_path)
        with pytest.raises(ValueError, match="not a regular file"):
            repo.load_current()


class TestSchemaValidation:
    """AC2: Schema version guard."""

    def test_load_raises_on_schema_mismatch(self, tmp_path: Path) -> None:
        data = {
            "schema_version": 1,
            "wallpaper": {"hash": "a" * 64, "source_path": "", "applied_at": _now_z()},
        }
        (tmp_path / "current.json").write_text(json.dumps(data))

        repo = JsonStateRepository(state_root=tmp_path)
        with pytest.raises(ValueError, match="unsupported schema_version.*expected 2"):
            repo.load_current()

    def test_load_raises_on_schema_missing(self, tmp_path: Path) -> None:
        data = {"wallpaper": {"hash": "a" * 64, "source_path": "", "applied_at": _now_z()}}
        (tmp_path / "current.json").write_text(json.dumps(data))

        repo = JsonStateRepository(state_root=tmp_path)
        with pytest.raises(ValueError, match="unsupported schema_version.*expected 2"):
            repo.load_current()

    def test_load_tolerates_monitors_absent_v1_but_schema_first(self, tmp_path: Path) -> None:
        # schema_version 1, no monitors — should raise schema error, not tolerate
        data = {
            "schema_version": 1,
            "wallpaper": {"hash": "a" * 64, "source_path": "", "applied_at": _now_z()},
        }
        (tmp_path / "current.json").write_text(json.dumps(data))

        repo = JsonStateRepository(state_root=tmp_path)
        with pytest.raises(ValueError, match="unsupported schema_version"):
            repo.load_current()

    def test_load_tolerates_monitors_absent_v2(self, tmp_path: Path) -> None:
        # schema_version 2, no monitors — should tolerate and return monitors={}
        wh = _make_wallpaper_hash()
        data = {
            "schema_version": 2,
            "wallpaper": {"hash": wh, "source_path": "", "applied_at": _now_z()},
            "applied_at": _now_z(),
        }
        (tmp_path / "current.json").write_text(json.dumps(data))

        repo = JsonStateRepository(state_root=tmp_path)
        loaded = repo.load_current()

        assert loaded is not None
        assert loaded.monitors == {}


class TestNullPaletteEffectsIcons:
    """Null palette/effects/icons round-trip."""

    def test_roundtrip_with_null_palette_effects_icons(self, tmp_path: Path) -> None:
        state = _make_state(palette=None, effects=None, icons=None)
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)
        loaded = repo.load_current()

        assert loaded is not None
        assert loaded.palette is None
        assert loaded.effects is None
        assert loaded.icons is None

        # JSON has null values
        data = json.loads((tmp_path / "current.json").read_text())
        assert data["palette"] is None
        assert data["effects"] is None
        assert data["icons"] is None


class TestErrorPropagation:
    """Error handling — propagate typed errors."""

    def test_load_raises_on_invalid_json(self, tmp_path: Path) -> None:
        (tmp_path / "current.json").write_text("{invalid")

        repo = JsonStateRepository(state_root=tmp_path)
        with pytest.raises(ValueError, match="not valid JSON"):
            repo.load_current()

    def test_load_raises_on_missing_required_field(self, tmp_path: Path) -> None:
        data = {"schema_version": 2}  # missing wallpaper
        (tmp_path / "current.json").write_text(json.dumps(data))

        repo = JsonStateRepository(state_root=tmp_path)
        with pytest.raises(ValueError, match="missing required field.*'wallpaper'"):
            repo.load_current()


class TestValidation:
    """Input validation for save."""

    def test_save_validates_hex64(self, tmp_path: Path) -> None:
        state = _make_state(wallpaper_hash="nothex")
        repo = JsonStateRepository(state_root=tmp_path)

        with pytest.raises(ValueError, match="64-char lowercase hex"):
            repo.save(state)
        # No file created
        assert not (tmp_path / "current.json").exists()

    def test_save_rejects_non_z_timestamp(self, tmp_path: Path) -> None:
        state = _make_state(applied_at="2026-08-30T00:00:00+00:00")
        repo = JsonStateRepository(state_root=tmp_path)

        with pytest.raises(ValueError, match="ISO-8601 UTC ending with 'Z'"):
            repo.save(state)

    def test_save_never_writes_history_jsonl(self, tmp_path: Path) -> None:
        state = _make_state()
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)

        assert not (tmp_path / "history.jsonl").exists()

    def test_init_rejects_null_byte(self) -> None:
        with pytest.raises(ValueError, match="null bytes"):
            JsonStateRepository(state_root=Path("/tmp/\x00"))

    def test_init_rejects_traversal(self) -> None:
        with pytest.raises(ValueError, match="traversal"):
            JsonStateRepository(state_root=Path("/tmp/../etc"))


class TestDesktopStateEquality:
    """Projection equality for reconstructed DesktopState."""

    def test_desktopstate_frozen_equality(self, tmp_path: Path) -> None:
        wh = _make_wallpaper_hash()
        state = _make_state(wallpaper_hash=wh)
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)
        loaded1 = repo.load_current()
        loaded2 = repo.load_current()

        assert loaded1 is not None
        assert loaded2 is not None
        # Projection fields should be equal
        assert loaded1.wallpaper == loaded2.wallpaper
        assert loaded1.monitors == loaded2.monitors
        assert loaded1.palette == loaded2.palette
        assert loaded1.effects == loaded2.effects
        assert loaded1.icons == loaded2.icons
        assert loaded1.applied_at == loaded2.applied_at


class TestXdgFallback:
    """XDG_STATE_HOME empty string fallback."""

    def test_xdg_empty_string_fallback(self, tmp_path: Path) -> None:
        # Mock XDG_STATE_HOME as empty string
        with patch.dict(os.environ, {"XDG_STATE_HOME": ""}):
            repo = JsonStateRepository()
            # Should resolve to ~/.local/state/dotfiles
            assert "dotfiles" in str(repo.state_root)

    def test_constructor_injection(self, tmp_path: Path) -> None:
        repo = JsonStateRepository(state_root=tmp_path)
        assert repo.state_root == tmp_path
        assert repo._path == tmp_path / "current.json"

    def test_constructor_custom_current_json(self, tmp_path: Path) -> None:
        custom = tmp_path / "custom.json"
        repo = JsonStateRepository(state_root=tmp_path, current_json=custom)
        assert repo._path == custom
