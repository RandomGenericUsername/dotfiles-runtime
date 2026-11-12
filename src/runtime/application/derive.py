"""Shared derivation plumbing for cache population use cases.

Implements AD-1 (hexagonal), AD-2 (input changes invalidate), AD-5
(state_root), AD-9 (staging-dir), AD-11 (read-only spine reads post-seed),
AD-14 (domain purity), AD-15 (cross-package boundary), AD-16 (hardlink).

Extracted from ``SeedCacheUseCase`` (Story 1.13, Task 2) so both the
seeder and ``ApplyWallpaperUseCase`` share the exact spine-discovery
helpers and the per-layer populate pattern (staging + drain + meta-in-
staging + load-on-race). Behavior contract locked by the seed test suite:

- Spine discovery resolves the install spine ONLY (R-3/AD-43). A repo checkout
  is consulted solely via the explicit `DOTFILES_DEV_INPUTS_ROOT` override.
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
- Migration (Story gt-2-1, AC 8): a palette entry is a valid cache hit
  ONLY if its ``meta.json`` ``artifact_hashes`` carries every name in
  ``PALETTE_ARTIFACT_NAMES`` AND every one of those artifact files exists.
  An incomplete entry (any pre-growth partial, including the 3-, 6- and
  7-artifact shapes) is evicted
  (``shutil.rmtree``, logged once) and regenerated through the normal
  staging path at the SAME ``<ph>`` — cache keys are unchanged, so
  eviction is what makes the write-once invariant yield exactly once,
  for pre-growth partial entries only. The guard lives in
  ``ensure_palette_entry_complete`` (this module) and is shared by
  ``DerivationPipeline.ensure_palette`` (seed/apply) and
  ``ReconcileDesktopStateUseCase._ensure_entries`` — ONE migration rule,
  three callers. ``inspect`` never calls it (read-only projection).

"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import cast

from runtime.adapters.cache import cache_entry_path, populate_via_staging
from runtime.adapters.hashing import (
    canonical_hash_dir,
    effects_entry_hash,
    hash_file,
    icons_entry_hash,
    skin_entry_hash,
)
from runtime.adapters.seeder import CacheSeeder
from runtime.application.palette_conformance import require_palette_conformance
from runtime.domain.models import (
    PALETTE_ARTIFACT_NAMES,
    EffectsEntry,
    IconsEntry,
    MissingDerivationInputError,
    PaletteEntry,
    SkinEntry,
    SkinUnavailableError,
)
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.icon_renderer import IIconRenderer
from runtime.ports.skin_expander import ISkinExpander

logger = logging.getLogger(__name__)

#: Explicit dev override (R-3, AD-43): when set, the runtime MAY resolve
#: derivation inputs from a checkout instead of the install spine. Since S1 the
#: only inputs with a checkout candidate are the two assets-fed ones, so the
#: value means "an assets checkout" (icons/ at its root). Unset in production —
#: resolution is spine-only and a missing input fails loud.
_DEV_INPUTS_ROOT_ENV = "DOTFILES_DEV_INPUTS_ROOT"

_KIND_DIR = "dir"
_KIND_FILE = "file"

#: Human labels + the spine path we name when an input is missing.
_INPUT_LABELS: dict[str, str] = {
    "weg_effects": "effects catalog",
    "icon_templates": "icon templates dir",
    "icon_mappings": "icon mappings",
    "skin_templates": "skin templates dir",
}

#: Keys already warned about a dev-checkout resolution (log once, not per call).
_WARNED: set[str] = set()


def _dev_inputs_root() -> Path | None:
    raw = os.environ.get(_DEV_INPUTS_ROOT_ENV)
    return Path(raw).expanduser().resolve() if raw else None


def _matches(path: Path, kind: str) -> bool:
    try:
        return path.is_dir() if kind == _KIND_DIR else path.is_file()
    except OSError:
        return False


def _input_candidates(
    install_spine: Path,
) -> dict[str, tuple[list[tuple[Path, str]], list[tuple[Path, str]]]]:
    """Spine candidates (absolute) and repo candidates (relative to the dev root)."""
    skin_templates_spine: list[tuple[Path, str]] = [
        (install_spine / "skin-templates", _KIND_DIR),
        (install_spine / "config" / "skin-templates", _KIND_DIR),
        (install_spine / "config" / "skins" / "templates", _KIND_DIR),
        (install_spine / "skins" / "templates", _KIND_DIR),
    ]
    # Pinned assets checkout: <install>/assets/<tag>/{skin-templates,skins/templates}.
    assets_root = install_spine / "assets"
    if assets_root.is_dir():
        try:
            for child in sorted(assets_root.iterdir()):
                if child.is_dir():
                    skin_templates_spine.append((child / "skin-templates", _KIND_DIR))
                    skin_templates_spine.append((child / "skins" / "templates", _KIND_DIR))
        except OSError:
            pass
    return {
        "weg_effects": (
            [(install_spine / "config" / "weg" / "effects.yaml", _KIND_FILE)],
            # No repo candidate: the in-tree weg package tree was retired in
            # cli-tools-repos S5 (weg is consumed from its pinned repo), so
            # there is no checkout to fall back to. The dev override still
            # covers the other three inputs; the catalog is spine-only.
            [],
        ),
        "icon_templates": (
            [
                (install_spine / "icon-templates", _KIND_DIR),
                (install_spine / "config" / "icon-templates-renderer" / "templates", _KIND_DIR),
            ],
            # S1: the corpus is the pinned `dotfiles-assets` repo, so the dev
            # root now means "an assets checkout" (icons/ at its root), not a
            # composition-repo tree. The spine stays the production source
            # (AD-13); this is opt-in via DOTFILES_DEV_INPUTS_ROOT and logged.
            [(Path("icons/templates"), _KIND_DIR)],
        ),
        "icon_mappings": (
            [
                (install_spine / "icon-mappings" / "icons.yaml", _KIND_FILE),
                (install_spine / "icon-mappings", _KIND_DIR),
                (install_spine / "config" / "icon-templates-renderer" / "icons.yaml", _KIND_FILE),
            ],
            [
                # S1: assets-checkout-relative — icons.yaml is the single group
                # map; defaults.yaml sits beside it as the vocabulary.
                (Path("icons/mappings/icons.yaml"), _KIND_FILE),
                (Path("icons/mappings"), _KIND_DIR),
            ],
        ),
        "skin_templates": (
            skin_templates_spine,
            [
                (Path("skin-templates"), _KIND_DIR),
                (Path("skins/templates"), _KIND_DIR),
            ],
        ),
    }


def resolve_input(key: str, install_spine: Path) -> tuple[Path | None, str]:
    """Resolve one derivation input; return ``(path, source)``.

    ``source`` is ``"spine"`` (production-correct), ``"repo"`` (dev override in
    effect), or ``"missing"``. Spine is always tried first; the repo is only
    consulted when ``DOTFILES_DEV_INPUTS_ROOT`` is set (and then logged).
    """
    spine_candidates, repo_candidates = _input_candidates(install_spine)[key]
    for path, kind in spine_candidates:
        if _matches(path, kind):
            return path, "spine"
    dev = _dev_inputs_root()
    if dev is not None:
        for rel, kind in repo_candidates:
            candidate = dev / rel
            if _matches(candidate, kind):
                if key not in _WARNED:
                    _WARNED.add(key)
                    logger.warning(
                        "derivation input %r resolved from dev checkout (%s=%s); "
                        "production reads the install spine only",
                        _INPUT_LABELS[key],
                        _DEV_INPUTS_ROOT_ENV,
                        dev,
                    )
                else:
                    logger.debug("derivation input %r from dev checkout: %s", key, candidate)
                return candidate, "repo"
    return None, "missing"


def input_provenance(install_spine: Path) -> dict[str, tuple[Path | None, str]]:
    """Per-input ``(path, source)`` for every derivation input — used by doctor."""
    return {key: resolve_input(key, install_spine) for key in _INPUT_LABELS}


def require_input(key: str, install_spine: Path) -> Path:
    path, _ = resolve_input(key, install_spine)
    if path is None:
        label = _INPUT_LABELS[key]
        spine = " | ".join(str(p) for p, _ in _input_candidates(install_spine)[key][0])
        raise MissingDerivationInputError(
            f"{label} not found in install spine ({spine}); "
            f"set {_DEV_INPUTS_ROOT_ENV} to a repo checkout for development"
        )
    return path


def find_effects_catalog(install_spine: Path) -> Path | None:
    """Resolve the WEG effects catalog (spine-only; dev override opt-in). None = absent."""
    path, _ = resolve_input("weg_effects", install_spine)
    return path


def find_icon_templates(install_spine: Path) -> Path | None:
    """Resolve the ITR icon-templates dir (spine-only; dev override opt-in). None = absent."""
    path, _ = resolve_input("icon_templates", install_spine)
    return path


def find_icon_mappings(install_spine: Path) -> Path | None:
    """Resolve the ITR icon mappings (spine-only; dev override opt-in). None = absent."""
    path, _ = resolve_input("icon_mappings", install_spine)
    return path


def find_skin_templates(install_spine: Path) -> Path | None:
    """Resolve the shared skin template set (spine-only; dev override opt-in)."""
    path, _ = resolve_input("skin_templates", install_spine)
    return path


def _skin_roots(install_spine: Path, dev: Path | None) -> list[Path]:
    """Ordered skin roots: spine, config copy, pinned assets checkouts, dev."""
    roots = [
        install_spine / "skins",
        install_spine / "config" / "skins",
    ]
    assets_root = install_spine / "assets"
    if assets_root.is_dir():
        try:
            for child in sorted(assets_root.iterdir()):
                if child.is_dir():
                    roots.append(child / "skins")
        except OSError:
            pass
    if dev is not None:
        roots.append(dev / "skins")
    return roots


def _manifest_paths(root: Path, name: str) -> list[Path]:
    return [
        # Canonical assets layout: <skins>/manifests/<name>.{toml,json}.
        root / "manifests" / f"{name}.toml",
        root / "manifests" / f"{name}.json",
        # Legacy/alternate in-spine layouts remain readable.
        root / f"{name}.toml",
        root / f"{name}.json",
        root / name / "manifest.toml",
        root / name / "manifest.json",
        root / name / "skin.toml",
        root / name / "skin.json",
    ]


def find_skin_manifest(name: str, install_spine: Path) -> tuple[Path | None, str]:
    """Resolve one skin manifest; return ``(path, source)`` (source: spine/repo/missing).

    ``source == "spine"`` covers the install spine, its ``config/skins`` copy
    and the pinned ``assets/<tag>/skins`` checkout (all read-only spine reads).
    ``source == "repo"`` is the explicit development override only.
    """
    if not name or "/" in name or "\\" in name or ".." in name or name.strip() != name:
        return None, "missing"
    dev = _dev_inputs_root()
    for root in _skin_roots(install_spine, dev):
        for candidate in _manifest_paths(root, name):
            if _matches(candidate, _KIND_FILE):
                source = "repo" if dev is not None and root == dev / "skins" else "spine"
                if source == "repo" and "skin_manifest" not in _WARNED:
                    _WARNED.add("skin_manifest")
                    logger.warning(
                        "skin manifest %r resolved from dev checkout (%s=%s); "
                        "production reads the install spine only",
                        name,
                        _DEV_INPUTS_ROOT_ENV,
                        dev,
                    )
                return candidate, source
    return None, "missing"


def _hash_path_input(path: Path, what: str) -> str:
    """Hash a derivation input path: canonical dir hash for dirs, file hash for files.

    Raises ``RuntimeError`` when the path is neither a dir nor a file.
    """
    if path.is_dir():
        return canonical_hash_dir(path)
    if path.is_file():
        return hash_file(path)
    raise RuntimeError(f"{what} path missing: {path}")


def ensure_palette_entry_complete(target: Path, seeder: CacheSeeder) -> bool:
    """Validate a palette cache hit's completeness; evict incomplete entries.

    The migration mechanism (Story gt-2-1, AC 8): a palette entry dir
    existing at ``cache/palettes/<ph>/`` is a valid cache hit ONLY if its
    co-located ``meta.json`` ``artifact_hashes`` carries ALL TEN names in
    ``PALETTE_ARTIFACT_NAMES`` and all ten artifact files exist (AD-3:
    meta.json is the completeness oracle — never trust dir existence
    alone).

    An incomplete entry (a pre-growth partial: the 3-, 6- or 7-artifact
    shape, or any future partial) is treated as a cache
    MISS: it is evicted (``shutil.rmtree``, ``ignore_errors=False`` — an
    OSError propagates to the caller's failure policy) so the SAME
    ``<ph>`` is regenerated through the normal staging path by the
    caller. Eviction is logged once (``logger.info``).

    Returns:
        True  — ``target`` does not exist (plain miss, nothing to evict)
                or is a complete, valid hit.
        False — ``target`` existed but was incomplete: it has been
                evicted and the caller must regenerate.

    Raises:
        OSError: if eviction fails (never leave a half-evicted dir).
    """
    if not target.exists():
        return False
    complete = True
    try:
        meta = seeder.read_entry_meta(target)
        artifact_hashes = meta["artifact_hashes"]
        for name in PALETTE_ARTIFACT_NAMES:
            if name not in artifact_hashes or not (target / name).is_file():
                complete = False
                break
    except OSError, ValueError, TypeError, KeyError:
        # Corrupt/absent meta.json or malformed artifact_hashes — an
        # entry whose completeness cannot be proven is not a hit.
        complete = False
    if complete:
        return True
    logger.info("palette entry incomplete (pre-growth artifact set); evicting: %s", target)
    shutil.rmtree(target, ignore_errors=False)
    return False


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
        palette_producer: IColorSchemeGenerator,
        weg: IEffectsGenerator,
        itr: IIconRenderer,
        install_spine: Path,
        skins: ISkinExpander | None = None,
    ) -> None:
        self._state_root = state_root
        self._seeder = seeder
        self._palette_producer = palette_producer
        self._weg = weg
        self._itr = itr
        self._install_spine = install_spine
        self._skins = skins

    def expected_palette_hash(self, wallpaper_hash: str) -> str:
        """The active producer's current palette key for ``wallpaper_hash``.

        Mode is part of the producer seed, so a mode flip changes this key even
        when the wallpaper and templates are unchanged.
        """
        return self._palette_producer.expected_entry_hash(wallpaper_hash)

    def expected_effects_hash(self, wallpaper_hash: str) -> str:
        """The effects entry key for ``wallpaper_hash`` from current spine inputs."""
        catalog_path = require_input("weg_effects", self._install_spine)
        return effects_entry_hash(wallpaper_hash, hash_file(catalog_path))

    def expected_icons_hash(self, palette_entry_hash: str) -> str:
        """The icons entry key for ``palette_entry_hash`` from current spine inputs.

        Internal invalidation helper: the same palette/templates/mappings
        inputs that :meth:`ensure_icons` hashes, without invoking the renderer.
        Reconcile uses it to distinguish a legitimate input-change rebuild
        (palette or icon templates/mappings moved) from a true cache miss that
        must reproduce the recorded key.
        """
        templates_dir = require_input("icon_templates", self._install_spine)
        mappings_path = require_input("icon_mappings", self._install_spine)
        return icons_entry_hash(
            palette_entry_hash,
            _hash_path_input(templates_dir, "icon templates"),
            _hash_path_input(mappings_path, "icon mappings"),
        )

    def ensure_palette(
        self, wallpaper_path: Path, wallpaper_hash: str
    ) -> tuple[PaletteEntry, bool]:
        """Ensure the palette cache entry.

        Cache hit requires completeness (migration, AC 8): dir existence
        alone is NOT enough — ``ensure_palette_entry_complete`` evicts a
        pre-growth partial entry so the same ``<ph>`` regenerates below.

        The active producer owns both the templates fingerprint and the entry
        key (:class:`runtime.ports.color_scheme_generator.IColorSchemeGenerator`).

        ``palette.v1`` (REQ-006): every produced entry is checked against the
        declared role vocabulary BEFORE it can become visible — both a fresh
        staged entry and an existing cache hit. A missing/renamed/malformed
        role raises the typed :class:`PaletteVocabularyError`, which the
        calling use case surfaces as a hard palette failure before the
        BUG-001 visible swap.
        """
        template_set_hash = self._palette_producer.template_set_hash()
        peh = self._palette_producer.expected_entry_hash(wallpaper_hash)
        target = cache_entry_path(self._state_root, "palettes", peh)
        if ensure_palette_entry_complete(target, self._seeder):
            require_palette_conformance(target)
            return self._seeder.load_palette_entry(target), True

        entry_holder: list[PaletteEntry] = []

        def _populate(staging: Path) -> None:
            work = staging / peh
            generated = self._palette_producer.generate(wallpaper_path, work)
            if generated.entry_hash != peh:
                raise RuntimeError(
                    f"adapter entry hash mismatch: adapter={generated.entry_hash} "
                    f"computed={peh} (templates dir divergence)"
                )
            # Fail closed BEFORE drain/staging rename: a nonconforming
            # entry never lands in the cache and never reaches the swap.
            require_palette_conformance(work)
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
                    "colors.adw.css": generated.artifact_hashes["colors_adw_css"],
                    "colors.sequences": generated.artifact_hashes["colors_sequences"],
                    "colors.rasi": generated.artifact_hashes["colors_rasi"],
                    "colors.kitty": generated.artifact_hashes["colors_kitty"],
                    "colors.btop.theme": generated.artifact_hashes["colors_btop"],
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
        catalog_path = require_input("weg_effects", self._install_spine)
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
        """Ensure the icons cache entry. Cache hit is entry-dir existence.

        Icons render exactly what the spine manifest resolves: the entry hash
        folds the palette, the icon templates and the mappings, and the
        renderer receives those same paths verbatim. There is no runtime
        retargeting overlay and no contrast policy.

        Raises:
            RuntimeError: on a missing spine input or an adapter entry-hash
                mismatch. The caller applies the failure policy (icons are
                graceful: the layer degrades to ``None``).
        """
        templates_dir = require_input("icon_templates", self._install_spine)
        mappings_path = require_input("icon_mappings", self._install_spine)

        templates_hash = _hash_path_input(templates_dir, "icon templates")
        mappings_hash_val = _hash_path_input(mappings_path, "icon mappings")

        ieh = icons_entry_hash(palette_entry_hash, templates_hash, mappings_hash_val)
        target = cache_entry_path(self._state_root, "icons", ieh)
        if target.exists():
            return self._seeder.load_icons_entry(target), True

        entry_holder: list[IconsEntry] = []

        def _populate(staging: Path) -> None:
            work = staging / ieh
            generated = self._itr.render(
                palette_entry_hash, templates_dir, mappings_path, work
            )
            if generated.entry_hash != ieh:
                raise RuntimeError(
                    f"adapter entry hash mismatch: adapter={generated.entry_hash} "
                    f"computed={ieh}"
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

    @property
    def has_skin_expander(self) -> bool:
        """True when a skin adapter is wired (legacy callers/tests may omit it)."""
        return self._skins is not None

    def ensure_skin(self, name: str) -> tuple[SkinEntry, bool]:
        """Ensure the selected skin cache entry (FROZEN SKIN SPEC v1).

        Identity is ``sha256("skin.v2\nrendererVersion=<n>\n" ‖ templates_hash
        ‖ manifest_hash)`` — palette- and wallpaper-independent by construction,
        so changing either never re-expands a skin. ``rendererVersion`` is the
        artifact-format version (``SKIN_RENDERER_VERSION``): bumping it
        invalidates old skin/appearance/generation entries after a renderer
        behavior change without changing rendered bytes for the same inputs.

        Raises:
            SkinUnavailableError: when the manifest/templates are missing,
                malformed or expansion fails. The caller applies the
                fail-closed policy (keep the previous skin; never touch CSS).
        """
        if self._skins is None:
            raise SkinUnavailableError(
                f"skin expander not configured for {name!r}",
                name=name,
                reason="no skin expander injected",
            )
        manifest_path, _source = find_skin_manifest(name, self._install_spine)
        if manifest_path is None:
            raise SkinUnavailableError(
                f"skin manifest not found for {name!r} in the install spine",
                name=name,
                reason="manifest missing",
                hint="install the pinned assets skin tree / select an existing skin",
            )
        templates_dir = find_skin_templates(self._install_spine)
        if templates_dir is None:
            raise SkinUnavailableError(
                f"skin templates not found for {name!r} in the install spine",
                name=name,
                reason="templates missing",
                manifest_path=str(manifest_path),
                hint="install the pinned assets skin template set",
            )
        try:
            templates_hash = _hash_path_input(templates_dir, "skin templates")
            manifest_hash = hash_file(manifest_path)
            seh = skin_entry_hash(templates_hash, manifest_hash)
            target = cache_entry_path(self._state_root, "skins", seh)
            if target.exists():
                return self._seeder.load_skin_entry(target), True

            entry_holder: list[SkinEntry] = []

            def _populate(staging: Path) -> None:
                work = staging / seh
                generated = self._skins.expand(name, manifest_path, templates_dir, work)
                if generated.entry_hash != seh:
                    raise RuntimeError(
                        f"adapter entry hash mismatch: adapter={generated.entry_hash} "
                        f"computed={seh} (skin manifest/templates divergence)"
                    )
                self._seeder.drain_work_dir(work, staging)
                self._seeder.write_skin_meta_in(
                    staging,
                    entry_hash=seh,
                    name=generated.name,
                    input_templates_hash=templates_hash,
                    input_manifest_hash=manifest_hash,
                    artifact_hashes=cast("dict[str, str]", generated.artifact_hashes),
                    generated_at=generated.generated_at,
                )
                entry_holder.append(generated)

            created = populate_via_staging(target, _populate)
            if created:
                return entry_holder[0], False
            return self._seeder.load_skin_entry(target), True
        except SkinUnavailableError:
            raise
        except Exception as exc:
            raise SkinUnavailableError(
                f"skin expansion failed for {name!r}: {exc}",
                name=name,
                reason=str(exc),
                manifest_path=str(manifest_path),
                templates_dir=str(templates_dir),
            ) from exc
