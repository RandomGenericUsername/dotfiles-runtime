from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.models import IconsEntry


class IIconRenderer(ABC):
    @abstractmethod
    def render(
        self,
        palette_hash: str,
        templates_dir: str,
        mappings_path: str,
        output_dir: str,
    ) -> IconsEntry:
        """Render icons from palette + icon templates/mappings.

        Writes SVG icons to output_dir.
        Returns the IconsEntry with artifact_hashes computed.
        """
