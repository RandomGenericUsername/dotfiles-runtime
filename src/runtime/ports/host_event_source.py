"""Host-event source port (pure) — the daemon's compositor-event seam.

The transport (Hyprland socket2) lives in ``adapters/``. Tests inject a fake
implementing this protocol, so debounce/trigger behavior is exercisable with no
live compositor and no polling.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.host_events import HostEvent
from runtime.domain.watch import WatchStatus


class IHostEventSource(ABC):
    """A blocking, event-driven source over a host compositor's event stream."""

    @abstractmethod
    def start(self) -> None:
        """Open the event stream; unavailable is recorded, never raised."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the stream and wake a blocked read (idempotent)."""

    @abstractmethod
    def read_event(self, timeout: float | None = None) -> HostEvent | None:
        """Block up to ``timeout`` seconds; return the next event or ``None``.

        ``None`` means "no event within the window" — the caller uses it as the
        debounce-quiet signal. It is NOT a poll of host state.
        """

    def status(self) -> WatchStatus:
        """Report source health; an unavailable socket is degraded."""
        return WatchStatus()
