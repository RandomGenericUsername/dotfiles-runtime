"""flock-based history mutex plus the already-held pass-through mutex.

There is ONE state-mutation lock in v3: ``state_root/converge.lock``
(:class:`runtime.adapters.converge_lock.ConvergeLock`). Use cases still accept
an ``ISeedMutex`` for dependency inversion, but production wires
:class:`PassThroughSeedMutex` because the composition root already holds the
one convergence lock. No runtime command creates ``.seed.lock``.

``FlockHistoryMutex`` implements ``IHistoryMutex`` on
``state_root/.history.lock`` — blocking by default (append critical sections
are millisecond-scale). The history append lock is a separate, short-lived
critical section, not a second convergence lock.

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

from runtime.domain.models import HistoryLockError
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


class PassThroughSeedMutex(ISeedMutex):
    """No-op inner mutex for a use case already inside ``converge.lock``.

    Every production mutation composition root holds the one
    :class:`~runtime.adapters.converge_lock.ConvergeLock`. Re-acquiring the
    same kernel flock inside the composed use case would fail (non-blocking)
    or deadlock (blocking), so inner use cases are wired with this
    pass-through — the outer ``converge.lock`` is the single serialization
    point. Never use outside an already-held critical section.
    """

    def hold(self, blocking: bool = False) -> AbstractContextManager[None]:
        return _pass_through()


@contextmanager
def _pass_through() -> Generator[None]:
    """Yield immediately — the outer composition root owns the lock."""
    yield
