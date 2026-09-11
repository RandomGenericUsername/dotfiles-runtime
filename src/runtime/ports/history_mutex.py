"""History mutex port — exclusive cross-process guard for history appends.

Protects the append-only ``history.jsonl`` invariant (AD-31): exactly one
process may heal/append per state_root at a time, so concurrent
``wallpaper set`` + ``reconcile`` serialize instead of interleaving or
losing lines. Blocking by default: the append critical section is
millisecond-scale, so waiting is correct — failing a CLI command over a
momentarily held lock would be wrong UX.

Domain purity (AD-1, AD-14):
- This module is a port: ABC only, no I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager

__all__ = ["IHistoryMutex"]


class IHistoryMutex(ABC):
    """Exclusive, crash-safe, cross-process mutex for history critical sections."""

    @abstractmethod
    def hold(self, blocking: bool = True) -> AbstractContextManager[None]:
        """Acquire the mutex exclusively.

        Args:
            blocking: when ``True`` (default), wait until the mutex is free
                instead of raising. The append critical section is bounded
                millisecond-scale work, so waiting is the correct behavior.

        Returns:
            A context manager that holds the lock for the duration of the
            ``with`` block and releases it afterwards.

        Raises:
            HistoryLockError: if the lock cannot be acquired (non-blocking
                contention) or used (lock-file I/O failure). The lock MUST
                be kernel-owned (auto-released on process death) so a crashed
                holder never wedges future appends.
        """
