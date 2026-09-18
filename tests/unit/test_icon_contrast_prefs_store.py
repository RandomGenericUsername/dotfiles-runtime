"""Unit tests for the icon-contrast prefs store adapter (tasks 1.2 + 1.3).

Corrupt-tolerant reads, missing-file-OK, atomic-write shape, the shared
``resolve_contrast`` helper, and the governing-hash helper (direct input,
variant with meta, variant without meta, corrupt meta).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.icon_contrast_prefs_store import (
    governing_wallpaper_hash,
    read_icons_policy,
    read_prefs,
    resolve_contrast,
    store_path,
    write_pref,
)

H1 = "a" * 64
H2 = "b" * 64


class TestStorePath:
    def test_lives_directly_under_state_root(self, tmp_path: Path) -> None:
        assert store_path(tmp_path) == tmp_path / "icon-contrast.json"


class TestReadPrefs:
    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_prefs(tmp_path / "icon-contrast.json") == {}

    def test_roundtrip(self, tmp_path: Path) -> None:
        path = store_path(tmp_path)
        write_pref(path, H1, False)
        assert read_prefs(path) == {H1: False}

    def test_corrupt_file_is_empty_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = store_path(tmp_path)
        path.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING", logger="runtime.adapters.icon_contrast_prefs_store"):
            assert read_prefs(path) == {}
        assert any("corrupt" in r.getMessage() for r in caplog.records)

    def test_wrong_version_is_empty(self, tmp_path: Path) -> None:
        path = store_path(tmp_path)
        path.write_text('{"version": 2, "prefs": {}}', encoding="utf-8")
        assert read_prefs(path) == {}

    def test_unreadable_non_utf8_is_empty(self, tmp_path: Path) -> None:
        path = store_path(tmp_path)
        path.write_bytes(b"\xff\xfe\x00bad")
        assert read_prefs(path) == {}


class TestWritePref:
    def test_write_is_atomic_shape(self, tmp_path: Path) -> None:
        path = store_path(tmp_path)
        write_pref(path, H1, False)
        # Canonical text, single file, no tmp leftovers.
        assert path.read_text(encoding="utf-8").endswith("\n")
        assert list(tmp_path.iterdir()) == [path]

    def test_write_merges_with_existing(self, tmp_path: Path) -> None:
        path = store_path(tmp_path)
        write_pref(path, H1, False)
        updated = write_pref(path, H2, True)
        assert updated == {H1: False, H2: True}
        assert read_prefs(path) == {H1: False, H2: True}

    def test_write_heals_corrupt_file(self, tmp_path: Path) -> None:
        path = store_path(tmp_path)
        path.write_text("garbage", encoding="utf-8")
        assert write_pref(path, H1, True) == {H1: True}

    def test_write_rejects_bad_hash(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid wallpaper hash"):
            write_pref(store_path(tmp_path), "nope", True)


class TestResolveContrast:
    def test_auto_absent_is_default_on(self, tmp_path: Path) -> None:
        resolved = resolve_contrast(
            flag="auto", governing_hash=H1, store_file=store_path(tmp_path)
        )
        assert resolved == (True, "default")

    def test_auto_follows_store(self, tmp_path: Path) -> None:
        path = store_path(tmp_path)
        write_pref(path, H1, False)
        assert resolve_contrast(flag="auto", governing_hash=H1, store_file=path) == (
            False,
            "store",
        )

    def test_explicit_flag_wins_over_store(self, tmp_path: Path) -> None:
        path = store_path(tmp_path)
        write_pref(path, H1, False)
        assert resolve_contrast(flag="on", governing_hash=H1, store_file=path) == (True, "flag")


class TestGoverningWallpaperHash:
    def test_direct_file_hashes_content(self, tmp_path: Path) -> None:
        img = tmp_path / "wall.png"
        img.write_bytes(b"pixels")
        assert governing_wallpaper_hash(img, tmp_path / "state") == hash_file(img)

    def test_variant_resolves_parent_hash(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        entry = state_root / "cache" / "effects" / ("e" * 64)
        variant = entry / "wallpaper-stem" / "effect" / "variant.png"
        variant.parent.mkdir(parents=True)
        variant.write_bytes(b"variant pixels")
        (entry / "meta.json").write_text(
            json.dumps({"source_wallpaper_hash": H1}), encoding="utf-8"
        )
        assert governing_wallpaper_hash(variant, state_root) == H1

    def test_variant_without_meta_falls_back_to_content_hash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state_root = tmp_path / "state"
        variant = state_root / "cache" / "effects" / ("e" * 64) / "variant.png"
        variant.parent.mkdir(parents=True)
        variant.write_bytes(b"variant pixels")
        with caplog.at_level("WARNING", logger="runtime.adapters.icon_contrast_prefs_store"):
            result = governing_wallpaper_hash(variant, state_root)
        assert result == hash_file(variant)
        assert caplog.records, "unresolvable parent must warn"

    def test_variant_with_corrupt_meta_falls_back(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state_root = tmp_path / "state"
        entry = state_root / "cache" / "effects" / ("e" * 64)
        variant = entry / "variant.png"
        variant.parent.mkdir(parents=True)
        variant.write_bytes(b"variant pixels")
        (entry / "meta.json").write_text("{corrupt", encoding="utf-8")
        with caplog.at_level("WARNING", logger="runtime.adapters.icon_contrast_prefs_store"):
            assert governing_wallpaper_hash(variant, state_root) == hash_file(variant)
        assert caplog.records

    def test_variant_with_bad_recorded_hash_falls_back(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        entry = state_root / "cache" / "effects" / ("e" * 64)
        variant = entry / "variant.png"
        variant.parent.mkdir(parents=True)
        variant.write_bytes(b"variant pixels")
        (entry / "meta.json").write_text(
            json.dumps({"source_wallpaper_hash": "not-a-hash"}), encoding="utf-8"
        )
        assert governing_wallpaper_hash(variant, state_root) == hash_file(variant)

    def test_unhashable_input_uses_fallback(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        missing = tmp_path / "gone.png"
        assert governing_wallpaper_hash(missing, state_root, fallback_hash=H1) == H1


class TestReadIconsPolicy:
    def _write_icons_meta(
        self, state_root: Path, entry_hash: str, contrast: dict | None
    ) -> None:
        entry = state_root / "cache" / "icons" / entry_hash
        entry.mkdir(parents=True)
        payload: dict = {
            "hash_algorithm": "sha256",
            "kind": "icons",
            "entry_hash": entry_hash,
            "source_palette_hash": "p" * 64,
            "input_templates_hash": "t" * 64,
            "input_mappings_hash": "m" * 64,
            "artifact_hashes": {},
            "generated_at": "2026-01-01T00:00:00Z",
        }
        if contrast is not None:
            payload["contrast"] = contrast
        (entry / "meta.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_returns_policy(self, tmp_path: Path) -> None:
        self._write_icons_meta(
            tmp_path, H1, {"policy": {"source": "store", "enabled": False}}
        )
        assert read_icons_policy(tmp_path, H1) == {"source": "store", "enabled": False}

    def test_pre_policy_entry_returns_none(self, tmp_path: Path) -> None:
        self._write_icons_meta(tmp_path, H1, {"backdrop_source": "palette"})
        assert read_icons_policy(tmp_path, H1) is None

    def test_guard_entry_without_contrast_returns_none(self, tmp_path: Path) -> None:
        self._write_icons_meta(tmp_path, H1, None)
        assert read_icons_policy(tmp_path, H1) is None

    def test_absent_entry_returns_none(self, tmp_path: Path) -> None:
        assert read_icons_policy(tmp_path, H1) is None
