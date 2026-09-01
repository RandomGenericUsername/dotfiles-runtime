"""State mutex port — exclusive cross-process guard for state-creation critical sections.

Protects the once-only seeding invariant (AD-11): exactly one process may
perform first-run seeding per state_root. Acquire before the
``load_current()`` double-check inside ``SeedCacheUseCase.run()`` so that
concurrent CLI invocations serialize instead of double-seeding.

The same mutex also serializes ``ApplyWallpaperUseCase``'s read-modify-write
of ``current.json`` against a concurrently running seed (Story 1.13 review):
apply holds it blocking around load→save, so a first-run seed can never be
overtaken by an applier that observed absent state.

Domain purity (AD-1, AD-14):
- This module is a port: ABC only, no I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager

from runtime.domain.models import SeedLockedError

__all__ = ["ISeedMutex", "SeedLockedError"]


class ISeedMutex(ABC):
    """Exclusive, crash-safe, cross-process mutex for state critical sections."""

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
