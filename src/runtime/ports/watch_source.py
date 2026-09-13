"""Watch-source port (pure) — the daemon's file-event seam.

The transport is inotify (AD-40); tests inject a fake implementing this
protocol, so the coalescing/recovery/backstop logic is exercisable with no
live inotify and no polling. The daemon never stats the watched roots: it
consumes events from this source only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.watch import WatchEvent


class IWatchSource(ABC):
    """A blocking, event-driven source over the AD-39 watch-root set."""

    @abstractmethod
    def start(self) -> None:
        """Install the watches over the enumerated roots."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the watches and any transport resources (idempotent)."""

    @abstractmethod
    def rebuild(self) -> None:
        """Re-establish the watches after a registration loss (AD-40)."""

    @abstractmethod
    def read_event(self, timeout: float | None = None) -> WatchEvent | None:
        """Block up to ``timeout`` seconds; return the next event or ``None``.

        ``None`` means "no event within the window" — the caller uses it as
        the debounce-quiet signal. It is NOT a poll of watched state.
        """
