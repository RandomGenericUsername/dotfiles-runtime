from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from runtime.domain.models import FitMode


class IStaticWallpaperBackend(ABC):
    @abstractmethod
    def set_image(self, path: str, monitor: str, fit_mode: FitMode) -> None:
        """Set a static image as wallpaper on the given monitor."""

    @abstractmethod
    def set_color(self, color: str, monitor: str) -> None:
        """Set a solid color as wallpaper on the given monitor."""

    @abstractmethod
    def reload(self) -> None:
        """Reload the wallpaper backend (e.g., hyprctl reload)."""


class IVideoWallpaperBackend(ABC):
    @abstractmethod
    def set_video(
        self,
        path: str,
        monitor: str,
        mpv_options: str | None,
        ipc_socket: str | None,
        auto_pause: bool,
        auto_stop: bool,
        layer: Literal["background", "bottom", "top", "overlay"],
    ) -> None:
        """Set a video as wallpaper on the given monitor."""

    @abstractmethod
    def set_playlist(
        self,
        paths: list[str],
        monitor: str,
        mpv_options: str | None,
    ) -> None:
        """Set a video playlist as wallpaper."""

    @abstractmethod
    def control(self, monitor: str, command: Literal["pause", "resume", "next", "prev", "stop"]) -> None:
        """Send a control command (pause, resume, next, etc.)."""
