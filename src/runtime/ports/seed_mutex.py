"""Seed mutex port — exclusive cross-process guard for first-run seeding.

Protects the once-only seeding invariant (AD-11): exactly one process may
perform first-run seeding per state_root. Acquire before the
``load_current()`` double-check inside ``SeedCacheUseCase.run()`` so that
concurrent CLI invocations serialize instead of double-seeding.

Domain purity (AD-1, AD-14):
- This module is a port: ABC only, no I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager

from runtime.domain.models import SeedLockedError

__all__ = ["ISeedMutex", "SeedLockedError"]


class ISeedMutex(ABC):
    """Exclusive, crash-safe, cross-process mutex for seeding."""

    @abstractmethod
    def hold(self) -> AbstractContextManager[None]:
        """Acquire the mutex exclusively.

        Returns:
            A context manager that holds the lock for the duration of the
            ``with`` block and releases it afterwards.

        Raises:
            SeedLockedError: if another process currently holds the mutex.
                The lock MUST be kernel-owned (auto-released on process
                death) so a crashed holder never wedges future seeds.
        """
