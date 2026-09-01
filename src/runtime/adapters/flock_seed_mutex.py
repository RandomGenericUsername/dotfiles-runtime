"""flock-based state mutex adapter.

Implements ``ISeedMutex`` using ``fcntl.flock`` on
``state_root/.seed.lock`` — non-blocking (``LOCK_EX | LOCK_NB``, raises
``SeedLockedError``) by default, blocking (``LOCK_EX``) when requested for
apply's read-modify-write. flock is kernel-owned: the lock is released
automatically when the holding process exits or dies — no stale-lockfile
cleanup logic is needed, which is why it is preferred over PID-file or
mkdir-based locks here.

Domain purity (AD-1, AD-14):
- This module lives in ``adapters/`` (allowed ``os``/``fcntl``/``pathlib``).
- ``domain/`` stays pure.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from runtime.ports.seed_mutex import ISeedMutex, SeedLockedError


class FlockSeedMutex(ISeedMutex):
    """Exclusive seeding mutex backed by a flock'd lock file."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path

    def hold(self, blocking: bool = False) -> AbstractContextManager[None]:
        return self._acquire(blocking)

    @contextmanager
    def _acquire(self, blocking: bool) -> Generator[None]:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self._lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            try:
                fcntl.flock(fd, flags)
            except OSError as exc:
                raise SeedLockedError(
                    f"seeding already in progress (lock held): {self._lock_path}"
                ) from exc
            try:
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            os.close(fd)
