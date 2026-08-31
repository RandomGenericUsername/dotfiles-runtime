"""Integration tests for JsonStateRepository — real filesystem.

Exercises filesystem authority: save → load → assert current.json authoritative.
Marked integration for pytest -k filtering.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    WallpaperEntry,
)

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _make_state(tmp_path: Path) -> DesktopState:
    """Create a test state using real wallpaper fixture."""
    wallpaper_path = FIXTURES / "wallpaper.png"
    wh = hash_file(wallpaper_path)
    applied = _now_z()

    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wh,
            source_path=str(wallpaper_path),
            imported_at=applied,
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
        applied_at=applied,
    )


class TestIntegrationSaveAndLoad:
    """Real filesystem save/load cycle."""

    def test_save_then_load_is_authoritative(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)
        loaded = repo.load_current()

        assert loaded is not None
        assert loaded.wallpaper.content_hash == state.wallpaper.content_hash
        assert loaded.wallpaper.source_path == state.wallpaper.source_path

        # current.json raw JSON wallpaper.hash matches
        raw = json.loads((tmp_path / "current.json").read_text())
        assert raw["wallpaper"]["hash"] == state.wallpaper.content_hash

    def test_history_jsonl_not_touched(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)

        assert not (tmp_path / "history.jsonl").exists()

    def test_deterministic_bytes(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        repo = JsonStateRepository(state_root=tmp_path)

        repo.save(state)
        first = (tmp_path / "current.json").read_bytes()

        repo.save(state)
        second = (tmp_path / "current.json").read_bytes()

        assert first == second

    def test_concurrent_saves_no_corruption(self, tmp_path: Path) -> None:
        """Simulate rapid sequential saves — no partial interleaving."""
        repo = JsonStateRepository(state_root=tmp_path)
        wh = "a" * 64

        for i in range(10):
            state = DesktopState(
                schema_version=2,
                wallpaper=WallpaperEntry(
                    hash_algorithm="sha256",
                    kind="wallpaper",
                    content_hash=wh,
                    source_path=f"/test/{i}.png",
                    imported_at=_now_z(),
                ),
                monitors={},
                palette=None,
                effects=None,
                icons=None,
                applied_at=_now_z(),
            )
            repo.save(state)

        loaded = repo.load_current()
        assert loaded is not None
        assert loaded.wallpaper.source_path == "/test/9.png"
        assert list(tmp_path.glob("current.json.tmp.*")) == []
