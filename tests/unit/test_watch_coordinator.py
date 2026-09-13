"""Watch coordinator tests — coalescing, overflow, recovery, no-polling.

All transport-free: a scripted :class:`IWatchSource` fake drives the
coordinator, so no live inotify is needed (AD-40's transport contract).
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from runtime.application.watch import WatchAccumulator, WatchCoordinator, WatchTrigger
from runtime.domain.watch import WatchEvent, WatchEventKind
from runtime.ports.watch_source import IWatchSource


class _ScriptedSource(IWatchSource):
    """Deterministic fake: pops scripted events, then returns ``None``."""

    def __init__(
        self,
        events: list[WatchEvent],
        *,
        none_reads: int = 1,
        on_none: Callable[[], None] | None = None,
    ) -> None:
        self._events = list(events)
        self._none_reads = none_reads
        self._on_none = on_none
        self.started = False
        self.closed = False
        self.rebuilds = 0

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def rebuild(self) -> None:
        self.rebuilds += 1

    def read_event(self, timeout: float | None = None) -> WatchEvent | None:
        if self._events:
            return self._events.pop(0)
        if self._none_reads > 0:
            self._none_reads -= 1
            return None
        if self._on_none is not None:
            self._on_none()
        return None


def _modify() -> WatchEvent:
    return WatchEvent(kind=WatchEventKind.MODIFIED, path="/t/x")


def test_burst_coalesces_to_one_trigger() -> None:
    captured: list[WatchTrigger] = []
    source = _ScriptedSource([_modify(), _modify(), _modify()], none_reads=1)
    stop = threading.Event()

    def _none() -> None:
        stop.set()

    source._on_none = _none
    WatchCoordinator(source, captured.append).serve_events(stop)

    assert len(captured) == 1
    assert captured[0].reason == "change"
    assert captured[0].full_rescan is False
    assert source.rebuilds == 0


def test_overflow_forces_full_rescan_without_rebuild() -> None:
    captured: list[WatchTrigger] = []
    source = _ScriptedSource([WatchEvent(kind=WatchEventKind.OVERFLOW)], none_reads=1)
    stop = threading.Event()
    source._on_none = stop.set
    WatchCoordinator(source, captured.append).serve_events(stop)

    assert len(captured) == 1
    assert captured[0].reason == "overflow"
    assert captured[0].full_rescan is True
    assert source.rebuilds == 0  # overflow does not lose the watches


def test_registration_loss_rebuilds_and_rescans() -> None:
    captured: list[WatchTrigger] = []
    source = _ScriptedSource([WatchEvent(kind=WatchEventKind.IGNORED, path="/t")], none_reads=1)
    stop = threading.Event()
    source._on_none = stop.set
    WatchCoordinator(source, captured.append).serve_events(stop)

    assert source.rebuilds == 1
    assert len(captured) == 1
    assert captured[0].reason == "registration-loss"
    assert captured[0].full_rescan is True


def test_move_self_and_delete_self_are_registration_loss() -> None:
    for kind in (WatchEventKind.MOVE_SELF, WatchEventKind.DELETE_SELF):
        captured: list[WatchTrigger] = []
        source = _ScriptedSource([WatchEvent(kind=kind, path="/t")], none_reads=1)
        stop = threading.Event()
        source._on_none = stop.set
        WatchCoordinator(source, captured.append).serve_events(stop)
        assert source.rebuilds == 1
        assert captured[0].full_rescan is True


def test_idle_reads_never_trigger_or_rescan() -> None:
    """No polling: a source with no events never fires and never rescans."""
    captured: list[WatchTrigger] = []
    source = _ScriptedSource([], none_reads=5)
    stop = threading.Event()

    def _none() -> None:
        stop.set()

    source._on_none = _none
    WatchCoordinator(source, captured.append).serve_events(stop)

    assert captured == []
    assert source.rebuilds == 0


def test_handler_failure_is_recoverable() -> None:
    calls: list[int] = []

    def _handler(_trigger: WatchTrigger) -> None:
        calls.append(1)
        raise RuntimeError("converge blew up")

    source = _ScriptedSource([_modify()], none_reads=1)
    stop = threading.Event()
    source._on_none = stop.set
    WatchCoordinator(source, _handler).serve_events(stop)  # must not raise
    assert calls == [1]


class TestAccumulator:
    def test_take_without_ingest_is_none(self) -> None:
        assert WatchAccumulator().take() is None

    def test_overflow_reason_wins_over_content(self) -> None:
        acc = WatchAccumulator()
        acc.ingest(_modify())
        acc.ingest(WatchEvent(kind=WatchEventKind.OVERFLOW))
        trigger = acc.take()
        assert trigger is not None
        assert trigger.reason == "overflow"
        assert trigger.full_rescan is True

    def test_reset_after_take(self) -> None:
        acc = WatchAccumulator()
        acc.ingest(_modify())
        assert acc.take() is not None
        assert acc.take() is None


def test_no_polling_static_guard() -> None:
    """AD-40: the coordinator never sleeps or stats watched state."""
    from pathlib import Path

    import runtime.application.watch as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "time.sleep" not in source
    assert "os.stat" not in source
    assert ".stat(" not in source
    assert "rglob" not in source
    assert "os.walk" not in source
