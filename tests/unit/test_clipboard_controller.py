"""Clipboard controller/host: lifecycle, capture, incognito, control."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from runtime.adapters.clipboard_config import JsonClipboardConfigReader
from runtime.adapters.json_history_store import JsonClipboardStore
from runtime.application.clipboard import ClipboardController
from runtime.application.clipboard_host import ClipboardHost
from runtime.domain.clipboard import ClipboardReading
from runtime.ports.clipboard import SOURCE_MODE_PROTOCOL, IClipboardSource
from runtime.ports.jobs import IControllableJobClient


class FakeJobClient(IControllableJobClient):
    def __init__(self) -> None:
        self.begun: list[tuple[str, float]] = []
        self.renewed: list[str] = []
        self.ended: list[tuple[str, int]] = []
        self.published: list[tuple[str, dict[str, object]]] = []
        self.handler: Callable[[str, str], None] | None = None

    def begin(self, kind: str, ttl: float) -> str:
        self.begun.append((kind, ttl))
        return "job-1"

    def renew(self, job_id: str) -> None:
        self.renewed.append(job_id)

    def report_progress(self, job_id: str, fraction: float) -> None:
        return None

    def end(self, job_id: str, exit_code: int) -> None:
        self.ended.append((job_id, exit_code))

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        self.published.append((topic, dict(payload)))

    def set_control_handler(self, handler: Callable[[str, str], None]) -> None:
        self.handler = handler


class FakeSource(IClipboardSource):
    def __init__(self, readings: list[ClipboardReading] | None = None) -> None:
        self.queue: deque[ClipboardReading] = deque(readings or [])
        self.started = False
        self.stopped = False
        self._mode = SOURCE_MODE_PROTOCOL

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def next_change(self, timeout: float) -> ClipboardReading | None:
        return self.queue.popleft() if self.queue else None

    @property
    def mode(self) -> str:
        return self._mode


def text_reading(text: str) -> ClipboardReading:
    return ClipboardReading(mimetypes=("text/plain",), text=text)


def build(tmp_path: Path, readings: list[ClipboardReading] | None = None):
    client = FakeJobClient()
    source = FakeSource(readings)
    store = JsonClipboardStore(
        tmp_path / "history.json",
        image_dir=tmp_path / "images",
        clock=lambda: 1000.0,
    )
    config = JsonClipboardConfigReader(tmp_path / "config.json")
    clock = [1000.0]
    controller = ClipboardController(
        client, source, store, config, clock=lambda: clock[0], renew_interval=10.0
    )
    return controller, client, source, store, clock


class TestLifecycle:
    def test_start_registers_and_announces_running(self, tmp_path: Path) -> None:
        controller, client, source, _, _ = build(tmp_path)
        assert controller.start() == "job-1"
        assert client.begun == [("clipboard", 3600.0)]
        assert source.started is True
        assert ("clipboard.state", {"state": "running", "job_id": "job-1"}) in client.published

    def test_stop_ends_job_and_source(self, tmp_path: Path) -> None:
        controller, client, source, _, _ = build(tmp_path)
        controller.start()
        controller.stop()
        assert source.stopped is True
        assert client.ended == [("job-1", 0)]
        assert controller.job_id is None
        assert client.published[-1] == ("clipboard.state", {"state": "idle", "job_id": "job-1"})

    def test_pause_resume_transitions(self, tmp_path: Path) -> None:
        controller, client, _, _, _ = build(tmp_path)
        controller.start()
        controller.pause()
        assert controller.state == "paused"
        controller.resume()
        assert controller.state == "running"
        states = [
            payload["state"]
            for topic, payload in client.published
            if topic == "clipboard.state"
        ]
        assert states == ["running", "paused", "running"]

    def test_renew_after_interval(self, tmp_path: Path) -> None:
        controller, client, _, _, clock = build(tmp_path)
        controller.start()
        clock[0] = 1005.0
        controller.tick()
        assert client.renewed == []
        clock[0] = 1011.0
        controller.tick()
        assert client.renewed == ["job-1"]


class TestCapture:
    def test_reading_is_stored_and_published(self, tmp_path: Path) -> None:
        controller, client, _, store, _ = build(tmp_path, [text_reading("hello world")])
        controller.start()
        assert controller.tick() is True
        assert [item.text for item in store.load()] == ["hello world"]
        updates = [payload for topic, payload in client.published if topic == "clipboard.update"]
        assert len(updates) == 1
        assert updates[0]["type"] == "text"
        assert updates[0]["preview"] == "hello world"
        assert updates[0]["path"] == ""
        assert len(updates[0]["hash"]) == 64

    def test_no_reading_is_noop(self, tmp_path: Path) -> None:
        controller, client, _, store, _ = build(tmp_path)
        controller.start()
        assert controller.tick() is False
        assert store.load() == []
        assert all(topic != "clipboard.update" for topic, _ in client.published)

    def test_incognito_discards_capture(self, tmp_path: Path) -> None:
        controller, client, source, store, _ = build(tmp_path)
        controller.start()
        controller.pause()
        source.queue.append(text_reading("secret"))
        controller.tick()
        assert store.load() == []
        assert all(topic != "clipboard.update" for topic, _ in client.published)

    def test_resume_captures_again(self, tmp_path: Path) -> None:
        controller, _, source, store, _ = build(tmp_path)
        controller.start()
        controller.pause()
        controller.resume()
        source.queue.append(text_reading("after"))
        controller.tick()
        assert [item.text for item in store.load()] == ["after"]


class TestControl:
    def test_control_routes_and_stop_sets_flag(self, tmp_path: Path) -> None:
        client = FakeJobClient()
        host = ClipboardHost(
            client,
            FakeSource([text_reading("x")]),
            JsonClipboardStore(tmp_path / "h.json", image_dir=tmp_path / "i"),
            JsonClipboardConfigReader(tmp_path / "c.json"),
            clock=lambda: 1.0,
        )
        host.start()
        assert client.handler is not None
        client.handler("job-1", "pause")
        assert host.state == "paused"
        client.handler("job-1", "resume")
        assert host.state == "running"
        client.handler("job-1", "stop")
        assert host.stop_requested() is True
        assert host.state == "idle"

    def test_unknown_job_is_rejected(self, tmp_path: Path) -> None:
        controller, _, _, _, _ = build(tmp_path)
        controller.start()
        with pytest.raises(RuntimeError):
            controller.control("other", "pause")

    def test_unknown_action_is_rejected(self, tmp_path: Path) -> None:
        controller, _, _, _, _ = build(tmp_path)
        controller.start()
        with pytest.raises(ValueError):
            controller.control("job-1", "explode")

    def test_degraded_serve_drains_until_stop(self, tmp_path: Path) -> None:
        controller, _, source, store, _ = build(tmp_path, [text_reading("one")])
        client = FakeJobClient()
        host = ClipboardHost(
            client,
            source,
            store,
            JsonClipboardConfigReader(tmp_path / "c.json"),
            clock=lambda: 1.0,
        )
        host.start()
        calls: list[int] = []

        def sleep(_seconds: float) -> None:
            calls.append(1)
            host.request_stop()

        host.serve(sleep=sleep)
        assert [item.text for item in store.load()] == ["one"]
        assert calls  # the loop ran and exited on the stop request
