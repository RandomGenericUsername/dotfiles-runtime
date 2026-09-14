"""Bus-available smoke test for the clipboard host (D1).

Skips gracefully when no session bus exists. With a live bus the host must
either register with a running daemon or degrade to the local (no-hub)
client — never raise, never crash-loop. A fake source/store means no live
compositor or history is touched, and the stop event is pre-set so the
serving loop exits immediately.
"""

from __future__ import annotations

import threading
from collections import deque

import pytest

from runtime.application.clipboard_host import ClipboardHost
from runtime.cli.main import _build_clipboard_client
from runtime.domain.clipboard import ClipboardReading, RetentionLimits
from runtime.ports.clipboard import SOURCE_MODE_POLLING, IClipboardConfigReader, IClipboardSource
from runtime.ports.jobs import IControllableJobClient


class _FakeSource(IClipboardSource):
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def next_change(self, timeout: float) -> ClipboardReading | None:
        return None

    @property
    def mode(self) -> str:
        return SOURCE_MODE_POLLING


class _FakeStore:
    def __init__(self) -> None:
        self.items: deque = deque()

    def load(self):  # type: ignore[no-untyped-def]
        return list(self.items)

    def add(self, item):  # type: ignore[no-untyped-def]
        self.items.appendleft(item)
        return item

    def delete(self, item_hash: str) -> bool:
        return False

    def set_favorite(self, item_hash: str, favorite: bool) -> bool:
        return False

    def evict(self, limits: RetentionLimits) -> list:  # type: ignore[type-arg]
        return []


class _FakeConfig(IClipboardConfigReader):
    def read(self) -> RetentionLimits:
        return RetentionLimits()


def _bus_available() -> bool:
    try:
        from jeepney.io.blocking import open_dbus_connection

        conn = open_dbus_connection(bus="SESSION")
    except Exception:
        return False
    try:
        conn.close()
    except Exception:  # pragma: no cover - best-effort teardown
        pass
    return True


def test_client_selection_never_raises_without_a_bus() -> None:
    """Graceful degradation: selecting the client is total, bus or not."""
    client = _build_clipboard_client()
    assert isinstance(client, IControllableJobClient)


def test_clipboard_host_degrades_or_registers_on_a_live_bus() -> None:
    if not _bus_available():
        pytest.skip("no session bus available")

    source = _FakeSource()
    store = _FakeStore()
    stop_event = threading.Event()
    stop_event.set()  # exit the serving loop immediately
    client = _build_clipboard_client()
    host = ClipboardHost(
        client,
        source,
        store,  # type: ignore[arg-type]
        _FakeConfig(),
        clock=lambda: 1000.0,
        stop_event=stop_event,
    )
    try:
        host.start()
    finally:
        host.stop()
        if hasattr(client, "close"):
            client.close()
    assert source.started is True
    assert source.stopped is True
