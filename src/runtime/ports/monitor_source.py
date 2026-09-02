from __future__ import annotations

from abc import ABC, abstractmethod


class IMonitorSource(ABC):
    @abstractmethod
    def detect_monitors(self) -> list[str]:
        """Return the ordered list of active output names available for
        wallpaper targeting (e.g. ``["eDP-1", "DP-3"]``).

        An empty list means detection is unavailable (no compositor, no
        binary, transient failure); callers fall back to their documented
        default monitor instead of failing. The order is deterministic.
        """
