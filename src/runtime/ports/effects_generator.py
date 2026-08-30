from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from runtime.domain.models import EffectsEntry


class IEffectsGenerator(ABC):
    @abstractmethod
    def generate(
        self,
        wallpaper_path: Path,
        output_dir: Path,
    ) -> EffectsEntry:
        """Generate effects from wallpaper file via WEG catalog.

        Writes effect artifacts (PNGs) into output_dir via env override.
        Returns the EffectsEntry with artifact_hashes computed.
        """
