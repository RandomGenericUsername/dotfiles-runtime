"""Persisted last-converged input-hash backstop (AD-36).

The backstop lives **under ``state_root``** — an output location, never a
watched root — so persisting it can never re-trigger the watcher. It is a
small JSON record ``{"version": 1, "input_hash": "<hex|sentinel>"}``.

Absent or corrupt backstop ⇒ ``None`` ("treat as changed"): the daemon then
converges and rewrites it. A symlinked record is refused (no-follow, mirrors
the desired/history/meta symlink policy) and read as ``None``. Writes are
atomic (tmp + ``os.replace``) so a crash mid-write never yields a torn
record — a torn record would merely read as corrupt and re-converge.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

BACKSTOP_FILENAME = "last-converged.json"
BACKSTOP_VERSION = 1


class LastConvergedBackstop:
    """Read/write the persisted last-converged input hash under ``state_root``."""

    def __init__(self, state_root: Path) -> None:
        self._path = state_root / BACKSTOP_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> str | None:
        """Return the persisted input hash, or ``None`` when absent/corrupt."""
        if self._path.is_symlink():
            logger.warning("backstop is a symlink (refusing to follow): %s", self._path)
            return None
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                logger.warning("backstop is a symlink (refusing to follow): %s", self._path)
                return None
            logger.warning("backstop unreadable; treating as changed: %s: %s", self._path, exc)
            return None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("backstop unreadable; treating as changed: %s: %s", self._path, exc)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("backstop corrupt; treating as changed: %s", self._path)
            return None
        if not isinstance(data, dict) or data.get("version") != BACKSTOP_VERSION:
            logger.warning("backstop shape invalid; treating as changed: %s", self._path)
            return None
        value = data.get("input_hash")
        if not isinstance(value, str) or not value:
            logger.warning("backstop input_hash invalid; treating as changed: %s", self._path)
            return None
        return value

    def write(self, input_hash: str) -> None:
        """Atomically persist ``input_hash`` (tmp + fsync + ``os.replace``)."""
        if not isinstance(input_hash, str) or not input_hash:
            raise ValueError(f"input_hash must be a non-empty string, got {input_hash!r}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {"version": BACKSTOP_VERSION, "input_hash": input_hash}
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        tmp = self._path.with_name(f".{self._path.name}.tmp-{os.getpid()}")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o644)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(line)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                pass
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
