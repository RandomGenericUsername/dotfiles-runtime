"""``wl-paste`` clipboard source — compositor events with a polled fallback (D2).

The healthy path is event-driven: ``wl-paste --watch echo`` registers with the
compositor's ``wlr-data-control`` protocol and prints one line per selection
change, so this source sleeps on a queue and consumes no CPU while idle. A
``reader`` thread drains the watch stream; each notification triggers a
``--list-types`` + content read.

When the watch process cannot start, or exits early (the HANDOFF
"data-control protocol" failure), the source transparently declares the
degraded **polling** mode and samples on demand, hash-comparing against the
previous selection so unchanged content never re-emits. The mode is exposed so
the host can report it.

Images are written to the cache directory under their content hash so a
re-copy maps to the same file. All subprocess access is injectable (``run`` /
``popen``) so the whole source is unit-testable without ``wl-clipboard``, a
compositor, or real time.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runtime.adapters.hashing import hash_file_bytes
from runtime.domain.clipboard import ClipboardReading
from runtime.ports.clipboard import (
    SOURCE_MODE_POLLING,
    SOURCE_MODE_PROTOCOL,
    IClipboardSource,
)

__all__ = ["WlPasteSource"]

logger = logging.getLogger(__name__)

#: Emits one newline per clipboard change; carries no content itself.
_WATCH_ARGV: tuple[str, ...] = ("wl-paste", "--watch", "echo")

_IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def _default_image_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "hypr-pano"


class WlPasteSource(IClipboardSource):
    """A ``wl-paste``-backed :class:`IClipboardSource`."""

    def __init__(
        self,
        *,
        run: Callable[..., Any] | None = None,
        popen: Callable[..., Any] | None = None,
        image_dir: Path | None = None,
        requested_mode: str = "auto",
        poll_interval: float = 0.5,
        timeout: float = 2.0,
    ) -> None:
        self._run = run if run is not None else subprocess.run
        self._popen = popen if popen is not None else subprocess.Popen
        self._image_dir = image_dir if image_dir is not None else _default_image_dir()
        self._requested_mode = requested_mode
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._mode = SOURCE_MODE_PROTOCOL if requested_mode == "auto" else requested_mode
        self._process: Any | None = None
        self._reader: threading.Thread | None = None
        self._signals: queue.Queue[bool] = queue.Queue(maxsize=16)
        self._running = False
        self._last_hash: str | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._running = True
        if self._requested_mode == SOURCE_MODE_POLLING:
            self._mode = SOURCE_MODE_POLLING
            logger.info("clipboard source: polling mode (requested)")
            return
        try:
            self._process = self._popen(
                list(_WATCH_ARGV),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            self._degrade(f"watch spawn failed: {exc}")
            return
        self._mode = SOURCE_MODE_PROTOCOL
        self._reader = threading.Thread(target=self._read_watch_stream, daemon=True)
        self._reader.start()
        logger.info("clipboard source: protocol mode (wlr-data-control watch)")

    def stop(self) -> None:
        self._running = False
        process, self._process = self._process, None
        if process is not None:
            try:
                process.terminate()
            except Exception:
                logger.debug("clipboard source: terminate failed", exc_info=True)
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            reader.join(timeout=1.0)

    def _read_watch_stream(self) -> None:
        """Drain the watch process; on early EOF declare the degraded mode."""
        process = self._process
        if process is None or process.stdout is None:
            self._degrade("watch emitted no stream")
            return
        while self._running:
            line = process.stdout.readline()
            if line == "":
                if self._running:
                    detail = ""
                    if process.stderr is not None:
                        try:
                            detail = (process.stderr.read() or "").strip()
                        except Exception:
                            detail = ""
                    self._degrade(f"watch exited early{f': {detail}' if detail else ''}")
                return
            try:
                self._signals.put_nowait(True)
            except queue.Full:
                # A burst of changes already has a pending notification; the
                # content read is a snapshot, so coalescing is correct.
                pass

    def _degrade(self, reason: str) -> None:
        """Switch to polling mode (declared degraded fallback)."""
        if self._mode != SOURCE_MODE_POLLING:
            logger.warning("clipboard source: %s; falling back to polling mode", reason)
        self._mode = SOURCE_MODE_POLLING

    # ── Reads ────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._mode

    def next_change(self, timeout: float) -> ClipboardReading | None:
        """Return the next changed reading, or ``None`` (see the port doc)."""
        if self._mode == SOURCE_MODE_PROTOCOL:
            try:
                self._signals.get(timeout=timeout)
            except queue.Empty:
                return None
        else:
            # Polling: a single sample per call. Sleep a little so a tight
            # caller does not spin; the host's serve loop supplies cadence.
            if timeout > 0:
                time.sleep(min(timeout, self._poll_interval))
        reading = self._build_reading()
        if reading is None:
            return None
        digest = hash_file_bytes(reading.canonical_bytes())
        if digest == self._last_hash:
            return None
        self._last_hash = digest
        return reading

    def _build_reading(self) -> ClipboardReading | None:
        types = self._list_types()
        if not types:
            return None
        image_mime = next((mime for mime in types if mime.startswith("image/")), None)
        if image_mime is not None:
            data = self._read_image(image_mime)
            if not data:
                return None
            path = self._store_image(data, image_mime)
            if path is None:
                return None
            return ClipboardReading(
                mimetypes=tuple(types),
                image_bytes=data,
                image_mime=image_mime,
                image_path=path,
            )
        text = self._read_text()
        if text is None:
            return None
        return ClipboardReading(mimetypes=tuple(types), text=text)

    # ── wl-clipboard calls ───────────────────────────────────────────

    def _list_types(self) -> list[str]:
        try:
            result = self._run(
                ["wl-paste", "--list-types"],
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("clipboard source: list-types failed: %s", exc)
            return []
        if result.returncode != 0:
            return []
        return [line for line in (result.stdout or "").splitlines() if line]

    def _read_text(self) -> str | None:
        try:
            result = self._run(
                ["wl-paste", "--no-newline"],
                capture_output=True,
                timeout=self._timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("clipboard source: read text failed: %s", exc)
            return None
        if result.returncode != 0:
            return None
        data = result.stdout or b""
        if isinstance(data, str):
            return data
        return data.decode("utf-8", errors="replace")

    def _read_image(self, mime: str) -> bytes | None:
        try:
            result = self._run(
                ["wl-paste", "--type", mime],
                capture_output=True,
                timeout=self._timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("clipboard source: read image failed: %s", exc)
            return None
        if result.returncode != 0:
            return None
        data = result.stdout or b""
        if isinstance(data, str):
            data = data.encode("utf-8", errors="replace")
        return data or None

    def _store_image(self, data: bytes, mime: str) -> str | None:
        suffix = _IMAGE_SUFFIXES.get(mime, ".png")
        filename = f"{hash_file_bytes(data)}{suffix}"
        path = self._image_dir / filename
        try:
            self._image_dir.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(data)
        except OSError as exc:
            logger.warning("clipboard source: cannot cache image %s: %s", path, exc)
            return None
        return str(path)
