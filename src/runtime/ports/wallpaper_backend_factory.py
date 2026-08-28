from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from runtime.domain.models import BackendType
from runtime.ports.wallpaper_backend import IStaticWallpaperBackend, IVideoWallpaperBackend

# ── runtime guards for factory (Literal is static-only) ──
_VALID_STATIC_BACKENDS = frozenset(
    {BackendType.hyprpaper, BackendType.swaybg, BackendType.swww}
)
_VALID_VIDEO_BACKENDS = frozenset({BackendType.mpvpaper})


def _validate_static_backend(
    value: object,
) -> Literal[BackendType.hyprpaper, BackendType.swaybg, BackendType.swww]:
    if value in _VALID_STATIC_BACKENDS:
        return value  # type: ignore[return-value]
    raise ValueError(f"invalid static backend {value!r}: expected one of {_VALID_STATIC_BACKENDS}")


def _validate_video_backend(value: object) -> Literal[BackendType.mpvpaper]:
    if value in _VALID_VIDEO_BACKENDS:
        return value  # type: ignore[return-value]
    raise ValueError(f"invalid video backend {value!r}: expected mpvpaper")


class IWallpaperBackendFactory(ABC):
    @abstractmethod
    def create_static(
        self,
        backend_type: Literal[BackendType.hyprpaper, BackendType.swaybg, BackendType.swww],
    ) -> IStaticWallpaperBackend:
        """Create a static wallpaper backend by BackendType enum.

        Runtime: implementations MUST call `_validate_static_backend(backend_type)`
        because `Literal` is not enforced at runtime.
        """

    @abstractmethod
    def create_video(
        self,
        backend_type: Literal[BackendType.mpvpaper],
    ) -> IVideoWallpaperBackend:
        """Create a video wallpaper backend by BackendType enum.

        Runtime: implementations MUST call `_validate_video_backend(backend_type)`.
        """

    @abstractmethod
    def auto_detect(self, source_path: str) -> BackendType | None:
        """Detect backend type from file extension.

        Returns: BackendType enum (e.g., BackendType.MPVAPER, BackendType.SWWW),
        or None if the extension is unrecognized.

        Callers MUST handle None explicitly::

            backend = factory.auto_detect(path)
            if backend is None:
                raise ValueError(f"unrecognized extension for {path!r}")
        """
