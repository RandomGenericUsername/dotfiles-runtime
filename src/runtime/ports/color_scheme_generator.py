from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.models import PaletteEntry


class IColorSchemeGenerator(ABC):
    @abstractmethod
    def generate(
        self,
        wallpaper_hash: str,
        template_dir: str,
        output_dir: str,
    ) -> PaletteEntry:
        """Generate palette from wallpaper hash + CSG templates.

        Writes color scheme artifacts to output_dir.
        Returns the PaletteEntry with artifact_hashes computed.
        """
