"""Unit tests for InspectCacheUseCase (Story 3.4, rt-3-4).

Covers: multi-layer listing in canonical pipeline order with per-layer
sorted hashes (AC 1, AR-2), absent cache / absent layer clean paths
(AC 3), noise tolerance incl. staging/files/non-hex/symlinks (AC 5,
AD-9), meta.json never required, read-only invariant (AC 4), and
deterministic canonical order.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from runtime.application.inspect import InspectCacheResult, InspectCacheUseCase

_LAYERS = ("wallpapers", "palettes", "effects", "icons")


def _make_entry(state_root: Path, layer: str, entry_hash: str) -> Path:
    path = state_root / "cache" / layer / entry_hash
    path.mkdir(parents=True, exist_ok=True)
    return path


def _snapshot(state_root: Path) -> dict[str, bytes | str]:
    """Snapshot every file's bytes + dir listing under state_root."""
    snap: dict[str, bytes | str] = {}
    if not state_root.exists():
        return snap
    for p in sorted(state_root.rglob("*")):
        rel = str(p.relative_to(state_root))
        if p.is_symlink():
            snap[rel] = f"symlink->{os.readlink(p)}"
        elif p.is_file():
            snap[rel] = p.read_bytes()
        else:
            snap[rel] = "dir"
    return snap


class TestInspectCacheListing:
    """AC 1 — per-layer listing by hash, canonical order, sorted hashes."""

    def test_multi_layer_lists_canonical_order_sorted(self, tmp_path: Path) -> None:
        # Intentionally unsorted creation order; hashes unsorted per layer.
        _make_entry(tmp_path, "icons", "d" * 64)
        _make_entry(tmp_path, "wallpapers", "b" * 64)
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        _make_entry(tmp_path, "palettes", "c" * 64)
        _make_entry(tmp_path, "effects", "e" * 64)
        _make_entry(tmp_path, "effects", "f" * 64)

        result = InspectCacheUseCase(tmp_path).run()

        assert isinstance(result, InspectCacheResult)
        assert list(result.layers.keys()) == list(_LAYERS)
        assert result.layers["wallpapers"] == ("a" * 64, "b" * 64)
        assert result.layers["palettes"] == ("c" * 64,)
        assert result.layers["effects"] == ("e" * 64, "f" * 64)
        assert result.layers["icons"] == ("d" * 64,)
        assert result.counts == {
            "wallpapers": 2,
            "palettes": 1,
            "effects": 2,
            "icons": 1,
        }
        assert result.total == 6

    def test_result_is_frozen(self, tmp_path: Path) -> None:
        result = InspectCacheUseCase(tmp_path).run()
        with pytest.raises(AttributeError):
            result.total = 99  # type: ignore[misc]

    def test_deterministic_repeated_runs(self, tmp_path: Path) -> None:
        _make_entry(tmp_path, "wallpapers", "b" * 64)
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        first = InspectCacheUseCase(tmp_path).run()
        second = InspectCacheUseCase(tmp_path).run()
        assert first.layers == second.layers
        assert first.counts == second.counts
        assert first.total == second.total


class TestInspectCacheEmpty:
    """AC 3 — absent/empty cache is clean (exit 0 upstream), never an error."""

    def test_absent_cache_returns_all_empty(self, tmp_path: Path) -> None:
        result = InspectCacheUseCase(tmp_path).run()
        assert result.layers == {layer: () for layer in _LAYERS}
        assert result.counts == {layer: 0 for layer in _LAYERS}
        assert result.total == 0

    def test_absent_cache_needs_no_current_json(self, tmp_path: Path) -> None:
        assert not (tmp_path / "current.json").exists()
        result = InspectCacheUseCase(tmp_path).run()
        assert result.total == 0

    def test_cache_readable_without_current_json(self, tmp_path: Path) -> None:
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        assert not (tmp_path / "current.json").exists()
        result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["wallpapers"] == ("a" * 64,)
        assert result.total == 1

    def test_present_but_empty_returns_all_empty(self, tmp_path: Path) -> None:
        (tmp_path / "cache").mkdir()
        result = InspectCacheUseCase(tmp_path).run()
        assert result.total == 0

    def test_absent_single_layer_others_intact(self, tmp_path: Path) -> None:
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        _make_entry(tmp_path, "icons", "d" * 64)
        result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["wallpapers"] == ("a" * 64,)
        assert result.layers["palettes"] == ()
        assert result.layers["effects"] == ()
        assert result.layers["icons"] == ("d" * 64,)
        assert result.total == 2

    def test_empty_layer_dirs_return_empty(self, tmp_path: Path) -> None:
        for layer in _LAYERS:
            (tmp_path / "cache" / layer).mkdir(parents=True)
        result = InspectCacheUseCase(tmp_path).run()
        assert result.total == 0

    def test_inaccessible_cache_propagates_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "cache").mkdir()

        def fail_stat(*args: object, **kwargs: object) -> object:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "stat", fail_stat)
        with pytest.raises(OSError, match="permission denied"):
            InspectCacheUseCase(tmp_path).run()

    def test_inaccessible_layer_propagates_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        original_open = os.open

        def fail_layer_open(path: object, *args: object, **kwargs: object) -> int:
            if Path(path) == tmp_path / "cache" / "wallpapers":
                raise OSError("permission denied")
            return original_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", fail_layer_open)
        with pytest.raises(OSError, match="permission denied"):
            InspectCacheUseCase(tmp_path).run()


