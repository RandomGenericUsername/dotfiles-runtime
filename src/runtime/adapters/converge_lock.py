"""The one v3 convergence lock: a blocking kernel ``flock``.

The daemon job queue and the CLI fallback both run under this exact file. There
is no bounded retry loop, no sleep, and no second seed/appearance lock: a
process death releases the kernel lock, which is the recovery mechanism.

Ordinary convergence blocks until the lock is free. A GUI-facing command that
must fail fast (``icons regenerate``) opts into ``blocking=False`` and receives
:class:`ConvergeLockBusyError` instead of waiting; it still writes through this
same lock, never a second file.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from runtime.adapters.generation_store import CONVERGE_LOCK_FILENAME

__all__ = [
    "ConvergeLock",
    "ConvergeLockBusyError",
    "converge_lock_path",
]


class ConvergeLockBusyError(RuntimeError):
    """The one convergence lock is held and the caller asked not to wait."""


def converge_lock_path(state_root: Path) -> Path:
    return state_root / CONVERGE_LOCK_FILENAME


class ConvergeLock:
    """Exclusive ``flock`` over ``state/converge.lock``."""

    def __init__(self, state_root: Path) -> None:
        self._path = converge_lock_path(state_root)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def hold(self, *, blocking: bool = True) -> Iterator[None]:
        """Hold the lock, or raise :class:`ConvergeLockBusyError`.

        Args:
            blocking: ``True`` (default) waits for every other convergence to
                finish. ``False`` fails fast when the lock is held; this is the
                GUI ``icons regenerate`` busy contract.

        The lock is released on context exit or by the kernel when the process
        dies, so a crashed holder never wedges future commands.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        acquired = False
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except BlockingIOError as exc:
                raise ConvergeLockBusyError(
                    f"convergence already in progress (lock held): {self._path}"
                ) from exc
            acquired = True
            yield
        finally:
            if acquired:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass
