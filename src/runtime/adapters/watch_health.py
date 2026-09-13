"""Persisted watch-set health under ``state_root`` (AD-40/AD-41, P5 follow-up).

The daemon's inotify registrations can be dropped by capacity exhaustion
(``max_user_watches`` / ``max_user_instances`` → ``ENOSPC``/``EMFILE``). The
live source exposes that as :class:`~runtime.domain.watch.WatchStatus`; this
adapter persists the latest status so the **daemon-independent** read-only
surface (``inspect daemon`` / ``doctor``) can report a degraded watch set
without talking to the daemon or changing any D-Bus contract.

Like the last-converged backstop it lives under ``state_root`` — an output
location, never a watched root — and is written atomically (tmp + fsync +
``os.replace``). A symlinked record is refused (no-follow) and read as
``None``; absent/corrupt likewise reads as ``None`` ("unknown"), never fatal.
"""

from __future__ import annotations

import errno
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from runtime.domain.watch import WatchStatus

logger = logging.getLogger(__name__)

WATCH_HEALTH_FILENAME = "watch-health.json"
WATCH_HEALTH_VERSION = 1


@dataclass(frozen=True, slots=True)
class WatchHealthRecord:
    """One persisted snapshot of the daemon's watch-set health."""

    degraded: bool
    failed_roots: tuple[str, ...]
    last_error: str | None
    updated_at: str | None


def _now_iso_z() -> str:
    """Current UTC time as strict ISO-8601 ending with ``Z``."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class WatchHealthStore:
    """Read/write the daemon's last reported watch-set health under ``state_root``."""

    def __init__(self, state_root: Path) -> None:
        self._path = state_root / WATCH_HEALTH_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def read(self) -> WatchHealthRecord | None:
        """Return the persisted record, or ``None`` when absent/corrupt.

        Read-only and never fatal: any unreadable/corrupt/symlinked record is
        reported as unknown (``None``) rather than raising.
        """
        if self._path.is_symlink():
            logger.warning("watch-health is a symlink (refusing to follow): %s", self._path)
            return None
        try:
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            return None
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                logger.warning("watch-health is a symlink (refusing to follow): %s", self._path)
                return None
            logger.warning("watch-health unreadable; treating as unknown: %s: %s", self._path, exc)
            return None
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                raw = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("watch-health unreadable; treating as unknown: %s: %s", self._path, exc)
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("watch-health corrupt; treating as unknown: %s", self._path)
            return None
        if not isinstance(data, dict):
            return None
        record = cast("dict[str, Any]", data)
        failed = record.get("failed_roots")
        if not isinstance(failed, list) or not all(isinstance(item, str) for item in failed):
            logger.warning("watch-health shape invalid; treating as unknown: %s", self._path)
            return None
        degraded = record.get("degraded")
        if not isinstance(degraded, bool):
            return None
        last_error = record.get("last_error")
        if last_error is not None and not isinstance(last_error, str):
            return None
        updated_at = record.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            return None
        return WatchHealthRecord(
            degraded=degraded,
            failed_roots=tuple(failed),
            last_error=last_error,
            updated_at=updated_at,
        )

    def write(self, status: WatchStatus) -> None:
        """Atomically persist ``status`` (tmp + fsync + replace)."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "version": WATCH_HEALTH_VERSION,
            "degraded": status.degraded,
            "failed_roots": list(status.failed),
            "last_error": status.last_error,
            "updated_at": _now_iso_z(),
        }
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
