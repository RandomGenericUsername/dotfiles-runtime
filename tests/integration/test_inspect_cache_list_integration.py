"""Integration tests for ``inspect cache list`` — real writer + fresh
reader (Story 3.4).

rt-3.1 lesson: integration realism — entries are written through the
real ``populate_via_staging`` (staging-dir + atomic rename), then read
back by a FRESH ``InspectCacheUseCase`` on the same state_root, so the
restart-survival path is exercised, not a fake. Covers multi-layer
round-trip (AC 1), diverged current/ isolation + absent current.json
(AC 4), and empty-cache cleanliness (AC 3).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from runtime.adapters.cache import cache_entry_path, populate_via_staging
from runtime.application.inspect import InspectCacheUseCase

_LAYERS = ("wallpapers", "palettes", "effects", "icons")


def _populate(state_root: Path, layer: str, entry_hash: str) -> None:
    target = cache_entry_path(state_root, layer, entry_hash)

    def _fill(staging: Path) -> None:
        (staging / "artifact.bin").write_bytes(b"data-" + entry_hash.encode())
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": "sha256", "entry_hash": entry_hash}),
            encoding="utf-8",
        )

    assert populate_via_staging(target, _fill) is True


class TestWriterReaderRoundTrip:
    """AC 1 — real populate_via_staging writes are listed by the reader."""

    def test_all_four_layers_round_trip(self, tmp_path: Path) -> None:
        hashes = {
            "wallpapers": ("a" * 64, "b" * 64),
            "palettes": ("c" * 64,),
            "effects": ("e" * 64, "f" * 64),
            "icons": ("d" * 64,),
        }
        for layer, entries in hashes.items():
            for h in entries:
                _populate(tmp_path, layer, h)

        result = InspectCacheUseCase(tmp_path).run()

        assert list(result.layers.keys()) == list(_LAYERS)
        assert result.layers["wallpapers"] == ("a" * 64, "b" * 64)
        assert result.layers["palettes"] == ("c" * 64,)
        assert result.layers["effects"] == ("e" * 64, "f" * 64)
        assert result.layers["icons"] == ("d" * 64,)
        assert result.total == 6
        assert result.counts["wallpapers"] == 2

    def test_fresh_reader_after_restart(self, tmp_path: Path) -> None:
        _populate(tmp_path, "wallpapers", "a" * 64)
        _populate(tmp_path, "icons", "d" * 64)
        first = InspectCacheUseCase(tmp_path).run()
        second = InspectCacheUseCase(tmp_path).run()
        assert first.layers == second.layers
        assert first.total == second.total == 2


class TestCacheIsolation:
    """AC 4 — live current/ divergence + absent current.json never affect output."""

    def test_diverged_current_tree_ignored(self, tmp_path: Path) -> None:
        _populate(tmp_path, "wallpapers", "a" * 64)
        current = tmp_path / "current"
        current.mkdir()
        os.symlink("/elsewhere/wallpaper.png", current / "wallpaper-DP-1.png")
        (tmp_path / "current.json").write_text(json.dumps({"stale": True}))
        result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["wallpapers"] == ("a" * 64,)
        assert result.total == 1

    def test_absent_current_json_does_not_affect_output(
        self, tmp_path: Path
    ) -> None:
        _populate(tmp_path, "palettes", "c" * 64)
        assert not (tmp_path / "current.json").exists()
        result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["palettes"] == ("c" * 64,)
        assert result.total == 1

    def test_empty_state_root_reads_clean(self, tmp_path: Path) -> None:
        result = InspectCacheUseCase(tmp_path).run()
        assert result.total == 0
        assert not (tmp_path / "cache").exists()
