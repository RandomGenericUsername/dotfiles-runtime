"""Bus-available smoke test for the capture host (5-4 follow-up N1).

Skips gracefully when no session bus exists. With a live bus the host must
either register with a running daemon or degrade to the local (no-hub)
client — never raise, never crash-loop. A fake recorder means no live
recorder is ever spawned, and the stop event is pre-set so the serving loop
exits immediately.
"""

from __future__ import annotations

import threading

import pytest

from runtime.application.capture_host import CaptureHost
from runtime.cli.main import _build_capture_client
from runtime.ports.jobs import IControllableJobClient, IRecorderProcess


class _FakeRecorder(IRecorderProcess):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def stop(self) -> None:
        self.calls.append("stop")

    def is_running(self) -> bool:
        return "start" in self.calls and "stop" not in self.calls


class _Clock:
    def __call__(self) -> float:
        return 1000.0


def _bus_available() -> bool:
    """True when a session bus can be opened (best-effort, closes it)."""
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
    """Graceful degradation: building the client is total, bus or not."""
    client = _build_capture_client()
    assert isinstance(client, IControllableJobClient)


def test_capture_host_degrades_or_registers_on_a_live_bus() -> None:
    if not _bus_available():
        pytest.skip("no session bus available")

    from runtime.adapters.dbus_job_client import DbusJobClient

    recorder = _FakeRecorder()
    client = _build_capture_client()
    stop_event = threading.Event()
    host = CaptureHost(client, recorder, clock=_Clock(), stop_event=stop_event)
    try:
        host.start()
        # Exercise immediate graceful teardown on whichever arm the probe
        # selected (D-Bus job registration or local reduced mode).
        stop_event.set()
        if isinstance(client, DbusJobClient):
            client.serve(host.stop_requested, tick=host.tick, timeout=0.01)
        else:
            host.wait()
    finally:
        host.stop()
        if isinstance(client, DbusJobClient):
            client.close()

    assert recorder.calls == ["start", "stop"]
