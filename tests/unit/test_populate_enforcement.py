"""Unit tests for populate-time digest enforcement (Story 1.5, AD-26).

Covers: happy-path publish, tampered/missing/extra artifacts, malformed
meta, nested layouts, meta exclusion, and existing-behavior pins
(write-once, guard order). Zero tools, zero network — direct
populate_via_staging calls on tmp_path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from runtime.adapters.cache import cache_entry_path, populate_via_staging
from runtime.domain.models import CorruptCacheError

LAYER = "palettes"
ENTRY = "ab" * 32


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _meta(artifact_hashes: dict[str, str] | None, algorithm: str = "sha256") -> str:
    payload: dict[str, object] = {"hash_algorithm": algorithm, "kind": "palette"}
    if artifact_hashes is not None:
        payload["artifact_hashes"] = artifact_hashes
    return json.dumps(payload, indent=2, sort_keys=True)


def _populate(files: dict[str, bytes], meta_text: str | None = "default") -> object:
    """Build a populate_fn writing files + meta into staging."""

    def _fn(staging: Path) -> None:
        for rel, content in files.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        text = meta_text
        if text == "default":
            text = _meta({rel: _h(content) for rel, content in files.items()})
        assert text is not None
        (staging / "meta.json").write_text(text, encoding="utf-8")

    return _fn


def _target(state_root: Path) -> Path:
    return cache_entry_path(state_root, LAYER, ENTRY)


def _staging_orphans(state_root: Path) -> list[Path]:
    return list((state_root / "cache").glob(".staging-*"))


class TestHappyPath:
    def test_matching_digests_publish(self, tmp_path: Path) -> None:
        assert populate_via_staging(_target(tmp_path), _populate({"a.bin": b"aaa"})) is True
        assert (_target(tmp_path) / "a.bin").read_bytes() == b"aaa"

    def test_meta_itself_is_never_hashed(self, tmp_path: Path) -> None:
        # meta.json content trivially matches nothing recorded — must pass.
        assert populate_via_staging(_target(tmp_path), _populate({"a.bin": b"aaa"})) is True

    def test_nested_layout_verified_by_relpath(self, tmp_path: Path) -> None:
        files = {"stem/effect/a.png": b"png", "top.bin": b"top"}
        assert populate_via_staging(_target(tmp_path), _populate(files)) is True

    def test_nested_layout_with_filename_keys_verified(self, tmp_path: Path) -> None:
        """WEG contract: files nest, meta keys them by FILENAME. Must verify."""

        def _fn(staging: Path) -> None:
            nested = staging / "abstract" / "effect"
            nested.mkdir(parents=True)
            (nested / "blur.jpg").write_bytes(b"blur-bytes")
            (staging / "meta.json").write_text(
                _meta({"blur.jpg": _h(b"blur-bytes")}), encoding="utf-8"
            )

        assert populate_via_staging(_target(tmp_path), _fn) is True
        assert (_target(tmp_path) / "abstract" / "effect" / "blur.jpg").read_bytes() == (
            b"blur-bytes"
        )

    def test_filename_key_tampered_nested_raises(self, tmp_path: Path) -> None:
        def _fn(staging: Path) -> None:
            nested = staging / "abstract" / "effect"
            nested.mkdir(parents=True)
            (nested / "blur.jpg").write_bytes(b"tampered")
            (staging / "meta.json").write_text(
                _meta({"blur.jpg": _h(b"original")}), encoding="utf-8"
            )

        with pytest.raises(CorruptCacheError, match="digest mismatch"):
            populate_via_staging(_target(tmp_path), _fn)
        assert not _target(tmp_path).exists()


class TestCorruption:
    def test_tampered_artifact_raises_and_publishes_nothing(self, tmp_path: Path) -> None:
        def _fn(staging: Path) -> None:
            (staging / "a.bin").write_bytes(b"tampered")
            (staging / "meta.json").write_text(_meta({"a.bin": _h(b"original")}), encoding="utf-8")

        with pytest.raises(CorruptCacheError, match="digest mismatch"):
            populate_via_staging(_target(tmp_path), _fn)
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []
        assert _staging_orphans(tmp_path) == []

    def test_recorded_but_absent_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CorruptCacheError, match="missing"):
            populate_via_staging(_target(tmp_path), _populate({}, _meta({"ghost.bin": _h(b"x")})))
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []

    def test_extra_unrecorded_file_raises(self, tmp_path: Path) -> None:
        def _fn(staging: Path) -> None:
            (staging / "a.bin").write_bytes(b"aaa")
            (staging / "stowaway.bin").write_bytes(b"s")
            (staging / "meta.json").write_text(_meta({"a.bin": _h(b"aaa")}), encoding="utf-8")

        with pytest.raises(CorruptCacheError, match="unrecorded"):
            populate_via_staging(_target(tmp_path), _fn)
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []

    def test_tampered_nested_file_raises(self, tmp_path: Path) -> None:
        def _fn(staging: Path) -> None:
            nested = staging / "stem" / "effect"
            nested.mkdir(parents=True)
            (nested / "a.png").write_bytes(b"tampered")
            (staging / "meta.json").write_text(
                _meta({"stem/effect/a.png": _h(b"original")}), encoding="utf-8"
            )

        with pytest.raises(CorruptCacheError, match="digest mismatch"):
            populate_via_staging(_target(tmp_path), _fn)
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []


class TestMalformedMeta:
    def test_absent_map_skips_verification(self, tmp_path: Path) -> None:
        """Heterogeneous/legacy shape (e.g. wallpaper metas keyed by
        content_hash): the generic mechanism stays shape-agnostic; Story 3.1
        annotates such entries on read. Only a PRESENT map opts into
        enforcement."""
        assert (
            populate_via_staging(_target(tmp_path), _populate({"a.bin": b"a"}, _meta(None))) is True
        )
        assert (_target(tmp_path) / "a.bin").read_bytes() == b"a"

    def test_malformed_map_raises(self, tmp_path: Path) -> None:
        def _fn(staging: Path) -> None:
            (staging / "a.bin").write_bytes(b"a")
            (staging / "meta.json").write_text(
                json.dumps({"hash_algorithm": "sha256", "artifact_hashes": {"a.bin": 123}}),
                encoding="utf-8",
            )

        with pytest.raises(CorruptCacheError, match="malformed"):
            populate_via_staging(_target(tmp_path), _fn)
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []

    def test_wrong_algorithm_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CorruptCacheError, match="hash_algorithm"):
            populate_via_staging(
                _target(tmp_path),
                _populate({"a.bin": b"a"}, _meta({"a.bin": _h(b"a")}, algorithm="md5")),
            )
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []

    def test_unparseable_meta_raises_not_crashes(self, tmp_path: Path) -> None:
        def _fn(staging: Path) -> None:
            (staging / "a.bin").write_bytes(b"a")
            (staging / "meta.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(CorruptCacheError, match="corrupt staging meta.json"):
            populate_via_staging(_target(tmp_path), _fn)
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []

    def test_non_object_meta_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CorruptCacheError, match="not an object"):
            populate_via_staging(_target(tmp_path), _populate({"a.bin": b"a"}, "[]"))
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []

    def test_traversal_and_absolute_keys_rejected(self, tmp_path: Path) -> None:
        for evil in ("/etc/passwd", "a/../../x", "", "meta.json"):
            with pytest.raises(CorruptCacheError):
                populate_via_staging(
                    _target(tmp_path), _populate({"a.bin": b"a"}, _meta({evil: _h(b"a")}))
                )
            assert not _target(tmp_path).exists()
            assert _staging_orphans(tmp_path) == []

    def test_bad_utf8_meta_raises(self, tmp_path: Path) -> None:
        def _fn(staging: Path) -> None:
            (staging / "a.bin").write_bytes(b"a")
            (staging / "meta.json").write_bytes(b"\xff\xfe{not json")

        with pytest.raises(CorruptCacheError, match="cannot read"):
            populate_via_staging(_target(tmp_path), _fn)
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []

    def test_nested_meta_json_must_be_recorded(self, tmp_path: Path) -> None:
        def _fn(staging: Path) -> None:
            (staging / "a.bin").write_bytes(b"a")
            sub = staging / "sub"
            sub.mkdir()
            (sub / "meta.json").write_text("{}", encoding="utf-8")
            (staging / "meta.json").write_text(_meta({"a.bin": _h(b"a")}), encoding="utf-8")

        with pytest.raises(CorruptCacheError, match="unrecorded"):
            populate_via_staging(_target(tmp_path), _fn)
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []

    def test_absent_map_wrong_algorithm_raises(self, tmp_path: Path) -> None:
        with pytest.raises(CorruptCacheError, match="hash_algorithm"):
            populate_via_staging(
                _target(tmp_path),
                _populate({"a.bin": b"a"}, _meta(None, algorithm="md5")),
            )
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []
        assert _staging_orphans(tmp_path) == []


class TestExistingBehaviorPreserved:
    def test_write_once_second_populate_returns_false(self, tmp_path: Path) -> None:
        assert populate_via_staging(_target(tmp_path), _populate({"a.bin": b"aaa"})) is True
        assert populate_via_staging(_target(tmp_path), _populate({"a.bin": b"bbb"})) is False
        assert (_target(tmp_path) / "a.bin").read_bytes() == b"aaa"

    def test_empty_staging_still_runtime_error(self, tmp_path: Path) -> None:
        """Guard order preserved: empty/meta-absent checks run BEFORE verify."""
        with pytest.raises(RuntimeError, match="left staging empty"):
            populate_via_staging(_target(tmp_path), lambda staging: None)
        assert not _target(tmp_path).exists()
        assert _staging_orphans(tmp_path) == []
