from __future__ import annotations

from abc import ABC, abstractmethod


class IDesktopReloader(ABC):
    @abstractmethod
    def reload(self) -> bool:
        """Reload the desktop consumer. Returns True on success."""
