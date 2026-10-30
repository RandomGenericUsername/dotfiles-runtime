"""The versioned ``palette.v1`` role vocabulary (REQ-006).

Vocabulary ``v1`` is the 50 Material roles emitted by theme-producer's
engine plus the produced ``on_backdrop`` role:

- Source: theme-producer ``v0.5.0`` REQ-022, ``MaterialRole`` in
  ``src/theme_producer/domain/enums.py`` (50 engine-emitted roles).
- Additive role: theme-producer ``v0.5.0`` REQ-022 adds the produced
  ``on_backdrop`` ("ink over the wallpaper band") role to every named-token
  projection (``theme.json``, ``colors.yaml``, ``colors.gtk.css``,
  ``colors.adw.css``, ``colors.rasi``).
- Vendored probe fixture: ``tests/fixtures/palette-vocabulary-v1.json``.
  Drift between this constant and the fixture fails the test suite.

This module is PURE domain data (no I/O). Conformance validation lives in
:mod:`runtime.application.palette_conformance`; this module only declares
what "the vocabulary" means and the typed failure it produces.
"""

from __future__ import annotations

from typing import Final

__all__ = [
    "PALETTE_VOCABULARY_ROLES",
    "PALETTE_VOCABULARY_V1",
    "PALETTE_VOCABULARY_VERSION",
    "PaletteVocabularyError",
]

#: Version of the declared role vocabulary. A breaking role change (rename or
#: removal) requires a new version; additive roles keep the version and are
#: reported as extras by conformance inspection.
PALETTE_VOCABULARY_VERSION: Final[str] = "1"

#: The ordered ``palette.v1`` declaration: the 50 theme-producer Material
#: roles in ``MaterialRole`` declaration order, then ``on_backdrop``.
#:
#: Source: theme-producer ``v0.5.0`` REQ-022
#: (``src/theme_producer/domain/enums.py::MaterialRole`` + the produced
#: ``on_backdrop`` role).
PALETTE_VOCABULARY_V1: Final[tuple[str, ...]] = (
    "background",
    "error",
    "error_container",
    "inverse_on_surface",
    "inverse_primary",
    "inverse_surface",
    "on_background",
    "on_error",
    "on_error_container",
    "on_primary",
    "on_primary_container",
    "on_primary_fixed",
    "on_primary_fixed_variant",
    "on_secondary",
    "on_secondary_container",
    "on_secondary_fixed",
    "on_secondary_fixed_variant",
    "on_surface",
    "on_surface_variant",
    "on_tertiary",
    "on_tertiary_container",
    "on_tertiary_fixed",
    "on_tertiary_fixed_variant",
    "outline",
    "outline_variant",
    "primary",
    "primary_container",
    "primary_fixed",
    "primary_fixed_dim",
    "scrim",
    "secondary",
    "secondary_container",
    "secondary_fixed",
    "secondary_fixed_dim",
    "shadow",
    "source_color",
    "surface",
    "surface_bright",
    "surface_container",
    "surface_container_high",
    "surface_container_highest",
    "surface_container_low",
    "surface_container_lowest",
    "surface_dim",
    "surface_tint",
    "surface_variant",
    "tertiary",
    "tertiary_container",
    "tertiary_fixed",
    "tertiary_fixed_dim",
    "on_backdrop",
)

#: Set projection of the declaration, for membership checks.
PALETTE_VOCABULARY_ROLES: Final[frozenset[str]] = frozenset(PALETTE_VOCABULARY_V1)


class PaletteVocabularyError(RuntimeError):
    """A produced palette does not conform to the declared vocabulary.

    Typed, fail-closed violation: the caller must abort BEFORE the visible
    swap (BUG-001 ordering) — there is deliberately no warning-only path and
    no fallback vocabulary. ``missing_roles`` names removed/renamed roles;
    ``malformed_roles`` names roles present with a non-hex value.
    """

    def __init__(
        self,
        message: str,
        *,
        vocabulary_version: str = PALETTE_VOCABULARY_VERSION,
        missing_roles: tuple[str, ...] = (),
        malformed_roles: tuple[str, ...] = (),
        artifact_errors: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.vocabulary_version = vocabulary_version
        self.missing_roles = missing_roles
        self.malformed_roles = malformed_roles
        self.artifact_errors = artifact_errors