class TestInspectCacheNoise:
    """AC 5 — staging/files/non-hex/symlinks tolerated, never loud."""

    def test_staging_dirs_ignored_silently(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        (tmp_path / "cache" / ".staging-1234-abcdef").mkdir(parents=True)
        (tmp_path / "cache" / ".staging-9999-zzzz").mkdir(parents=True)
        with caplog.at_level(logging.WARNING):
            result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["wallpapers"] == ("a" * 64,)
        assert result.total == 1
        assert caplog.messages == []

    def test_plain_files_ignored(self, tmp_path: Path) -> None:
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        (tmp_path / "cache" / "squatter.txt").write_text("junk", encoding="utf-8")
        (tmp_path / "cache" / "wallpapers" / "notes.txt").write_text(
            "junk", encoding="utf-8"
        )
        result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["wallpapers"] == ("a" * 64,)
        assert result.total == 1

    def test_non_hex_dir_ignored_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _make_entry(tmp_path, "palettes", "b" * 64)
        (tmp_path / "cache" / "palettes" / "not-a-hash").mkdir()
        with caplog.at_level(logging.WARNING):
            result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["palettes"] == ("b" * 64,)
        assert result.total == 1
        assert "cache: skipping non-entry dir palettes/not-a-hash" in caplog.messages

    def test_symlinked_cache_root_raises(self, tmp_path: Path) -> None:
        real = tmp_path / "real-cache"
        (real / "wallpapers" / ("a" * 64)).mkdir(parents=True)
        os.symlink(real, tmp_path / "cache")
        with pytest.raises(ValueError, match="[Ss]ymlink"):
            InspectCacheUseCase(tmp_path).run()

    def test_symlinked_layer_skipped_never_followed(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        (outside / ("f" * 64)).mkdir(parents=True)
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        os.symlink(outside, tmp_path / "cache" / "wallpapers" / "link-layer")
        # Symlinked layer dir itself (icons -> outside).
        os.symlink(outside, tmp_path / "cache" / "icons")
        result = InspectCacheUseCase(tmp_path).run()
        # The symlinked entry name must never appear as a hash.
        assert all("link-layer" not in e for e in result.layers["wallpapers"])
        assert result.layers["icons"] == ()
        assert ("f" * 64) not in result.layers["wallpapers"]
        assert ("f" * 64) not in result.layers["icons"]

    def test_symlinked_entry_skipped_never_traversed(self, tmp_path: Path) -> None:
        target = tmp_path / "target-entry"
        (target / "wallpaper.png").mkdir(parents=True, exist_ok=True)
        # Make target a dir with content; symlink it in as an entry name.
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        os.symlink(target, tmp_path / "cache" / "wallpapers" / ("b" * 64))
        result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["wallpapers"] == ("a" * 64,)

    def test_layer_replacement_with_symlink_cannot_escape_cache(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside"
        (outside / ("f" * 64)).mkdir(parents=True)
        _make_entry(tmp_path, "wallpapers", "a" * 64)
        layer = tmp_path / "cache" / "icons"
        layer.mkdir()
        layer.rmdir()
        layer.symlink_to(outside, target_is_directory=True)

        result = InspectCacheUseCase(tmp_path).run()

        assert result.layers["icons"] == ()
        assert ("f" * 64) not in result.layers["icons"]

    def test_meta_json_never_required(self, tmp_path: Path) -> None:
        entry = _make_entry(tmp_path, "effects", "e" * 64)
        assert not (entry / "meta.json").exists()
        result = InspectCacheUseCase(tmp_path).run()
        assert result.layers["effects"] == ("e" * 64,)


class TestInspectCacheReadOnly:
    """AC 4 — the reader mutates nothing (mirror rt-3.2/rt-3.3 style)."""

    def test_absent_cache_creates_nothing(self, tmp_path: Path) -> None:
        before = {p.name for p in tmp_path.iterdir()}
        assert InspectCacheUseCase(tmp_path).run().total == 0
        assert {p.name for p in tmp_path.iterdir()} == before
        assert not (tmp_path / "cache").exists()
        assert not (tmp_path / "current.json").exists()
        assert not (tmp_path / "history.jsonl").exists()

    def test_existing_tree_bytes_identical(self, tmp_path: Path) -> None:
        entry = _make_entry(tmp_path, "wallpapers", "a" * 64)
        (entry / "wallpaper.png").write_bytes(b"png-bytes")
        (entry / "meta.json").write_text('{"hash_algorithm": "sha256"}', encoding="utf-8")
        before = _snapshot(tmp_path)
        InspectCacheUseCase(tmp_path).run()
        InspectCacheUseCase(tmp_path).run()
        assert _snapshot(tmp_path) == before
        assert not (tmp_path / "current.json").exists()
        assert not (tmp_path / "current").exists()
        assert not (tmp_path / "history.jsonl").exists()
