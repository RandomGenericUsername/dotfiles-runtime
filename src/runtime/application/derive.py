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
  ONLY if its ``meta.json`` ``artifact_hashes`` carries all six artifact
  names AND the six artifact files exist. An incomplete entry
  (pre-growth partial: 3 legacy artifacts + old meta) is evicted
  (``shutil.rmtree``, logged once) and regenerated through the normal
  staging path at the SAME ``<ph>`` — cache keys are unchanged, so
  eviction is what makes the write-once invariant yield exactly once,
  for pre-growth partial entries only. The guard lives in
  ``ensure_palette_entry_complete`` (this module) and is shared by
  ``DerivationPipeline.ensure_palette`` (seed/apply) and
  ``ReconcileDesktopStateUseCase._ensure_entries`` — ONE migration rule,
  three callers. ``inspect`` never calls it (read-only projection).

Icon-contrast guard (openspec ``add-icon-contrast-guard``, workstream §3):
``DerivationPipeline.ensure_icons`` evaluates the WCAG 2.1 contrast of
allowlisted bar groups against the bar backdrop and, when below
threshold, renders from a staging-only overlay ``icons.yaml`` (spine
read-only, AD-11) whose group-level ``COLOR_FOREGROUND``/``COLOR_JOIN``
placeholders are retargeted to the highest-contrast palette-resident
token. The icons cache key hashes the EFFECTIVE (overlay) mappings, so
``ih = sha256(peh || t_h || m_h)`` stays content-addressed. A no-retarget
evaluation passes the spine path through untouched (pre-guard key
stable); ANY guard failure degrades to the spine mappings with a warning
(icons stay graceful).

Config (env only — no new settings plumbing; ``RUNTIME__``
double-underscore convention):

- ``RUNTIME__ICON_CONTRAST__THRESHOLD`` — minimum ratio, default ``4.5``.
- ``RUNTIME__ICON_CONTRAST__GROUPS`` — comma-separated group allowlist,
  default ``BAR_GROUPS`` (intersected with the domain allowlist).
- ``RUNTIME__ICON_CONTRAST__BAND_PX`` — top-strip sample height, default
  ``48``.

Backdrop choice: the sampled top-strip luminance picks the nearest of
the palette ``background``/``foreground`` hexes as the WCAG backdrop
(``backdrop_source="sampled"``); when sampling is unavailable the palette
``background`` is used directly (``backdrop_source="palette"``). The
backdrop is therefore always a real palette hex — never a synthetic gray.

No-YAML-subset note: the runtime intentionally does NOT depend on a YAML
library (``pyproject.toml`` carries no ``pyyaml``; cross-package ITR
imports would violate AD-15), so palette/mappings reads use a documented
line-subset parser (top-level scalars, ``special:``/``colors:`` blocks,
group-level ``color_mappings:``) and the overlay is a comment-preserving
text patch. Anything outside the subset degrades to the spine mappings
with a warning — never a hard failure.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Final, cast

from runtime.adapters.cache import cache_entry_path, populate_via_staging
from runtime.adapters.hashing import (
    canonical_hash_dir,
    effects_entry_hash,
    hash_file,
    icons_entry_hash,
    palette_entry_hash,
)
from runtime.adapters.icon_contrast_sampler import sample_top_strip_luminance
from runtime.adapters.seeder import CacheSeeder
from runtime.domain.icon_contrast import (
    BAR_GROUPS,
    DEFAULT_THRESHOLD,
    PLACEHOLDERS,
    decide_overrides,
    hex_to_rgb,
    pick_best_token,
    relative_luminance,
)
from runtime.domain.icon_contrast_policy import POLICY_SOURCES
from runtime.domain.models import (
    EffectsEntry,
    IconsEntry,
    MissingDerivationInputError,
    PaletteEntry,
)
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.icon_renderer import IIconRenderer

logger = logging.getLogger(__name__)

#: Explicit dev override (R-3, AD-43): when set, the runtime MAY resolve
#: derivation inputs from a repo checkout instead of the install spine. Unset
#: in production — resolution is spine-only and a missing input fails loud.
_DEV_INPUTS_ROOT_ENV = "DOTFILES_DEV_INPUTS_ROOT"

_KIND_DIR = "dir"
_KIND_FILE = "file"

