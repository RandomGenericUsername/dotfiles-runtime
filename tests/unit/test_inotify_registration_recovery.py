"""Watch-registration exhaustion recovery (AD-40, P5 follow-up).

``inotify_add_watch`` can fail with ``ENOSPC``/``EMFILE`` when the per-user
watch/instance limit is hit. That must not silently drop a root: the failure
is logged loudly, surfaced as a degraded :class:`WatchStatus`, retried on the
next safe opportunity (an arriving event — never a poll), and (for the real
adapter) recovered by ``rebuild``.
"""

from __future__ import annotations

import errno
import logging
import threading
from pathlib import Path

import pytest

import runtime.adapters.inotify_watch_source as inotify_module
from runtime.adapters.inotify_watch_source import InotifyWatchSource
from runtime.adapters.watch_roots import WatchRoot
from runtime.application.watch import WatchCoordinator, WatchTrigger
from runtime.domain.watch import WatchEvent, WatchEventKind, WatchStatus
from runtime.ports.watch_source import IWatchSource


class _FlakySource(IWatchSource):
    """Fake source: starts degraded, recovers on ``rebuild``."""

    def __init__(self, *, degraded: bool = True) -> None:
        self._degraded = degraded
        self.rebuilds = 0
        self._events = [WatchEvent(kind=WatchEventKind.MODIFIED, path="/t/x")]

    def start(self) -> None:
        self._degraded = True

    def close(self) -> None:
        return None

    def rebuild(self) -> None:
        self.rebuilds += 1
        self._degraded = False

    def status(self) -> WatchStatus:
        if not self._degraded:
            return WatchStatus(registered=1)
        return WatchStatus(registered=0, failed=("/t/x",), last_error="No space left on device")

    def read_event(self, timeout: float | None = None) -> WatchEvent | None:
        if self._events:
            return self._events.pop(0)
        return None


class TestCoordinatorRetry:
    def test_degraded_source_is_retried_on_next_event(self) -> None:
        source = _FlakySource(degraded=True)
        statuses: list[WatchStatus] = []
        triggers: list[WatchTrigger] = []
        stop = threading.Event()

        def _on_trigger(trigger: WatchTrigger) -> None:
            triggers.append(trigger)
            stop.set()

        coordinator = WatchCoordinator(source, _on_trigger, on_status=statuses.append)
        coordinator.serve_events(stop)

        assert source.rebuilds == 1
        assert [s.degraded for s in statuses] == [True, False]
        assert len(triggers) == 1
        assert triggers[0].reason == "change"

    def test_healthy_source_is_not_rebuilt_on_events(self) -> None:
        source = _FlakySource(degraded=False)
        triggers: list[WatchTrigger] = []
        stop = threading.Event()

        def _on_trigger(trigger: WatchTrigger) -> None:
            triggers.append(trigger)
            stop.set()

        WatchCoordinator(source, _on_trigger).serve_events(stop)
        assert source.rebuilds == 0
        assert len(triggers) == 1


class _FakeLib:
    """Minimal libc inotify stand-in: first add fails, then succeeds."""

    def __init__(self, *, fail_first: bool = True) -> None:
        self.calls = 0
        self.rm_calls = 0
        self.fail_first = fail_first
        self.next_wd = 100

    def inotify_init1(self, _flags: int) -> int:
        return 42

    def inotify_add_watch(self, _fd: int, _path: bytes, _mask: int) -> int:
        self.calls += 1
        if self.fail_first and self.calls == 1:
            return -1
        wd = self.next_wd
        self.next_wd += 1
        return wd

    def inotify_rm_watch(self, _fd: int, _wd: int) -> int:
        self.rm_calls += 1
        return 0


