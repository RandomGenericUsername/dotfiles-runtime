from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from runtime.domain.models import PaletteEntry


class IColorSchemeGenerator(ABC):
    #: Stable producer identity folded into cache keys ("csg" | "tpr").
    producer_name: str

    @property
    @abstractmethod
    def templates_dir(self) -> Path | None:
        """Resolved templates dir for this producer (``None`` when absent)."""

    @abstractmethod
    def template_set_hash(self) -> str:
        """Canonical hash of the producer's template set.

        Raises ``FileNotFoundError`` when the templates dir was not supplied,
        is missing or is empty (the adapter cannot produce a stable key).
        """

    @abstractmethod
    def expected_entry_hash(self, wallpaper_hash: str) -> str:
        """Entry hash this producer would use for ``wallpaper_hash``.

        ``derive`` and invalidation share this value; it folds the producer
        fingerprint (see ``adapters.hashing.palette_producer_seed``) so two
        producers can never collide on one cache entry.
        """

    @abstractmethod
    def generate(
        self,
        wallpaper_path: Path,
        output_dir: Path,
    ) -> PaletteEntry:
        """Generate palette from wallpaper file via the producer's templates.

        Writes color scheme artifacts (colors.yaml, colors.conf, colors.gtk.css,
        colors.adw.css, colors.sequences, colors.rasi, colors.kitty,
        colors.btop.theme) into output_dir via env override. Returns the
        PaletteEntry with artifact_hashes computed.
        """
