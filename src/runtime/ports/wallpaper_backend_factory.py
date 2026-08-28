from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.models import BackendType
from runtime.ports.wallpaper_backend import IStaticWallpaperBackend, IVideoWallpaperBackend


class IWallpaperBackendFactory(ABC):
    @abstractmethod
    def create_static(self, backend_type: BackendType) -> IStaticWallpaperBackend:
        """Create a static wallpaper backend by BackendType enum."""

    @abstractmethod
    def create_video(self, backend_type: BackendType) -> IVideoWallpaperBackend:
        """Create a video wallpaper backend by BackendType enum."""

    @abstractmethod
    def auto_detect(self, source_path: str) -> BackendType:
        """Detect backend type from file extension.

        Returns: BackendType enum (e.g., BackendType.MPVAPER, BackendType.SWWW).
        """
