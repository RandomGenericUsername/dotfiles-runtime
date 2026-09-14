"""``wl-paste`` source: protocol stream, polling fallback, dedupe, images."""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any

from runtime.adapters.wl_paste_source import WlPasteSource
from runtime.ports.clipboard import SOURCE_MODE_POLLING, SOURCE_MODE_PROTOCOL


class FakeResult:
    def __init__(self, stdout: Any = b"", returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


class FakeRunner:
    """Answers ``wl-paste`` argv by subcommand."""

    def __init__(self) -> None:
        self.types = "text/plain\n"
        self.text: bytes | str = b"hello"
        self.image = b"\x89PNG-data"

    def __call__(self, argv: list[str], **_kw: Any) -> FakeResult:
        if "--list-types" in argv:
            return FakeResult(stdout=self.types, returncode=0 if self.types else 1)
        if "--no-newline" in argv:
            return FakeResult(stdout=self.text)
        if "--type" in argv:
            return FakeResult(stdout=self.image)
        return FakeResult(returncode=1)


class FakeProcess:
    """A watch process whose stdout yields queued lines then blocks (or EOFs)."""

    def __init__(self, lines: list[str] | None = None, *, eof: bool = False) -> None:
        self._lines = list(lines or [])
        self._eof = eof
        self._gate = threading.Event()
        self._closed = threading.Event()
        self.stdout = self
        self.stderr = io.StringIO("")
        self.terminated = False

    def readline(self) -> str:
        while not self._lines:
            if self._closed.is_set():
                return ""
            if self._eof:
                return ""
            self._gate.wait(0.02)
        self._gate.clear()
        return self._lines.pop(0)

    def feed(self, line: str) -> None:
        self._lines.append(line)
        self._gate.set()

    def terminate(self) -> None:
        self.terminated = True
        self._closed.set()
        self._gate.set()


def source(
    runner: FakeRunner,
    tmp_path: Path,
    *,
    process: FakeProcess | None = None,
    requested_mode: str = "auto",
) -> tuple[WlPasteSource, list[FakeProcess]]:
    spawned: list[FakeProcess] = []

    def popen(_argv: list[str], **_kw: Any) -> FakeProcess:
        proc = process if process is not None else FakeProcess(eof=True)
        spawned.append(proc)
        return proc

    return (
        WlPasteSource(
            run=runner,
            popen=popen,
            image_dir=tmp_path / "images",
            requested_mode=requested_mode,
        ),
        spawned,
    )


class TestProtocolMode:
    def test_event_triggers_a_reading(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        proc = FakeProcess(lines=["change\n"], eof=False)
        src, _ = source(runner, tmp_path, process=proc)
        src.start()
        assert src.mode == SOURCE_MODE_PROTOCOL
        reading = src.next_change(timeout=1.0)
        assert reading is not None
        assert reading.text == "hello"
        src.stop()
        assert proc.terminated

    def test_no_event_returns_none_on_timeout(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        proc = FakeProcess(lines=[], eof=False)
        src, _ = source(runner, tmp_path, process=proc)
        src.start()
        assert src.next_change(timeout=0.05) is None
        src.stop()

    def test_watch_early_exit_degrades_to_polling(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        proc = FakeProcess(eof=True)
        src, _ = source(runner, tmp_path, process=proc)
        src.start()
        deadline = time.monotonic() + 1.0
        while src.mode != SOURCE_MODE_POLLING and time.monotonic() < deadline:
            time.sleep(0.01)
        assert src.mode == SOURCE_MODE_POLLING

    def test_spawn_failure_degrades_to_polling(self, tmp_path: Path) -> None:
        runner = FakeRunner()

        def boom(*_a: Any, **_k: Any) -> Any:
            raise OSError("no wl-paste")

        src = WlPasteSource(run=runner, popen=boom, image_dir=tmp_path / "images")
        src.start()
        assert src.mode == SOURCE_MODE_POLLING


class TestPollingMode:
    def test_dedupes_unchanged_content(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        src, _ = source(runner, tmp_path, requested_mode=SOURCE_MODE_POLLING)
        src.start()
        first = src.next_change(timeout=0.0)
        assert first is not None and first.text == "hello"
        assert src.next_change(timeout=0.0) is None

    def test_changed_content_re_emits(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        src, _ = source(runner, tmp_path, requested_mode=SOURCE_MODE_POLLING)
        src.start()
        assert src.next_change(timeout=0.0) is not None
        runner.text = b"second"
        second = src.next_change(timeout=0.0)
        assert second is not None and second.text == "second"

    def test_empty_clipboard_is_none(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.types = ""
        src, _ = source(runner, tmp_path, requested_mode=SOURCE_MODE_POLLING)
        src.start()
        assert src.next_change(timeout=0.0) is None


class TestImages:
    def test_image_is_cached_by_hash(self, tmp_path: Path) -> None:
        runner = FakeRunner()
        runner.types = "image/png\n"
        src, _ = source(runner, tmp_path, requested_mode=SOURCE_MODE_POLLING)
        src.start()
        reading = src.next_change(timeout=0.0)
        assert reading is not None
        assert reading.image_mime == "image/png"
        assert reading.image_path is not None
        cached = Path(reading.image_path)
        assert cached.exists()
        assert cached.read_bytes() == runner.image
        # Re-copying identical bytes maps to the same file (hash-named).
        src._last_hash = None
        again = src.next_change(timeout=0.0)
        assert again is not None
        assert again.image_path == reading.image_path
