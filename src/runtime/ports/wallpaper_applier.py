from __future__ import annotations

from abc import ABC, abstractmethod


class IWallpaperApplier(ABC):
    """Apply the wallpaper selected by the runtime's current state."""

    @abstractmethod
    def apply(self) -> bool:
        """Apply the current wallpaper. Returns True on success."""
