"""Shared stubs for the palette-producer port surface.

Test fakes only need to write the eight artifacts; derive now asks the active
adapter for its templates fingerprint and entry key (the producer seam). These
mixins keep every fake honest without re-implementing the formula.
"""

from __future__ import annotations

from pathlib import Path

from runtime.adapters.hashing import canonical_hash_dir, palette_entry_hash

_DEFAULT_TEMPLATE_SET_HASH = "c" * 64


class PaletteProducerStub:
    """Minimal ``IColorSchemeGenerator`` key surface for duck-typed fakes."""

    producer_name: str = "csg"
    templates_dir: Path | None = None

    def template_set_hash(self) -> str:
        templates_dir = getattr(self, "templates_dir", None)
        if templates_dir is not None:
            return canonical_hash_dir(templates_dir)
        return _DEFAULT_TEMPLATE_SET_HASH

    def expected_entry_hash(self, wallpaper_hash: str) -> str:
        return palette_entry_hash(wallpaper_hash, self.template_set_hash())
