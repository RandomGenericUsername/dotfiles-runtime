"""flock-based state mutex adapters.

``FlockSeedMutex`` implements ``ISeedMutex`` on ``state_root/.seed.lock`` —
non-blocking by default, blocking when requested for apply's
read-modify-write. ``FlockHistoryMutex`` implements ``IHistoryMutex`` on
``state_root/.history.lock`` — blocking by default (append critical
sections are millisecond-scale). Both share :func:`_flock_hold`: one flock
implementation, two files, two error types (Story 4.6, AC 3).

flock is kernel-owned: the lock is released automatically when the holding
process exits or dies — no stale-lockfile cleanup logic is needed, which is
why it is preferred over PID-file or mkdir-based locks here.

Domain purity (AD-1, AD-14):
- This module lives in ``adapters/`` (allowed ``os``/``fcntl``/``pathlib``).
- ``domain/`` stays pure.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

from runtime.domain.models import HistoryLockError, SeedLockedError
from runtime.ports.history_mutex import IHistoryMutex
from runtime.ports.seed_mutex import ISeedMutex


def _flock_hold(
    lock_path: Path,
    blocking: bool,
    make_error: Callable[[Path], Exception],
    mode: int = 0o600,
) -> AbstractContextManager[None]:
    """Shared kernel-flock acquisition (single implementation, AC 3)."""
    return _acquire(lock_path, blocking, make_error, mode)


@contextmanager
def _acquire(
    lock_path: Path,
    blocking: bool,
    make_error: Callable[[Path], Exception],
    mode: int,
) -> Generator[None]:
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise make_error(lock_path) from exc
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, mode)
    except OSError as exc:
        raise make_error(lock_path) from exc
    try:
        flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            raise make_error(lock_path) from exc
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


class FlockSeedMutex(ISeedMutex):
    """Exclusive seeding mutex backed by a flock'd lock file."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path

    def hold(self, blocking: bool = False) -> AbstractContextManager[None]:
        return _flock_hold(
            self._lock_path,
            blocking,
            lambda p: SeedLockedError(f"seeding already in progress (lock held): {p}"),
        )


class FlockHistoryMutex(IHistoryMutex):
    """Exclusive history-append mutex backed by a flock'd lock file."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path

    def hold(self, blocking: bool = True) -> AbstractContextManager[None]:
        return _flock_hold(
            self._lock_path,
            blocking,
            lambda p: HistoryLockError(f"history lock acquire failed: {p}"),
        )
