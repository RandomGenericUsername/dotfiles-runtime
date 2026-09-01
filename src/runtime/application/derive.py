"""Shared derivation plumbing for cache population use cases.

Implements AD-1 (hexagonal), AD-2 (input changes invalidate), AD-5
(state_root), AD-9 (staging-dir), AD-11 (read-only spine reads post-seed),
AD-14 (domain purity), AD-15 (cross-package boundary), AD-16 (hardlink).

Extracted from ``SeedCacheUseCase`` (Story 1.13, Task 2) so both the
seeder and ``ApplyWallpaperUseCase`` share the exact spine-discovery
helpers and the per-layer populate pattern (staging + drain + meta-in-
staging + load-on-race). Behavior contract locked by the seed test suite:

- Spine discovery checks the install spine first, then walks repo
  ancestors for dev checkouts (deferred-work rt-1-11 coupling).
- All spine reads are READ-ONLY (AD-11 post-seed rule, AD-15).
- Entry hash formulas per shared-data-contract Derivation-input hashing
  (adapters/hashing.py): palette = sha256(wh || template_set_hash),
  effects = sha256(wh || catalog_hash), icons = sha256(peh || t_h || m_h).
- Adapters validate ``output_dir.name == expected_entry_hash`` and reject
  ``.staging-*`` names, so populate callbacks generate into
  ``staging/<entry_hash>`` and artifacts are drained into the staging
  root where ``populate_via_staging`` expects them plus ``meta.json``.
- Failure policy is owned by the CALLING use case: the pipeline raises;
  the seeder wraps palette failures as ``palette seeding failed:`` and
  apply wraps as ``palette apply failed:`` (palette hard dependency);
  effects/icons degrade gracefully per use case.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

from runtime.adapters.cache import cache_entry_path, populate_via_staging
from runtime.adapters.hashing import (
    canonical_hash_dir,
    effects_entry_hash,
    hash_file,
    icons_entry_hash,
    palette_entry_hash,
)
from runtime.adapters.seeder import CacheSeeder
from runtime.domain.models import EffectsEntry, IconsEntry, PaletteEntry
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.icon_renderer import IIconRenderer

logger = logging.getLogger(__name__)


def find_templates_dir(install_spine: Path) -> Path | None:
    """Discover CSG templates directory (spine first, then repo ancestors)."""
    spine_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    try:
        if spine_templates.is_dir():
            return spine_templates
    except OSError:
        pass

    for parent in install_spine.parents:
        candidates = [
            parent
            / "src"
            / "cli-tools"
            / "color-scheme-generator"
            / "src"
            / "color_scheme_generator"
            / "defaults"
            / "templates",
            parent / "src" / "cli-tools" / "color-scheme-generator" / "defaults" / "templates",
        ]
        for candidate in candidates:
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
    return None


def find_effects_catalog(install_spine: Path) -> Path | None:
    """Discover WEG effects catalog (spine first, then repo ancestors)."""
    spine_catalog = install_spine / "config" / "weg" / "effects.yaml"
    try:
        if spine_catalog.is_file():
            return spine_catalog
    except OSError:
        pass

    for parent in install_spine.parents:
        candidates = [
            parent
            / "src"
            / "cli-tools"
            / "wallpaper-effects-generator"
            / "src"
            / "wallpaper_effects_generator"
            / "defaults"
            / "effects.yaml",
            parent
            / "src"
            / "cli-tools"
            / "wallpaper-effects-generator"
            / "defaults"
            / "effects.yaml",
        ]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def find_icon_templates(install_spine: Path) -> Path | None:
    """Discover ITR icon templates directory (spine first, then repo ancestors)."""
    spine_templates = install_spine / "config" / "icon-templates-renderer" / "templates"
    try:
        if spine_templates.is_dir():
            return spine_templates
    except OSError:
        pass

    for parent in install_spine.parents:
        candidates = [
            parent
            / "src"
            / "cli-tools"
            / "icon-templates-renderer"
            / "src"
            / "icon_templates_renderer"
            / "defaults"
            / "templates",
            parent / "src" / "cli-tools" / "icon-templates-renderer" / "defaults" / "templates",
        ]
        for candidate in candidates:
            try:
                if candidate.is_dir():
                    return candidate
            except OSError:
                continue
    return None


def find_icon_mappings(install_spine: Path) -> Path | None:
    """Discover ITR icon mappings (spine first, then repo ancestors)."""
    spine_mappings = install_spine / "config" / "icon-templates-renderer" / "icons.yaml"
    try:
        if spine_mappings.is_file():
            return spine_mappings
    except OSError:
        pass

    for parent in install_spine.parents:
        candidates = [
            parent
            / "src"
            / "cli-tools"
            / "icon-templates-renderer"
            / "src"
            / "icon_templates_renderer"
            / "defaults"
            / "icons.yaml",
            parent / "src" / "cli-tools" / "icon-templates-renderer" / "defaults" / "icons.yaml",
        ]
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def _hash_path_input(path: Path, what: str) -> str:
    """Hash a derivation input path: canonical dir hash for dirs, file hash for files.

    Raises ``RuntimeError`` when the path is neither a dir nor a file.
    """
    if path.is_dir():
        return canonical_hash_dir(path)
    if path.is_file():
        return hash_file(path)
    raise RuntimeError(f"{what} path missing: {path}")


class DerivationPipeline:
    """Per-layer ensure-entry orchestrator shared by seed and apply.

    Constructor receives ports and the ``CacheSeeder`` adapter (dependency
    inversion): the pipeline wires no concrete adapters itself.

    Each ``ensure_*`` method:
    1. Computes the entry hash from the canonical input set (re-read on
       every derivation — AD-11 read-only; AD-2: input changes invalidate).
    2. Returns ``(entry, cache_hit=True)`` immediately if the cache entry
       dir exists — the entry is rebuilt from its co-located ``meta.json``
       so callers never persist sentinel hashes.
    3. Otherwise populates via the staging-dir pattern (tool invoked once)
       and returns ``(entry, cache_hit=False)``. On a rename race the
       staging is discarded and the winner's entry is loaded from
       meta.json (reported as a cache hit — the entry exists).

    Raises:
        RuntimeError: on missing spine inputs, adapter entry-hash
            mismatch (templates divergence), or populate failure. The
            CALLER applies the failure policy (palette hard, effects/
            icons graceful).
    """

    def __init__(
        self,
        state_root: Path,
        seeder: CacheSeeder,
        csg: IColorSchemeGenerator,
        weg: IEffectsGenerator,
        itr: IIconRenderer,
        install_spine: Path,
    ) -> None:
        self._state_root = state_root
        self._seeder = seeder
        self._csg = csg
        self._weg = weg
        self._itr = itr
        self._install_spine = install_spine

    def ensure_palette(
        self, wallpaper_path: Path, wallpaper_hash: str
    ) -> tuple[PaletteEntry, bool]:
        """Ensure the palette cache entry. Cache hit is entry-dir existence."""
        templates_dir = find_templates_dir(self._install_spine)
        if templates_dir is None:
            raise RuntimeError(f"CSG templates dir not found (install_spine={self._install_spine})")

        template_set_hash = canonical_hash_dir(templates_dir)
        peh = palette_entry_hash(wallpaper_hash, template_set_hash)
        target = cache_entry_path(self._state_root, "palettes", peh)
        if target.exists():
            return self._seeder.load_palette_entry(target), True

        entry_holder: list[PaletteEntry] = []

        def _populate(staging: Path) -> None:
            work = staging / peh
            generated = self._csg.generate(wallpaper_path, work)
            if generated.entry_hash != peh:
                raise RuntimeError(
                    f"adapter entry hash mismatch: adapter={generated.entry_hash} "
                    f"computed={peh} (templates dir divergence)"
                )
            self._seeder.drain_work_dir(work, staging)
            self._seeder.write_palette_meta_in(
                staging,
                entry_hash=peh,
                source_wallpaper_hash=wallpaper_hash,
                input_template_hash=template_set_hash,
                artifact_hashes={
                    "colors.yaml": generated.artifact_hashes["colors_yaml"],
                    "colors.conf": generated.artifact_hashes["colors_conf"],
                    "colors.gtk.css": generated.artifact_hashes["colors_gtk_css"],
                },
                generated_at=generated.generated_at,
            )
            entry_holder.append(
                PaletteEntry(
                    hash_algorithm="sha256",
                    kind="palette",
                    entry_hash=peh,
                    source_wallpaper_hash=wallpaper_hash,
                    input_template_hash=template_set_hash,
                    artifact_hashes=generated.artifact_hashes,
                    generated_at=generated.generated_at,
                )
            )

        created = populate_via_staging(target, _populate)
        if created:
            return entry_holder[0], False
        # Entry already existed (lost race): rebuild from its meta.json so
        # the state still carries real hashes.
        return self._seeder.load_palette_entry(target), True

    def ensure_effects(
        self, wallpaper_path: Path, wallpaper_hash: str
    ) -> tuple[EffectsEntry, bool]:
        """Ensure the effects cache entry. Cache hit is entry-dir existence."""
        catalog_path = find_effects_catalog(self._install_spine)
        if catalog_path is None:
            raise RuntimeError(f"effects catalog not found (install_spine={self._install_spine})")

        catalog_hash = hash_file(catalog_path)
        eeh = effects_entry_hash(wallpaper_hash, catalog_hash)
        target = cache_entry_path(self._state_root, "effects", eeh)
        if target.exists():
            return self._seeder.load_effects_entry(target), True

        entry_holder: list[EffectsEntry] = []

        def _populate(staging: Path) -> None:
            work = staging / eeh
            generated = self._weg.generate(wallpaper_path, work)
            if generated.entry_hash != eeh:
                raise RuntimeError(
                    f"adapter entry hash mismatch: adapter={generated.entry_hash} computed={eeh}"
                )
            self._seeder.drain_work_dir(work, staging)
            self._seeder.write_effects_meta_in(
                staging,
                entry_hash=eeh,
                source_wallpaper_hash=wallpaper_hash,
                input_catalog_hash=catalog_hash,
                artifact_hashes=cast("dict[str, str]", generated.artifact_hashes),
                generated_at=generated.generated_at,
            )
            entry_holder.append(
                EffectsEntry(
                    hash_algorithm="sha256",
                    kind="effects",
                    entry_hash=eeh,
                    source_wallpaper_hash=wallpaper_hash,
                    input_catalog_hash=catalog_hash,
                    artifact_hashes=generated.artifact_hashes,
                    generated_at=generated.generated_at,
                )
            )

        created = populate_via_staging(target, _populate)
        if created:
            return entry_holder[0], False
        return self._seeder.load_effects_entry(target), True

    def ensure_icons(self, palette_entry_hash: str) -> tuple[IconsEntry, bool]:
        """Ensure the icons cache entry. Cache hit is entry-dir existence."""
        templates_dir = find_icon_templates(self._install_spine)
        mappings_path = find_icon_mappings(self._install_spine)
        if templates_dir is None or mappings_path is None:
            raise RuntimeError(
                f"icon templates/mappings not found (install_spine={self._install_spine})"
            )

        templates_hash = _hash_path_input(templates_dir, "icon templates")
        mappings_hash_val = _hash_path_input(mappings_path, "icon mappings")

        ieh = icons_entry_hash(palette_entry_hash, templates_hash, mappings_hash_val)
        target = cache_entry_path(self._state_root, "icons", ieh)
        if target.exists():
            return self._seeder.load_icons_entry(target), True

        entry_holder: list[IconsEntry] = []

        def _populate(staging: Path) -> None:
            work = staging / ieh
            generated = self._itr.render(palette_entry_hash, templates_dir, mappings_path, work)
            if generated.entry_hash != ieh:
                raise RuntimeError(
                    f"adapter entry hash mismatch: adapter={generated.entry_hash} computed={ieh}"
                )
            self._seeder.drain_work_dir(work, staging)
            self._seeder.write_icons_meta_in(
                staging,
                entry_hash=ieh,
                source_palette_hash=palette_entry_hash,
                input_templates_hash=templates_hash,
                input_mappings_hash=mappings_hash_val,
                artifact_hashes=cast("dict[str, str]", generated.artifact_hashes),
                generated_at=generated.generated_at,
            )
            entry_holder.append(
                IconsEntry(
                    hash_algorithm="sha256",
                    kind="icons",
                    entry_hash=ieh,
                    source_palette_hash=palette_entry_hash,
                    input_templates_hash=templates_hash,
                    input_mappings_hash=mappings_hash_val,
                    artifact_hashes=generated.artifact_hashes,
                    generated_at=generated.generated_at,
                )
            )

        created = populate_via_staging(target, _populate)
        if created:
            return entry_holder[0], False
        return self._seeder.load_icons_entry(target), True
