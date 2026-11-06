"""Port: compose one immutable appearance generation from cached inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from runtime.domain.models import AppearanceEntry, PaletteEntry, SkinEntry


class IAppearanceGenerator(Protocol):
    """Compose palette + skin + mode into one appearance cache entry."""

    def ensure(
        self,
        *,
        palette_entry: PaletteEntry,
        skin_entry: SkinEntry,
        install_spine: Path,
        manifest_path: Path | None = None,
        templates_dir: Path | None = None,
        keep: int = 5,
    ) -> tuple[AppearanceEntry, bool]:
        """Return ``(entry, cache_hit)`` for the composed generation.

        Raises on invalid inputs; the caller applies the fail-closed policy
        (keep the previous pointer and provider).
        """
        ...
