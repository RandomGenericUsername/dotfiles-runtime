"""Inner state mutex port — dependency-inversion seam for use case composition.

v3 has ONE cross-process state-mutation lock: ``ConvergeLock`` over
``state/converge.lock``. Composition roots hold it while running the use case,
so the injected inner mutex is a pass-through
(``runtime.adapters.flock_seed_mutex.PassThroughSeedMutex``). There is no
``.seed.lock`` and no second lock file.

The port remains so use cases keep no concrete adapter dependency and can be
tested with a fake. First-run seeding is a sub-step of the one convergence and
uses the same pass-through (no nested flock/deadlock); ``converge.lock``'s
kernel flock releases on process death.

Domain purity (AD-1, AD-14):
- This module is a port: ABC only, no I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager

from runtime.domain.models import SeedLockedError

__all__ = ["ISeedMutex", "SeedLockedError"]


class ISeedMutex(ABC):
    """Inner state mutex seam; production passes through the held converge lock."""

    @abstractmethod
    def hold(self, blocking: bool = False) -> AbstractContextManager[None]:
        """Acquire the mutex exclusively.

        Args:
            blocking: when ``True``, wait until the mutex is free instead of
                raising. Use for apply's read-modify-write (a concurrent seed
                is bounded work; waiting is the correct behavior). The
                default ``False`` preserves the seed contract: fail fast so
                the losing CLI invocation can skip quietly.

        Returns:
            A context manager that holds the lock for the duration of the
            ``with`` block and releases it afterwards.

        Raises:
            SeedLockedError: if another process currently holds the mutex and
                ``blocking`` is ``False``. The lock MUST be kernel-owned
                (auto-released on process death) so a crashed holder never
                wedges future seeds.
        """
