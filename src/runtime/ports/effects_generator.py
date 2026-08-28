from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.models import EffectsEntry


class IEffectsGenerator(ABC):
    @abstractmethod
    def generate(
        self,
        wallpaper_hash: str,
        catalog_path: str,
        output_dir: str,
    ) -> EffectsEntry:
        """Generate effects from wallpaper hash + effects catalog.

        Writes effect artifacts to output_dir.
        Returns the EffectsEntry with artifact_hashes computed.
        """
