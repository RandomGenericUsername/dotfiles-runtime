"""JSON clipboard history store: dedupe, recency, favorites, eviction, atomicity."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from runtime.adapters.json_history_store import JsonClipboardStore
from runtime.domain.clipboard import ClipboardItem, RetentionLimits


def make_store(tmp_path: Path, *, clock=lambda: 100.0) -> JsonClipboardStore:
    return JsonClipboardStore(
        tmp_path / "history.json",
        image_dir=tmp_path / "images",
        clock=clock,
    )


def item(hash_: str, kind: str = "text", ts: float = 1.0, **kw: object) -> ClipboardItem:
    return ClipboardItem(hash=hash_, kind=kind, timestamp=ts, **kw)  # type: ignore[arg-type]


class TestPersistence:
    def test_missing_file_loads_empty(self, tmp_path: Path) -> None:
        assert make_store(tmp_path).load() == []

    def test_add_then_load_roundtrip(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.add(item("h1", text="hello"))
        loaded = store.load()
        assert [entry.hash for entry in loaded] == ["h1"]
        assert loaded[0].text == "hello"

    def test_newest_first(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.add(item("old", ts=1.0))
        store.add(item("new", ts=2.0))
        assert [entry.hash for entry in store.load()] == ["new", "old"]

    def test_corrupt_file_loads_empty(self, tmp_path: Path) -> None:
        (tmp_path / "history.json").write_text("{ broken")
        assert make_store(tmp_path).load() == []

    def test_skips_malformed_records(self, tmp_path: Path) -> None:
        records = [{"hash": "ok", "kind": "text", "timestamp": 1}, 42]
        (tmp_path / "history.json").write_text(json.dumps({"version": 1, "items": records}))
        assert [entry.hash for entry in make_store(tmp_path).load()] == ["ok"]


class TestDedupe:
    def test_recopy_bumps_recency_without_duplicating(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.add(item("h", ts=1.0))
        store.add(item("h", ts=5.0))
        loaded = store.load()
        assert len(loaded) == 1
        assert loaded[0].timestamp == 5.0

    def test_recopy_preserves_favorite(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.add(item("h", ts=1.0))
        store.set_favorite("h", True)
        stored = store.add(item("h", ts=9.0))
        assert stored.favorite is True
        assert store.load()[0].favorite is True


class TestMutations:
    def test_delete_removes_item_and_image(self, tmp_path: Path) -> None:
        images = tmp_path / "images"
        images.mkdir()
        image = images / "pic.png"
        image.write_bytes(b"png")
        store = make_store(tmp_path)
        store.add(item("h", kind="image", path=str(image)))
        assert store.delete("h") is True
        assert store.load() == []
        assert not image.exists()

    def test_delete_missing_returns_false(self, tmp_path: Path) -> None:
        assert make_store(tmp_path).delete("nope") is False

    def test_set_favorite(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.add(item("h"))
        assert store.set_favorite("h", True) is True
        assert store.load()[0].favorite is True
        assert store.set_favorite("missing", True) is False


class TestEviction:
    def test_evicts_over_limit_and_removes_image(self, tmp_path: Path) -> None:
        images = tmp_path / "images"
        images.mkdir()
        image = images / "old.png"
        image.write_bytes(b"png")
        store = make_store(tmp_path)
        store.add(item("new", ts=10.0))
        store.add(item("old-img", kind="image", ts=1.0, path=str(image)))
        evicted = store.evict(RetentionLimits(text=10, image=0))
        assert [entry.hash for entry in evicted] == ["old-img"]
        assert not image.exists()

    def test_evict_keeps_favorites(self, tmp_path: Path) -> None:
        store = make_store(tmp_path)
        store.add(item("fav", ts=1.0))
        store.set_favorite("fav", True)
        store.add(item("new", ts=2.0))
        evicted = store.evict(RetentionLimits(text=0))
        assert [entry.hash for entry in evicted] == ["new"]
        assert {entry.hash for entry in store.load()} == {"fav"}

    def test_cleanup_orphans(self, tmp_path: Path) -> None:
        images = tmp_path / "images"
        images.mkdir()
        referenced = images / "keep.png"
        referenced.write_bytes(b"png")
        orphan = images / "orphan.png"
        orphan.write_bytes(b"png")
        store = make_store(tmp_path)
        store.add(item("h", kind="image", path=str(referenced)))
        assert store.cleanup_orphans() == 1
        assert referenced.exists()
        assert not orphan.exists()


class TestAtomicity:
    def test_interrupted_write_keeps_previous_document(self, tmp_path: Path, monkeypatch) -> None:
        store = make_store(tmp_path)
        store.add(item("first"))
        before = (tmp_path / "history.json").read_text()

        def boom(src, dst):  # type: ignore[no-untyped-def]
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            store.add(item("second"))
        assert (tmp_path / "history.json").read_text() == before
        assert [entry.hash for entry in store.load()] == ["first"]
