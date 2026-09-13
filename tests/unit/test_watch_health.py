"""Persisted watch-set health store (AD-40, P5 follow-up)."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.adapters.watch_health import WatchHealthStore
from runtime.domain.watch import WatchStatus


def test_roundtrip_healthy(tmp_path: Path) -> None:
    store = WatchHealthStore(tmp_path)
    store.write(WatchStatus(registered=7))
    record = store.read()
    assert record is not None
    assert record.degraded is False
    assert record.failed_roots == ()
    assert record.last_error is None
    assert record.updated_at


def test_roundtrip_degraded(tmp_path: Path) -> None:
    store = WatchHealthStore(tmp_path)
    store.write(
        WatchStatus(
            registered=3,
            failed=("/spine/icon-templates", "/spine/weg/effects.yaml"),
            last_error="No space left on device",
        )
    )
    record = store.read()
    assert record is not None
    assert record.degraded is True
    assert record.failed_roots == ("/spine/icon-templates", "/spine/weg/effects.yaml")
    assert record.last_error == "No space left on device"


def test_absent_is_none(tmp_path: Path) -> None:
    assert WatchHealthStore(tmp_path).read() is None


def test_corrupt_is_none(tmp_path: Path) -> None:
    path = tmp_path / "watch-health.json"
    path.write_text("{not json", encoding="utf-8")
    assert WatchHealthStore(tmp_path).read() is None


def test_invalid_shape_is_none(tmp_path: Path) -> None:
    path = tmp_path / "watch-health.json"
    path.write_text(json.dumps({"degraded": "yes"}), encoding="utf-8")
    assert WatchHealthStore(tmp_path).read() is None


def test_symlink_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"degraded": False, "failed_roots": []}), encoding="utf-8")
    link = tmp_path / "watch-health.json"
    link.symlink_to(target)
    assert WatchHealthStore(tmp_path).read() is None


def test_write_is_atomic_replace(tmp_path: Path) -> None:
    store = WatchHealthStore(tmp_path)
    store.write(WatchStatus(registered=1, failed=("/a",), last_error="ENOSPC"))
    store.write(WatchStatus(registered=2))
    record = store.read()
    assert record is not None
    assert record.degraded is False
    assert record.failed_roots == ()
    # No stray tmp file survives the atomic replace.
    assert not list(tmp_path.glob(".watch-health.json.tmp-*"))
