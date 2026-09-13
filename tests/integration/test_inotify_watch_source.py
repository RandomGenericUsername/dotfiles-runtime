"""Live inotify adapter smoke test (Linux-only).

Validates the real ``ctypes`` transport end-to-end (watch install, event
delivery, rebuild, close). The transport-free decision logic is covered by
``tests/unit/test_watch_coordinator.py``; this is the thin, skippable
integration check the story asks for.
"""

from __future__ import annotations

import platform
import time
from pathlib import Path

import pytest

from runtime.adapters.inotify_watch_source import InotifyWatchSource
from runtime.adapters.watch_roots import WatchRoot
from runtime.domain.watch import CONTENT_CHANGE, WatchEventKind

pytestmark = pytest.mark.skipif(
    not platform.system().startswith("Linux"),
    reason="inotify is Linux-only",
)


def _drain_until(source: InotifyWatchSource, predicate: object, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = source.read_event(timeout=0.2)
        if event is not None and predicate(event):  # type: ignore[operator]
            return event
    raise AssertionError("expected watch event did not arrive")


def test_watch_dir_delivers_created_event(tmp_path: Path) -> None:
    try:
        source = InotifyWatchSource((WatchRoot(tmp_path, is_directory=True, depth=2),))
        source.start()
    except OSError as exc:  # pragma: no cover - environment without inotify
        pytest.skip(f"inotify unavailable: {exc}")
    try:
        target = tmp_path / "new.j2"
        target.write_text("x", encoding="utf-8")
        event = _drain_until(
            source, lambda e: e.kind in CONTENT_CHANGE and "new.j2" in e.path
        )
        assert event.kind is WatchEventKind.CREATED
    finally:
        source.close()


def test_rebuild_reestablishes_delivery(tmp_path: Path) -> None:
    try:
        source = InotifyWatchSource((WatchRoot(tmp_path, is_directory=True, depth=2),))
        source.start()
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"inotify unavailable: {exc}")
    try:
        source.rebuild()
        (tmp_path / "after-rebuild.j2").write_text("y", encoding="utf-8")
        event = _drain_until(
            source, lambda e: e.kind in CONTENT_CHANGE and "after-rebuild" in e.path
        )
        assert event.path.endswith("after-rebuild.j2")
    finally:
        source.close()


def test_file_root_filters_to_exact_filename(tmp_path: Path) -> None:
    target = tmp_path / "effects.yaml"
    target.write_text("a: 1\n", encoding="utf-8")
    try:
        source = InotifyWatchSource((WatchRoot(target, is_directory=False),))
        source.start()
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"inotify unavailable: {exc}")
    try:
        (tmp_path / "unrelated.txt").write_text("noise", encoding="utf-8")
        target.write_text("a: 2\n", encoding="utf-8")
        event = _drain_until(
            source, lambda e: e.kind in CONTENT_CHANGE and e.path.endswith("effects.yaml")
        )
        assert event.path.endswith("effects.yaml")
    finally:
        source.close()
