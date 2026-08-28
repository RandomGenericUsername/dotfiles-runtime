from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from runtime.domain.models import FitMode

# ── runtime guards (Literal/FitMode are static-only; adapters must validate at runtime) ──
_VALID_LAYERS = frozenset({"background", "bottom", "top", "overlay"})
_VALID_COMMANDS = frozenset({"pause", "resume", "next", "prev", "stop"})


def _validate_fit_mode(value: object) -> FitMode:
    if isinstance(value, FitMode):
        return value
    raise ValueError(f"invalid fit_mode {value!r}: expected FitMode enum")


def _validate_layer(value: object) -> Literal["background", "bottom", "top", "overlay"]:
    if value in _VALID_LAYERS:
        return value  # type: ignore[return-value]
    raise ValueError(f"invalid layer {value!r}: expected one of {_VALID_LAYERS}")


def _validate_command(value: object) -> Literal["pause", "resume", "next", "prev", "stop"]:
    if value in _VALID_COMMANDS:
        return value  # type: ignore[return-value]
    raise ValueError(f"invalid command {value!r}: expected one of {_VALID_COMMANDS}")


class IStaticWallpaperBackend(ABC):
    @abstractmethod
    def set_image(self, path: str, monitor: str, fit_mode: FitMode) -> None:
        """Set a static image as wallpaper on the given monitor.

        Runtime: adapters MUST call `_validate_fit_mode(fit_mode)` (or `isinstance` check)
        because `Literal`/`FitMode` are static-only — plain `str` bypasses type-checking
        at runtime and would crash downstream (e.g., hyprpaper/s-www backend).
        """

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
        """Set a video as wallpaper on the given monitor.

        Runtime: adapters MUST validate `layer` via `_validate_layer(layer)`.
        """

    @abstractmethod
    def set_playlist(
        self,
        paths: list[str],
        monitor: str,
        mpv_options: str | None,
    ) -> None:
        """Set a video playlist as wallpaper."""

    @abstractmethod
    def control(
        self,
        monitor: str,
        command: Literal["pause", "resume", "next", "prev", "stop"],
    ) -> None:
        """Send a control command (pause, resume, next, etc.).

        Runtime: adapters MUST validate `command` via `_validate_command(command)`.
        """
