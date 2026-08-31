from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from runtime.domain.models import IconsEntry


class IIconRenderer(ABC):
    @abstractmethod
    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> IconsEntry:
        """Render icons from palette + icon templates/mappings.

        Writes SVG icons to output_dir via env overrides.
        Returns the IconsEntry with artifact_hashes computed.
        """