class TestInotifyAdapterExhaustion:
    def test_start_failure_is_surfaced_then_rebuild_recovers(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        (tmp_path / "tree").mkdir()
        monkeypatch.setattr(inotify_module.ctypes, "get_errno", lambda: errno.ENOSPC)
        source = InotifyWatchSource((WatchRoot(tmp_path / "tree", is_directory=True, depth=1),))
        source._lib = _FakeLib(fail_first=True)  # type: ignore[assignment]
        with caplog.at_level(logging.ERROR, logger="runtime.adapters.inotify_watch_source"):
            source.start()
        try:
            status = source.status()
            assert status.degraded is True
            assert status.registered == 0
            assert status.failed == (str(tmp_path / "tree"),)
            assert status.last_error
            assert any("registration exhausted" in r.message for r in caplog.records)

            source.rebuild()
            recovered = source.status()
            assert recovered.degraded is False
            assert recovered.registered == 1
            assert recovered.failed == ()
        finally:
            source.close()

    def test_missing_root_is_surfaced_as_unwatchable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(inotify_module.ctypes, "get_errno", lambda: errno.ENOENT)
        source = InotifyWatchSource(
            (WatchRoot(tmp_path / "does-not-exist", is_directory=True, depth=1),)
        )
        source._lib = _FakeLib()  # type: ignore[assignment]
        source.start()
        try:
            status = source.status()
            assert status.degraded is True
            assert status.failed == (str(tmp_path / "does-not-exist"),)
        finally:
            source.close()


class TestIgnoredEventHandling:
    """IN_IGNORED from our OWN rebuild removal must not look like a loss.

    ``rebuild`` calls ``inotify_rm_watch`` for every wd; the kernel answers
    with one IN_IGNORED per removal. Treating those as registration loss made
    the coordinator re-trigger ``rebuild`` forever (the ~500 warns/s flood).
    """

    def _source(self, tmp_path: Path) -> InotifyWatchSource:
        (tmp_path / "tree").mkdir()
        source = InotifyWatchSource((WatchRoot(tmp_path / "tree", is_directory=True, depth=1),))
        source._lib = _FakeLib(fail_first=False)  # type: ignore[assignment]
        source.start()
        return source

    def test_ignored_for_untracked_watch_is_not_surfaced(self, tmp_path: Path) -> None:
        source = self._source(tmp_path)
        try:
            source._pending.clear()
            # A wd the source no longer tracks == the kernel acking OUR rm_watch.
            source._dispatch(999999, inotify_module.IN_IGNORED, "")
            assert list(source._pending) == []
        finally:
            source.close()

    def test_ignored_for_tracked_watch_is_surfaced(self, tmp_path: Path) -> None:
        source = self._source(tmp_path)
        try:
            source._pending.clear()
            wd = next(iter(source._watches))
            source._dispatch(wd, inotify_module.IN_IGNORED, "")
            assert [e.kind for e in source._pending] == [WatchEventKind.IGNORED]
            assert wd not in source._watches
        finally:
            source.close()

    def test_rebuild_never_calls_rm_watch(self, tmp_path: Path) -> None:
        """Rebuild must re-add idempotently; rm_watch would self-emit IN_IGNORED."""
        source = self._source(tmp_path)
        try:
            source.rebuild()
            source.rebuild()
            assert source._lib.rm_calls == 0  # type: ignore[attr-defined]
            assert source.status().registered >= 1
        finally:
            source.close()


class TestMissingRootWarnOnce:
    def test_missing_parent_warns_once_across_rebuilds(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        missing = tmp_path / "nope" / "desired.json"  # parent directory absent
        source = InotifyWatchSource((WatchRoot(missing, is_directory=False),))
        source._lib = _FakeLib()  # type: ignore[assignment]
        with caplog.at_level(logging.WARNING, logger="runtime.adapters.inotify_watch_source"):
            source.start()
            source.rebuild()
            source.rebuild()
        try:
            warnings = [
                r for r in caplog.records if "file root parent missing" in r.getMessage()
            ]
            assert len(warnings) == 1
            assert source.status().degraded is True
        finally:
            source.close()
