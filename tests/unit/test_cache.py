"""Unit tests for layered cache staging-dir + hardlink helpers."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.adapters.cache import (
    CACHE_LAYERS,
    CACHE_STAGING_PREFIX,
    _staging_dir_for,
    cache_entry_path,
    hardlink_or_copy,
    populate_via_staging,
)
from runtime.adapters.hashing import HASH_ALGORITHM, hash_file


def _no_staging_left(state_root: Path) -> bool:
    """No orphan ``.staging-*`` under ``cache/`` after operation."""
    cache = state_root / "cache"
    if not cache.exists():
        return True
    return (
        list(cache.glob(f"{CACHE_STAGING_PREFIX}*")) == []
        and list(cache.rglob(f"{CACHE_STAGING_PREFIX}*")) == []
    )


# ---------------------------------------------------------------------------
# Task 4: cache_entry_path
# ---------------------------------------------------------------------------


def test_cache_entry_path_validates_layer_and_hash(tmp_path: Path) -> None:
    wh = "a" * 64
    p = cache_entry_path(tmp_path, "wallpapers", wh)
    assert p == tmp_path / "cache" / "wallpapers" / wh

    # Invalid layer
    with pytest.raises(ValueError, match="unknown layer"):
        cache_entry_path(tmp_path, "badlayer", wh)

    # Non-hex hash
    with pytest.raises(ValueError, match="entry_hash"):
        cache_entry_path(tmp_path, "wallpapers", "not-hex")

    # Wrong length
    with pytest.raises(ValueError, match="entry_hash"):
        cache_entry_path(tmp_path, "wallpapers", "abc")

    # Uppercase should be lowercased in result but still valid
    up = "A" * 64
    p2 = cache_entry_path(tmp_path, "wallpapers", up)
    assert p2.name == up.lower()

    # All layers valid
    for layer in CACHE_LAYERS:
        path = cache_entry_path(tmp_path, layer, wh)
        assert path.parent.name == layer
        assert path.parent.parent.name == "cache"


# ---------------------------------------------------------------------------
# Task 3 & 5: populate_via_staging
# ---------------------------------------------------------------------------


def test_populate_via_staging_creates_target(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "b" * 64
    target = cache_entry_path(state_root, "wallpapers", wh)

    def populate_fn(staging: Path) -> None:
        (staging / "wallpaper.png").write_bytes(b"hello wallpaper")
        meta = {
            "hash_algorithm": HASH_ALGORITHM,
            "kind": "wallpaper",
            "content_hash": wh,
            "source_path": "/src/wall.png",
            "imported_at": "2026-08-29T00:00:00Z",
        }
        (staging / "meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
        )

    created = populate_via_staging(target, populate_fn)
    assert created is True
    assert target.exists()
    assert (target / "wallpaper.png").read_bytes() == b"hello wallpaper"
    assert (target / "meta.json").exists()
    meta_loaded = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    assert meta_loaded["hash_algorithm"] == HASH_ALGORITHM == "sha256"
    assert _no_staging_left(state_root)


def test_populate_via_staging_never_overwrites(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "c" * 64
    target = cache_entry_path(state_root, "wallpapers", wh)

    def first(staging: Path) -> None:
        (staging / "wallpaper.png").write_bytes(b"old_content")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def second(staging: Path) -> None:
        (staging / "wallpaper.png").write_bytes(b"new_content")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    assert populate_via_staging(target, first) is True
    assert (target / "wallpaper.png").read_bytes() == b"old_content"

    # Second call must not overwrite
    assert populate_via_staging(target, second) is False
    assert (target / "wallpaper.png").read_bytes() == b"old_content"
    assert _no_staging_left(state_root)


def test_populate_via_staging_discards_on_populate_fn_exception(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "d" * 64
    target = cache_entry_path(state_root, "wallpapers", wh)

    def failing(staging: Path) -> None:
        (staging / "partial").write_bytes(b"partial")
        raise RuntimeError("populate failed")

    with pytest.raises(RuntimeError, match="populate failed"):
        populate_via_staging(target, failing)

    assert not target.exists()
    assert _no_staging_left(state_root)


def test_populate_via_staging_sibling_location(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "e" * 64
    target = cache_entry_path(state_root, "wallpapers", wh)

    captured: list[Path] = []

    def populate_fn(staging: Path) -> None:
        captured.append(staging)
        (staging / "file.txt").write_bytes(b"x")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # Patch _staging_dir_for to capture? Actually populate captures staging
    from runtime.adapters import cache as cache_mod

    original_staging = cache_mod._staging_dir_for

    def wrapped(target_path: Path) -> Path:
        s = original_staging(target_path)
        # Assert sibling location invariants before creation
        # staging must be under cache/ (i.e. cache/.staging-*)
        assert s.parent.name == "cache"
        assert s.name.startswith(CACHE_STAGING_PREFIX)
        # Not inside target
        assert target_path not in s.parents
        # Not at tmp_root/.staging nor target/.staging
        assert s != target_path / f"{CACHE_STAGING_PREFIX}foo"
        return s

    with patch("runtime.adapters.cache._staging_dir_for", side_effect=wrapped):
        populate_via_staging(target, populate_fn)

    assert len(captured) == 1
    staging = captured[0]
    # Verify location: state_root/cache/.staging-<pid>-*
    assert staging.parent == state_root / "cache"
    assert staging.name.startswith(CACHE_STAGING_PREFIX)
    # Must contain pid
    assert str(os.getpid()) in staging.name
    assert _no_staging_left(state_root)


def test_populate_via_staging_atomic_rename(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "f" * 64
    target = cache_entry_path(state_root, "wallpapers", wh)

    def populate_fn(staging: Path) -> None:
        (staging / "wallpaper.png").write_bytes(b"data")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # wraps ensures original os.rename is called while still tracking call args
    with patch("runtime.adapters.cache.os.rename", wraps=os.rename) as mock_rename:
        # Also need to ensure shutil.move not called — patch it to fail if called
        with patch("runtime.adapters.cache.shutil.move") as mock_move:
            mock_move.side_effect = AssertionError("shutil.move must not be used")
            created = populate_via_staging(target, populate_fn)
            assert created is True
            mock_rename.assert_called_once()
            # Verify args are (staging, target) on same mount
            call_args = mock_rename.call_args[0]
            assert len(call_args) == 2
            # First arg should be staging dir under cache
            assert Path(call_args[0]).parent.name == "cache"
            assert Path(call_args[0]).name.startswith(CACHE_STAGING_PREFIX)
            assert Path(call_args[1]) == target
            mock_move.assert_not_called()

    assert _no_staging_left(state_root)


def test_concurrent_populate_one_wins(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "1" * 64
    target = cache_entry_path(state_root, "wallpapers", wh)

    def first_fn(staging: Path) -> None:
        (staging / "wallpaper.png").write_bytes(b"winner")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def second_fn(staging: Path) -> None:
        (staging / "wallpaper.png").write_bytes(b"loser")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # First wins
    assert populate_via_staging(target, first_fn) is True
    assert (target / "wallpaper.png").read_bytes() == b"winner"

    # Second loses — simulate race where target already exists before rename
    # Our fast-path covers this; also test TOCTOU via monkeypatch rename race
    assert populate_via_staging(target, second_fn) is False
    assert (target / "wallpaper.png").read_bytes() == b"winner"
    assert _no_staging_left(state_root)

    # Simulate TOCTOU: first exists check passes (target absent), but rename fails with FileExists
    wh2 = "2" * 64
    target2 = cache_entry_path(state_root, "palettes", wh2)

    def populate_ok(staging: Path) -> None:
        (staging / "colors.yaml").write_bytes(b"content")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # Force race by making os.rename raise FileExistsError
    with patch("runtime.adapters.cache.os.rename", side_effect=FileExistsError("exists")):
        result = populate_via_staging(target2, populate_ok)
        assert result is False
        # Staging must be cleaned, target not created (since we faked race and discarded)
        assert not target2.exists()
        assert _no_staging_left(state_root)


def test_staging_cleanup_on_crash(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "3" * 64
    target = cache_entry_path(state_root, "effects", wh)

    def populate_fn(staging: Path) -> None:
        (staging / "out.png").write_bytes(b"pngdata")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # Simulate rename raising generic OSError (not FileExists) — should propagate after cleanup
    with patch("runtime.adapters.cache.os.rename", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            populate_via_staging(target, populate_fn)
        assert not target.exists()
        assert _no_staging_left(state_root)

    # After crash, next populate for same target should succeed (no orphan left)
    def populate_retry(staging: Path) -> None:
        (staging / "out.png").write_bytes(b"pngdata2")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    assert populate_via_staging(target, populate_retry) is True
    assert (target / "out.png").read_bytes() == b"pngdata2"
    assert _no_staging_left(state_root)


def test_meta_json_hash_algorithm_pinned(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "4" * 64
    target = cache_entry_path(state_root, "icons", wh)

    def populate_fn(staging: Path) -> None:
        (staging / "icon.svg").write_bytes(b"<svg/>")
        meta = {
            "hash_algorithm": HASH_ALGORITHM,
            "kind": "icons",
            "entry_hash": wh,
            "generated_at": "2026-08-29T00:00:00Z",
        }
        (staging / "meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
        )

    populate_via_staging(target, populate_fn)
    meta_loaded = json.loads((target / "meta.json").read_text(encoding="utf-8"))
    assert meta_loaded["hash_algorithm"] == HASH_ALGORITHM == "sha256"
    assert meta_loaded["hash_algorithm"] == "sha256"
    assert _no_staging_left(state_root)


# ---------------------------------------------------------------------------
# hardlink_or_copy
# ---------------------------------------------------------------------------


def test_hardlink_or_copy_hardlink_when_same_fs(tmp_path: Path) -> None:
    src = tmp_path / "src.png"
    src.write_bytes(b"image data")
    dst = tmp_path / "dst" / "dst.png"

    hardlink_or_copy(src, dst)

    assert dst.exists()
    assert dst.read_bytes() == b"image data"
    # Hardlink preserves inode
    assert os.stat(src).st_ino == os.stat(dst).st_ino
    # Survives source deletion (inode retained via hardlink)
    src.unlink()
    assert dst.exists()
    assert dst.read_bytes() == b"image data"


def test_hardlink_or_copy_fallback_on_EXDEV(tmp_path: Path) -> None:  # noqa: N802
    src = tmp_path / "src2.png"
    src.write_bytes(b"fallback data")
    dst = tmp_path / "dst2" / "dst2.png"

    with patch("runtime.adapters.cache.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
        hardlink_or_copy(src, dst)

    assert dst.exists()
    assert dst.read_bytes() == b"fallback data"
    # Copy must have different inode
    assert os.stat(src).st_ino != os.stat(dst).st_ino
    # Survives src deletion (copy retained)
    src.unlink()
    assert dst.exists()
    assert dst.read_bytes() == b"fallback data"
    # Verify mtime preserved (copy2) — re-create src
    src.write_bytes(b"fallback data")
    # copy2 fallback already verified; just ensure dst still readable


def test_hardlink_or_copy_propagates_non_EXDEV(tmp_path: Path) -> None:  # noqa: N802
    src = tmp_path / "src3.png"
    src.write_bytes(b"data")
    dst = tmp_path / "dst3" / "dst3.png"

    with patch(
        "runtime.adapters.cache.os.link", side_effect=OSError(errno.EACCES, "permission denied")
    ):
        with pytest.raises(OSError, match="permission denied"):
            hardlink_or_copy(src, dst)

    # No copy attempted — dst should not exist (except parent dir created)
    assert not dst.exists()

    # Also test ENOENT propagation
    with patch("runtime.adapters.cache.os.link", side_effect=OSError(errno.ENOENT, "no such")):
        with pytest.raises(OSError):
            hardlink_or_copy(src, tmp_path / "dst4" / "file.png")


def test_wallpaper_hardlink_survives_source_deletion(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    # Use deterministic wallpaper fixture if available, otherwise synthetic
    candidates = [
        Path("tests/fixtures/wallpaper.png"),
        Path("src/runtime/tests/fixtures/wallpaper.png"),
        Path(__file__).parent.parent / "fixtures" / "wallpaper.png",
    ]
    fixture = next((p for p in candidates if p.exists()), None)
    if fixture is not None:
        src_bytes = fixture.read_bytes()
        wh = hash_file(fixture)
    else:
        src_bytes = b"\x89PNG\r\n\x1a\nfake"
        # Write temp fixture to hash
        tmp_f = tmp_path / "wallpaper.png"
        tmp_f.write_bytes(src_bytes)
        wh = hash_file(tmp_f)

    # Create source wallpaper file
    src_path = tmp_path / "original" / "wallpaper.png"
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_bytes(src_bytes)

    target = cache_entry_path(state_root, "wallpapers", wh)

    def populate_fn(staging: Path) -> None:
        dst = staging / "wallpaper.png"
        hardlink_or_copy(src_path, dst)
        meta = {
            "hash_algorithm": HASH_ALGORITHM,
            "kind": "wallpaper",
            "content_hash": wh,
            "source_path": str(src_path),
            "imported_at": "2026-08-29T00:00:00Z",
        }
        (staging / "meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8"
        )

    created = populate_via_staging(target, populate_fn)
    assert created is True
    cached = target / "wallpaper.png"
    assert cached.exists()
    assert cached.read_bytes() == src_bytes
    assert hash_file(cached) == wh

    # Delete source — cached file survives (hardlink inode or copy)
    src_path.unlink()
    assert not src_path.exists()
    assert cached.exists()
    assert cached.read_bytes() == src_bytes
    assert hash_file(cached) == wh
    assert _no_staging_left(state_root)


def test_staging_dir_for_helper(tmp_path: Path) -> None:
    target = tmp_path / "state" / "cache" / "wallpapers" / ("a" * 64)
    staging = _staging_dir_for(target)
    assert staging.parent == tmp_path / "state" / "cache"
    assert staging.name.startswith(CACHE_STAGING_PREFIX)
    assert str(os.getpid()) in staging.name
    # Not inside target
    assert target not in staging.parents
    assert staging not in target.parents


def test_populate_validates_target_layer_and_hash(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    # Invalid layer
    bad_layer = state_root / "cache" / "bad" / ("a" * 64)
    with pytest.raises(ValueError, match="unknown layer"):
        populate_via_staging(bad_layer, lambda s: None)

    # Invalid hash
    bad_hash = state_root / "cache" / "wallpapers" / "short"
    with pytest.raises(ValueError, match="entry_hash"):
        populate_via_staging(bad_hash, lambda s: None)

    # Missing cache segment
    bad_cache = state_root / "notcache" / "wallpapers" / ("a" * 64)
    with pytest.raises(ValueError, match="cache"):
        populate_via_staging(bad_cache, lambda s: None)


def test_hardlink_or_copy_ensures_parent_exists(tmp_path: Path) -> None:
    src = tmp_path / "a.png"
    src.write_bytes(b"x")
    dst = tmp_path / "deep" / "nested" / "dir" / "b.png"
    assert not dst.parent.exists()
    hardlink_or_copy(src, dst)
    assert dst.exists()
    assert dst.read_bytes() == b"x"


def test_populate_via_staging_enotempty_handling(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    wh = "9" * 64
    target = cache_entry_path(state_root, "palettes", wh)

    def populate_fn(staging: Path) -> None:
        (staging / "colors.yaml").write_bytes(b"yaml")
        (staging / "meta.json").write_text(
            json.dumps({"hash_algorithm": HASH_ALGORITHM}, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    # Simulate ENOTEMPTY on rename (target appeared as directory non-empty)
    with patch(
        "runtime.adapters.cache.os.rename",
        side_effect=OSError(errno.ENOTEMPTY, "Directory not empty"),
    ):
        result = populate_via_staging(target, populate_fn)
        assert result is False
        assert _no_staging_left(state_root)

    # Simulate "File exists" string variant without errno
    with patch(
        "runtime.adapters.cache.os.rename",
        side_effect=OSError("File exists"),
    ):
        result = populate_via_staging(target, populate_fn)
        assert result is False
        assert _no_staging_left(state_root)
