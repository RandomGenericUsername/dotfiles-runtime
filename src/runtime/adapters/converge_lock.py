"""The one v3 convergence lock: a blocking kernel ``flock``.

The daemon job queue and the CLI fallback both run under this exact file. There
is no bounded retry loop, no sleep, and no second seed/appearance lock: a
process death releases the kernel lock, which is the recovery mechanism.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from runtime.adapters.generation_store import CONVERGE_LOCK_FILENAME

__all__ = ["ConvergeLock", "converge_lock_path"]


def converge_lock_path(state_root: Path) -> Path:
    return state_root / CONVERGE_LOCK_FILENAME


class ConvergeLock:
    """Blocking exclusive ``flock`` over ``state/converge.lock``."""

    def __init__(self, state_root: Path) -> None:
        self._path = converge_lock_path(state_root)

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Block until the lock is held, then yield; released on exit/death."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._path), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        acquired = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
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
