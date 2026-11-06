"""Skin expansion port (FROZEN SKIN SPEC v1).

A skin is a palette/ink mapping + paint + rhythm description expanded from a
shared template set and one chosen manifest. It carries no structure or
variant tokens. The canonical manifest is a ``schema_major``/``name`` header
plus the six ``shape``/``surface``/``ink``/``type``/``rhythm``/``layout``
groups (no ``[tokens]``/``[apps]``). Implementations parse/validate the
manifest, substitute every ``<configDir>.gtk.css.j2`` deterministically, and
write the per-config CSS plus the grouped ``skin.json`` token map.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from runtime.domain.models import SkinEntry


class ISkinExpander(ABC):
    """Expand one skin manifest + template set into a cache-entry work dir."""

    @abstractmethod
    def expand(
        self,
        name: str,
        manifest_path: Path,
        templates_dir: Path,
        output_dir: Path,
    ) -> SkinEntry:
        """Expand ``name`` into ``output_dir`` and return the entry.

        ``output_dir`` is the adapter work dir named after the expected entry
        hash (mirrors palette/WEG/ITR adapters). Raises ``ValueError``/
        ``SkinUnavailableError`` on a malformed/unsafe manifest or template;
        writes nothing outside ``output_dir``.
        """
        raise NotImplementedError
