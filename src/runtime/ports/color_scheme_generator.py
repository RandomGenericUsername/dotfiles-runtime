from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from runtime.domain.models import PaletteEntry


class IColorSchemeGenerator(ABC):
    @abstractmethod
    def generate(
        self,
        wallpaper_path: Path,
        output_dir: Path,
    ) -> PaletteEntry:
        """Generate palette from wallpaper file via CSG templates.

        Writes color scheme artifacts (colors.yaml, colors.conf, colors.gtk.css,
        colors.adw.css, colors.sequences, colors.rasi)
        into output_dir via env override. Returns the PaletteEntry with
        artifact_hashes computed.
        """