#: Human labels + the spine path we name when an input is missing.
_INPUT_LABELS: dict[str, str] = {
    "csg_templates": "CSG templates dir",
    "weg_effects": "effects catalog",
    "icon_templates": "icon templates dir",
    "icon_mappings": "icon mappings",
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
    return {
        "csg_templates": (
            [(install_spine / "config" / "color-scheme-generator" / "templates", _KIND_DIR)],
            [
                (
                    Path(
                        "src/cli-tools/color-scheme-generator/src/color_scheme_generator/defaults/templates"
                    ),
                    _KIND_DIR,
                ),
                (Path("src/cli-tools/color-scheme-generator/defaults/templates"), _KIND_DIR),
            ],
        ),
        "weg_effects": (
            [(install_spine / "config" / "weg" / "effects.yaml", _KIND_FILE)],
            [
                (
                    Path(
                        "src/cli-tools/wallpaper-effects-generator/src/wallpaper_effects_generator/defaults/effects.yaml"
                    ),
                    _KIND_FILE,
                ),
                (
                    Path("src/cli-tools/wallpaper-effects-generator/defaults/effects.yaml"),
                    _KIND_FILE,
                ),
            ],
        ),
        "icon_templates": (
            [
                (install_spine / "icon-templates", _KIND_DIR),
                (install_spine / "config" / "icon-templates-renderer" / "templates", _KIND_DIR),
            ],
            [(Path("dotfiles/assets/icon-templates"), _KIND_DIR)],
        ),
        "icon_mappings": (
            [
                (install_spine / "icon-mappings" / "icons.yaml", _KIND_FILE),
                (install_spine / "icon-mappings", _KIND_DIR),
                (install_spine / "config" / "icon-templates-renderer" / "icons.yaml", _KIND_FILE),
            ],
            [
                (
                    Path("dotfiles/config/icon-template-color-scheme-mappings/icons.yaml"),
                    _KIND_FILE,
                ),
                (Path("dotfiles/config/icon-template-color-scheme-mappings"), _KIND_DIR),
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


def find_templates_dir(install_spine: Path) -> Path | None:
    """Resolve the CSG templates dir (spine-only; dev override opt-in). None = absent."""
    path, _ = resolve_input("csg_templates", install_spine)
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


def _hash_path_input(path: Path, what: str) -> str:
    """Hash a derivation input path: canonical dir hash for dirs, file hash for files.

    Raises ``RuntimeError`` when the path is neither a dir nor a file.
    """
    if path.is_dir():
        return canonical_hash_dir(path)
    if path.is_file():
        return hash_file(path)
    raise RuntimeError(f"{what} path missing: {path}")


PALETTE_ARTIFACT_NAMES: Final[tuple[str, ...]] = (
    "colors.yaml",
    "colors.conf",
    "colors.gtk.css",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
    "colors.kitty",
)


def ensure_palette_entry_complete(target: Path, seeder: CacheSeeder) -> bool:
    """Validate a palette cache hit's completeness; evict incomplete entries.

    The migration mechanism (Story gt-2-1, AC 8): a palette entry dir
    existing at ``cache/palettes/<ph>/`` is a valid cache hit ONLY if its
    co-located ``meta.json`` ``artifact_hashes`` carries ALL SIX artifact
    names and the six artifact files exist (AD-3: meta.json is the
    completeness oracle — never trust dir existence alone).

    An incomplete entry (a pre-growth partial: 3 legacy artifacts +
    old-shape meta.json, or any future partial) is treated as a cache
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


#: Env overrides for the icon-contrast guard (D5). Read directly with
#: ``os.environ`` (the ``RUNTIME__`` double-underscore convention); no new
#: settings plumbing.
_ICON_CONTRAST_THRESHOLD_ENV = "RUNTIME__ICON_CONTRAST__THRESHOLD"
_ICON_CONTRAST_GROUPS_ENV = "RUNTIME__ICON_CONTRAST__GROUPS"
_ICON_CONTRAST_BAND_PX_ENV = "RUNTIME__ICON_CONTRAST__BAND_PX"
_ICON_CONTRAST_BAND_PX_DEFAULT = 48


def icon_contrast_threshold() -> float:
    """Minimum WCAG ratio for bar icons (default ``DEFAULT_THRESHOLD``)."""
    raw = os.environ.get(_ICON_CONTRAST_THRESHOLD_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_THRESHOLD
    try:
        value = float(raw.strip())
    except ValueError:
        logger.warning(
            "icons: invalid %s=%r; using default %.1f",
            _ICON_CONTRAST_THRESHOLD_ENV,
            raw,
            DEFAULT_THRESHOLD,
        )
        return DEFAULT_THRESHOLD
    if not 1.0 <= value <= 21.0:
        logger.warning(
            "icons: %s=%r outside WCAG range [1.0, 21.0]; using default %.1f",
            _ICON_CONTRAST_THRESHOLD_ENV,
            raw,
            DEFAULT_THRESHOLD,
        )
        return DEFAULT_THRESHOLD
    return value


def icon_contrast_groups() -> tuple[str, ...]:
    """Bar-group allowlist override (default ``BAR_GROUPS``).

    Intersected with the domain ``BAR_GROUPS`` at evaluation time, so an
    env entry outside the domain allowlist can never widen the scope.
    """
    raw = os.environ.get(_ICON_CONTRAST_GROUPS_ENV)
    if raw is None or not raw.strip():
        return BAR_GROUPS
    groups = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not groups:
        return BAR_GROUPS
    return groups


def icon_contrast_band_px() -> int:
    """Top-strip sample height in px (default 48, the bar height)."""
    raw = os.environ.get(_ICON_CONTRAST_BAND_PX_ENV)
    if raw is None or not raw.strip():
        return _ICON_CONTRAST_BAND_PX_DEFAULT
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning(
            "icons: invalid %s=%r; using default %d",
            _ICON_CONTRAST_BAND_PX_ENV,
            raw,
            _ICON_CONTRAST_BAND_PX_DEFAULT,
        )
        return _ICON_CONTRAST_BAND_PX_DEFAULT
    if value < 1:
        logger.warning(
            "icons: %s=%r must be >= 1; using default %d",
            _ICON_CONTRAST_BAND_PX_ENV,
            raw,
            _ICON_CONTRAST_BAND_PX_DEFAULT,
        )
        return _ICON_CONTRAST_BAND_PX_DEFAULT
    return value


_TOP_KEY_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*):(?:[ \t]+(.*?))?[ \t]*$")
_LIST_ITEM_RE = re.compile(r"^[ \t]*-[ \t]+(.*?)[ \t]*$")


def _clean_scalar(raw: str) -> str | None:
    """Extract a plain scalar value; ``None`` for comments/blocks/collections."""
    text = raw.strip()
    if not text or text.startswith("#"):
        return None
    if text[0] in ('"', "'"):
        end = text.find(text[0], 1)
        if end == -1:
            return None
        return text[1:end]
    if text[0] in ("|", ">", "[", "{", "&", "*", "!", "@", "`"):
        return None
    token = text.split(None, 1)[0]
    if token.startswith("#"):
        return None
    return token


def _parse_palette_mapping(text: str) -> dict[str, str]:
    """Parse a CSG ``colors.yaml`` without a YAML library (subset only).

    Mirrors ``FileColorSchemeLoader._extract_values`` for the shapes the
    runtime must read: top-level ``background``/``foreground``/``cursor``
    scalars, a ``colors:`` block list (``- <hex>`` → ``color0..N`` in
    order) or mapping block (``colorN: <hex>``), the legacy ``special:``
    block (only its ``background``/``foreground``/``cursor`` keys), and
    any other top-level plain scalar as passthrough. Deeper nesting, flow
    collections, and multi-line blocks are ignored (tolerance — candidates
    are intersected with what parsed, never synthesized).
    """
    values: dict[str, str] = {}
    block: str | None = None
    color_index = 0
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        leading = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        if "\t" in leading:
            continue
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 0:
            match = _TOP_KEY_RE.match(stripped)
            if match is None:
                block = None
                continue
            key, rest = match.group(1), (match.group(2) or "").strip()
            if rest == "":
                block = key if key in ("special", "colors") else None
                continue
            cleaned = _clean_scalar(rest)
            if cleaned is not None:
                values[key] = cleaned
            block = None
        elif indent == 2 and block in ("special", "colors"):
            if block == "colors":
                item = _LIST_ITEM_RE.match(stripped)
                if item is not None:
                    cleaned = _clean_scalar(item.group(1))
                    if cleaned is not None:
                        values[f"color{color_index}"] = cleaned
                        color_index += 1
                    continue
            match = _TOP_KEY_RE.match(stripped)
            if match is None:
                continue
            key, rest = match.group(1), (match.group(2) or "").strip()
            if rest == "":
                continue
            if block == "special" and key not in ("background", "foreground", "cursor"):
                continue
            cleaned = _clean_scalar(rest)
            if cleaned is not None:
                values[key] = cleaned
        # Deeper lines are nested structures (variant/alias blocks) — ignored.
    return values


_GROUP_RE = re.compile(r"^([^\s#:][^:]*):[ \t]*(?:#.*)?$")
_SECTION_RE = re.compile(r"^  ([A-Za-z0-9_]+):[ \t]*(?:#.*)?$")
_ENTRY_RE = re.compile(rf"^    ({'|'.join(PLACEHOLDERS)}):(?P<gap>[ \t]+)(?P<rest>.*)$")
_LINE_VALUE_RE = _ENTRY_RE


def _split_mapping_value(rest: str) -> tuple[str, str, str] | None:
    """Split a mappings value into ``(token, quote, tail)``.

    ``quote`` is the surrounding quote char (or ``""`` when bare) and
    ``tail`` is everything after the token (gap + trailing comment),
    preserved verbatim so the overlay patch is byte-stable elsewhere.
    """
    text = rest.strip()
    if not text or text.startswith("#"):
        return None
    if text[0] in ('"', "'"):
        quote = text[0]
        end = text.find(quote, 1)
        if end == -1:
            return None
        return (text[1:end], quote, text[end + 1 :])
    token = text.split(None, 1)[0]
    return (token, "", text[len(token) :])


def _scan_group_color_mappings(text: str) -> dict[str, dict[str, str]]:
    """Extract group-level ``color_mappings`` from an ``icons.yaml`` (subset).

    Only ``<group>:`` → ``  color_mappings:`` → ``    PLACEHOLDER: token``
    lines are collected. Variant-level ``color_mappings`` (indent 6),
    ``bar_mappings:``, and every other section pass through untouched —
    the scope guard (D5) is structural, not name-based.
    """
    groups: dict[str, dict[str, str]] = {}
    group: str | None = None
    section: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        if raw_line[0] not in (" ", "\t"):
            match = _GROUP_RE.match(raw_line.strip())
            group = match.group(1).strip() if match else None
            section = None
            if group is not None:
                groups.setdefault(group, {})
            continue
        if group is None:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if indent == 2:
            section_match = _SECTION_RE.match(raw_line)
            section = section_match.group(1) if section_match else None
            continue
        if indent == 4 and section == "color_mappings":
            entry = _ENTRY_RE.match(raw_line)
            if entry is None:
                continue
            split = _split_mapping_value(entry.group("rest"))
            if split is None:
                continue
            token, _, _ = split
            if token:
                groups[group][entry.group(1)] = token
    return groups


def _patch_overlay_text(text: str, overrides: Mapping[tuple[str, str], str]) -> str:
    """Retarget group-level placeholders via a comment-preserving text patch.

    Only lines the scanner attributes to group-level ``color_mappings``
    are rewritten (same state machine as :func:`_scan_group_color_mappings`
    — variant entries and ``bar_mappings`` are structurally unreachable);
    quoting style, trailing comments, key order, and the final newline are
    preserved, so the overlay is deterministic per input bytes.
    """
    by_group: dict[str, dict[str, str]] = {}
    for (override_group, placeholder), new_token in overrides.items():
        by_group.setdefault(override_group, {})[placeholder] = new_token
    out: list[str] = []
    group: str | None = None
    section: str | None = None
    for raw_line in text.splitlines():
        patched = raw_line
        if raw_line.strip() and not raw_line.lstrip().startswith("#"):
            if raw_line[0] not in (" ", "\t"):
                match = _GROUP_RE.match(raw_line.strip())
                group = match.group(1).strip() if match else None
                section = None
            elif group is not None:
                indent = len(raw_line) - len(raw_line.lstrip(" "))
                if indent == 2:
                    section_match = _SECTION_RE.match(raw_line)
                    section = section_match.group(1) if section_match else None
                elif indent == 4 and section == "color_mappings":
                    entry = _LINE_VALUE_RE.match(raw_line)
                    if entry is not None:
                        placeholder = entry.group(1)
                        new_token = by_group.get(group, {}).get(placeholder)
                        if new_token is not None:
                            split = _split_mapping_value(entry.group("rest"))
                            if split is not None:
                                current, quote, tail = split
                                if current and not current.startswith("#"):
                                    patched = (
                                        f"    {placeholder}:{entry.group('gap')}"
                                        f"{quote}{new_token}{quote}{tail}"
                                    )
        out.append(patched)
    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result


def _choose_backdrop_hex(
    sampled: float | None, palette: Mapping[str, str]
) -> tuple[str, str] | None:
    """Pick the WCAG backdrop hex + source label.

    With a sampled top-strip luminance, the nearest of the palette
    ``background``/``foreground`` hexes wins (luminance distance), so the
    backdrop is always palette-resident; without a sample the palette
    ``background`` is used directly. ``None`` when no usable hex exists —
    the caller then passes the spine mappings through untouched.
    """
    background = palette.get("background")
    foreground = palette.get("foreground")
    if sampled is not None:
        scored: list[tuple[float, str]] = []
        for candidate in (background, foreground):
            if not candidate:
                continue
            try:
                lum = relative_luminance(hex_to_rgb(candidate))
            except ValueError:
                continue
            scored.append((abs(lum - sampled), candidate))
        if scored:
            scored.sort(key=lambda item: item[0])
            return scored[0][1], "sampled"
    if background:
        try:
            hex_to_rgb(background)
        except ValueError:
            return None
        return background, "palette"
    return None


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
        """Ensure the palette cache entry.

        Cache hit requires completeness (migration, AC 8): dir existence
        alone is NOT enough — ``ensure_palette_entry_complete`` evicts a
        pre-growth partial entry so the same ``<ph>`` regenerates below.
        """
        templates_dir = require_input("csg_templates", self._install_spine)
        template_set_hash = canonical_hash_dir(templates_dir)
        peh = palette_entry_hash(wallpaper_hash, template_set_hash)
        target = cache_entry_path(self._state_root, "palettes", peh)
        if ensure_palette_entry_complete(target, self._seeder):
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
                    "colors.adw.css": generated.artifact_hashes["colors_adw_css"],
                    "colors.sequences": generated.artifact_hashes["colors_sequences"],
                    "colors.rasi": generated.artifact_hashes["colors_rasi"],
                    "colors.kitty": generated.artifact_hashes["colors_kitty"],
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

    def _load_contrast_palette(self, palette_entry_hash: str) -> dict[str, str] | None:
        """Load the palette ``colors.yaml`` into a token→hex dict (read-only).

        Returns ``None`` when the palette entry (or its ``colors.yaml``) is
        missing or yields no usable colors — the guard then passes the
        spine mappings through untouched.
        """
        colors_path = self._state_root / "cache" / "palettes" / palette_entry_hash / "colors.yaml"
        try:
            text = colors_path.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            return None
        palette = {k: v for k, v in _parse_palette_mapping(text).items() if v}
        return palette or None

    def _cached_wallpaper_path(self, palette_entry_hash: str) -> Path | None:
        """Resolve the cached wallpaper bytes for contrast sampling (read-only).

        The palette entry meta records its ``source_wallpaper_hash``; the
        sampler reads ``cache/wallpapers/<wh>/wallpaper.png`` without ever
        mutating it. ``None`` when the palette meta or cached bytes are
        unavailable — sampling is best-effort, the palette fallback covers it.
        """
        palette_dir = self._state_root / "cache" / "palettes" / palette_entry_hash
        try:
            meta = self._seeder.read_entry_meta(palette_dir)
            source_hash = meta.get("source_wallpaper_hash")
        except OSError, ValueError:
            return None
        if not isinstance(source_hash, str) or not source_hash:
            return None
        candidate = self._state_root / "cache" / "wallpapers" / source_hash / "wallpaper.png"
        try:
            return candidate if candidate.is_file() else None
        except OSError:
            return None

    def _read_spine_mappings_text(self, mappings_path: Path) -> str:
        """Read the spine mappings text the guard evaluates.

        File case: the ``icons.yaml`` itself. Dir case: the dir's
        ``icons.yaml`` (the only file the overlay patches). Raises when the
        text cannot be read — the caller degrades to the spine mappings.
        """
        if mappings_path.is_dir():
            candidate = mappings_path / "icons.yaml"
            if not candidate.is_file():
                raise RuntimeError(f"icon mappings dir has no icons.yaml: {mappings_path}")
            return candidate.read_text(encoding="utf-8")
        return mappings_path.read_text(encoding="utf-8")

    @staticmethod
    def _stage_icon_overlay(spine_mappings: Path, patched_text: str, stack: ExitStack) -> Path:
        """Materialize the overlay under a temp dir owned by ``stack``.

        File case: a patched ``icons.yaml``. Dir case: a full replication
        of the spine dir with only ``icons.yaml`` patched (same relpaths,
        so ``canonical_hash_dir`` stays deterministic). The temp dir lives
        in the system temp area (never under ``state_root``) and is removed
        when the enclosing ``ensure_icons`` scope exits.
        """
        tmp = stack.enter_context(tempfile.TemporaryDirectory(prefix="icon-contrast-overlay-"))
        overlay_root = Path(tmp)
        if spine_mappings.is_dir():
            for child in sorted(spine_mappings.iterdir()):
                dest = overlay_root / child.name
                try:
                    if child.is_symlink():
                        dest.symlink_to(os.readlink(child))
                    elif child.is_dir():
                        shutil.copytree(child, dest, symlinks=True)
                    else:
                        shutil.copy2(child, dest)
                except OSError as exc:
                    raise RuntimeError(
                        f"cannot replicate icon mappings dir {spine_mappings}: {exc}"
                    ) from exc
            (overlay_root / "icons.yaml").write_text(patched_text, encoding="utf-8")
            return overlay_root
        overlay_file = overlay_root / "icons.yaml"
        overlay_file.write_text(patched_text, encoding="utf-8")
        # ITR resolves its placeholder vocabulary as
        # ``yaml_path.parent / "defaults.yaml"`` (ITR ``VOCABULARY_FILENAME``;
        # absent file means an EMPTY vocabulary). Groups with empty
        # ``color_mappings`` (wlogout, email-client, wallpaper-selector) rely
        # on those defaults, so the overlay dir must carry the spine's
        # sibling vocabulary along — otherwise ``itr render`` fails with
        # ``Placeholder ... has no entry in color_mappings``. (Runtime must
        # not import the ITR package per AD-15, hence the literal filename.)
        vocab = spine_mappings.parent / "defaults.yaml"
        try:
            if vocab.is_file():
                shutil.copy2(vocab, overlay_root / "defaults.yaml")
        except OSError as exc:
            raise RuntimeError(f"cannot stage icon vocabulary {vocab}: {exc}") from exc
        return overlay_file

    def _effective_icon_mappings(
        self, stack: ExitStack, mappings_path: Path, palette_entry_hash: str
    ) -> tuple[Path, str, dict[str, Any] | None]:
        """Evaluate the guard → ``(render mappings path, its hash, contrast)``.

        Returns the spine path with a ``None`` contrast record when the
        guard has nothing to retarget or cannot evaluate (both keep the
        pre-guard cache key byte-stable). Raises on unexpected failure —
        the caller degrades to the spine mappings with a warning.
        """
        threshold = icon_contrast_threshold()
        allowed = icon_contrast_groups()
        band_px = icon_contrast_band_px()
        palette = self._load_contrast_palette(palette_entry_hash)
        if palette is None:
            logger.debug("icons: contrast guard skipped (palette colors.yaml unavailable)")
            return mappings_path, _hash_path_input(mappings_path, "icon mappings"), None
        wallpaper_path = self._cached_wallpaper_path(palette_entry_hash)
        sampled = (
            sample_top_strip_luminance(wallpaper_path, band_px)
            if wallpaper_path is not None
            else None
        )
        choice = _choose_backdrop_hex(sampled, palette)
        if choice is None:
            logger.debug("icons: contrast guard skipped (no usable backdrop hex)")
            return mappings_path, _hash_path_input(mappings_path, "icon mappings"), None
        backdrop_hex, source = choice
        spine_text = self._read_spine_mappings_text(mappings_path)
        scanned = _scan_group_color_mappings(spine_text)
        groups_config = {g: m for g, m in scanned.items() if g in allowed and m}
        overrides = decide_overrides(palette, backdrop_hex, groups_config, threshold)
        if not overrides:
            logger.debug(
                "icons: contrast guard evaluated (%s backdrop, threshold %.2f): no retarget",
                source,
                threshold,
            )
            return mappings_path, _hash_path_input(mappings_path, "icon mappings"), None
        patched = _patch_overlay_text(spine_text, overrides)
        overlay = self._stage_icon_overlay(mappings_path, patched, stack)
        # ``m_h`` hashes the overlay FILE itself: ``ItrAdapter`` recomputes
        # the entry hash from the exact path it receives
        # (``output_dir.name`` validation), so pipeline and adapter must
        # hash identically. The staged ``defaults.yaml`` copy intentionally
        # stays out of the key — mirroring the pre-guard spine key, which
        # is ``hash_file(icons.yaml)`` and likewise never covered the
        # vocabulary file.
        overlay_hash = _hash_path_input(overlay, "icon contrast overlay")
        decisions: list[dict[str, Any]] = []
        for (group, placeholder), new_token in sorted(overrides.items()):
            current = groups_config[group][placeholder]
            _, ratio_before, ratio_after = pick_best_token(backdrop_hex, current, palette)
            decisions.append(
                {
                    "group": group,
                    "placeholder": placeholder,
                    "from": current,
                    "to": new_token,
                    "ratio_before": round(ratio_before, 3),
                    "ratio_after": round(ratio_after, 3),
                }
            )
        logger.info(
            "icons: contrast guard retargeted %d placeholder(s) (%s backdrop, threshold %.2f)",
            len(decisions),
            source,
            threshold,
        )
        contrast: dict[str, Any] = {
            "backdrop_source": source,
            "threshold": threshold,
            "decisions": decisions,
        }
        return overlay, overlay_hash, contrast

    def ensure_icons(
        self,
        palette_entry_hash: str,
        *,
        contrast_enabled: bool = True,
        contrast_source: str = "default",
    ) -> tuple[IconsEntry, bool]:
        """Ensure the icons cache entry. Cache hit is entry-dir existence.

        Contrast guard (D1/D2/D3/D5/D6): group-level
        ``COLOR_FOREGROUND``/``COLOR_JOIN`` placeholders of allowlisted bar
        groups below threshold are retargeted in a staging-only overlay
        passed to ``self._itr.render`` (spine read-only); ``m_h`` hashes
        the EFFECTIVE mappings so ``ih`` stays content-addressed. A
        no-retarget evaluation reuses the spine path (pre-guard key
        stable); ANY guard exception degrades to the spine mappings with a
        warning — icons never hard-fail the caller.

        Per-wallpaper opt-out (``icon-contrast-opt-out`` D2): when
        ``contrast_enabled`` is ``False`` the guard is skipped entirely —
        the spine mappings render unchanged (pre-guard key, no overlay)
        and the entry records ``contrast.policy == {source, enabled:
        False}`` for ``inspect``/``doctor`` traceability.
        ``contrast_source`` (``"flag"``/``"store"``/``"default"``) names
        the provenance of the decision and is recorded in the policy.
        """
        if contrast_source not in POLICY_SOURCES:
            raise ValueError(
                f"invalid contrast source: {contrast_source!r} "
                f"(expected one of {', '.join(POLICY_SOURCES)})"
            )
        templates_dir = require_input("icon_templates", self._install_spine)
        mappings_path = require_input("icon_mappings", self._install_spine)

        templates_hash = _hash_path_input(templates_dir, "icon templates")

        with ExitStack() as stack:
            if not contrast_enabled:
                effective_mappings = mappings_path
                mappings_hash_val = _hash_path_input(mappings_path, "icon mappings")
                contrast: dict[str, Any] | None = {
                    "policy": {"source": contrast_source, "enabled": False}
                }
                logger.info(
                    "icons: contrast guard disabled by policy (source=%s); "
                    "using spine mappings",
                    contrast_source,
                )
            else:
                try:
                    effective_mappings, mappings_hash_val, guard_contrast = (
                        self._effective_icon_mappings(stack, mappings_path, palette_entry_hash)
                    )
                except Exception as exc:
                    logger.warning("icons: contrast guard failed (%s); using spine mappings", exc)
                    effective_mappings = mappings_path
                    mappings_hash_val = _hash_path_input(mappings_path, "icon mappings")
                    guard_contrast = None
                if guard_contrast is None:
                    contrast = None
                else:
                    contrast = {
                        **guard_contrast,
                        "policy": {"source": contrast_source, "enabled": True},
                    }

            ieh = icons_entry_hash(palette_entry_hash, templates_hash, mappings_hash_val)
            target = cache_entry_path(self._state_root, "icons", ieh)
            if target.exists():
                return self._seeder.load_icons_entry(target), True

            entry_holder: list[IconsEntry] = []

            def _populate(staging: Path) -> None:
                work = staging / ieh
                generated = self._itr.render(
                    palette_entry_hash, templates_dir, effective_mappings, work
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
                    contrast=contrast,
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
