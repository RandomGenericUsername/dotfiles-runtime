from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from runtime.domain.models import BackendType
from runtime.ports.wallpaper_backend import IStaticWallpaperBackend, IVideoWallpaperBackend


class IWallpaperBackendFactory(ABC):
    @abstractmethod
    def create_static(
        self,
        backend_type: Literal[BackendType.hyprpaper, BackendType.swaybg, BackendType.swww],
    ) -> IStaticWallpaperBackend:
        """Create a static wallpaper backend by BackendType enum."""

    @abstractmethod
    def create_video(
        self,
        backend_type: Literal[BackendType.mpvpaper],
    ) -> IVideoWallpaperBackend:
        """Create a video wallpaper backend by BackendType enum."""

    @abstractmethod
    def auto_detect(self, source_path: str) -> BackendType | None:
        """Detect backend type from file extension.

        Returns: BackendType enum (e.g., BackendType.MPVAPER, BackendType.SWWW),
        or None if the extension is unrecognized.
        """
