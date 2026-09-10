"""Spine input-hash walk adapter implementing IInvalidationQuery (Phase 3, Story 1.2).

Implements AD-21 (invalidation is hash-compare only): recompute spine input
hashes (csg templates, weg catalog, icon templates/mappings) with the Phase 2
canonicalization and compare against per-layer ``meta.json``.

Recorded field map (cache.py docstring + shared-data-contract.md):
  palette <- ``input_template_hash``
  effects <- ``input_catalog_hash``
  icons   <- ``input_templates_hash`` + ``input_mappings_hash``
Structural links (``source_wallpaper_hash``, ``source_palette_hash``) are NEVER
compared — entry identity, not staleness. Artifact hashes are NEVER compared
here — artifact-hash mismatch is corrupt-by-digest (Story 1.5 / doctor), not
staleness (boundary pinned 2026-09-10).

Conventions pinned (Stories 1.3/1.4 rely on them):
  1. Icons two-input encoding is ``f"{templates}\\x00{mappings}"`` — an
     equality-encoding, not a hash (same separator convention as
     ``canonical_hash_dir`` D2). Recorded and recomputed sides MUST use
     identical field order. AD-21 holds: no new hash formulas, no layout change.
  2. ``wallpapers`` excluded from input-hash compare (content-addressed
     invariant: a changed wallpaper is a NEW entry, never stale; freshness is
     structural, owned by doctor Story 2.1).
  3. Missing ``meta.json`` -> ``None`` (stale; regen rebuilds). Unparseable
     ``meta.json`` -> ``ValueError`` naming the entry (loud; corruption
     taxonomy belongs to doctor Story 2.1).
  4. Composition owns discovery: ctor takes resolved spine paths (``None`` =
     absent input). The adapter NEVER imports ``find_*`` from
     ``application/derive.py`` (layering: adapters never import application);
     callers resolve via ``derive.find_*`` and inject.

References:
  - AD-21 hash-compare only; AD-25 new units land in existing layers
  - canonicalization source: ``application/derive.py::_hash_path_input``
    (re-implemented locally below; adapters must not import application)
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from pathlib import Path

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.hashing import canonical_hash_dir, hash_file
from runtime.domain.invalidation import (
    DerivationLayer,
    close_stale_set,
    diff_input_hashes,
)
from runtime.ports.invalidation_query import IInvalidationQuery

#: Separator for multi-input layer encodings (mirrors canonical_hash_dir D2).
_INPUT_JOINER = "\x00"


def _present_str(value: object) -> str | None:
    """Return ``value`` if it is a non-empty string, else ``None``.

    One representation of missing at the I/O boundary: ``None``, ``""``,
    and wrong types all degrade to ``None`` (fail-safe toward regen).
    """
    if not isinstance(value, str) or value == "":
        return None
    return value


def _hash_dir_input(path: Path | None, what: str) -> str | None:
    """Hash a dir-only spine input (csg templates, icon templates).

    ``None`` (unresolved discovery) -> ``None`` (absent fails loud as stale
    downstream). A non-dir path raises ``RuntimeError`` (wrong-kind inputs
    must fail loud, never hash as the wrong kind). Read/hash failures raise
    ``RuntimeError`` with path context — spine environment problems are loud;
    ``None`` stays reserved for absent-by-configuration.
    """
    if path is None:
        return None
    if not path.is_dir():
        raise RuntimeError(f"{what} path must be a dir: {path}")
    try:
        return canonical_hash_dir(path)
    except OSError as exc:
        raise RuntimeError(f"cannot hash {what} path {path}: {exc}") from exc


def _hash_file_input(path: Path | None, what: str) -> str | None:
    """Hash a file-only spine input (weg catalog). Same loudness contract."""
    if path is None:
        return None
    if not path.is_file():
        raise RuntimeError(f"{what} path must be a file: {path}")
    try:
        return hash_file(path)
    except OSError as exc:
        raise RuntimeError(f"cannot hash {what} path {path}: {exc}") from exc


def _hash_path_input(path: Path | None, what: str) -> str | None:
    """Hash a polymorphic spine input (icon mappings: file OR dir).

    Canonicalization mirrors ``application/derive.py::_hash_path_input``
    (never imported: layering) — dir -> ``canonical_hash_dir``, file ->
    ``hash_file``.
    """
    if path is None:
        return None
    try:
        if path.is_dir():
            return canonical_hash_dir(path)
        if path.is_file():
            return hash_file(path)
    except OSError as exc:
        raise RuntimeError(f"cannot hash {what} path {path}: {exc}") from exc
    raise RuntimeError(f"{what} path is neither a dir nor a file: {path}")


class InvalidationQueryAdapter(IInvalidationQuery):
    """Hash-compare invalidation over spine inputs vs cache ``meta.json``.

    All filesystem I/O lives here (AD-25). Pure set computation delegates to
    ``runtime.domain.invalidation``.
    """

    def __init__(
        self,
        state_root: Path,
        templates_dir: Path | None = None,
        catalog_path: Path | None = None,
        icon_templates: Path | None = None,
        icon_mappings: Path | None = None,
    ) -> None:
        self._state_root = state_root
        self._templates_dir = templates_dir
        self._catalog_path = catalog_path
        self._icon_templates = icon_templates
        self._icon_mappings = icon_mappings

    def recompute_input_hashes(self) -> Mapping[DerivationLayer, str | None]:
        """Recompute current spine input hashes, keyed by derivation layer.

        ``wallpapers`` has no spine inputs (content-addressed) and is always
        ``None`` here; :meth:`compare_against_meta` excludes it (convention 2).
        Any unresolved (``None``) spine input yields ``None`` for its layer.
        """
        templates_hash = _hash_dir_input(self._templates_dir, "csg templates")
        catalog_hash = _hash_file_input(self._catalog_path, "weg catalog")
        icon_templates_hash = _hash_dir_input(self._icon_templates, "icon templates")
        icon_mappings_hash = _hash_path_input(self._icon_mappings, "icon mappings")
        if icon_templates_hash is None or icon_mappings_hash is None:
            icons_hash: str | None = None
        else:
            icons_hash = f"{icon_templates_hash}{_INPUT_JOINER}{icon_mappings_hash}"
        return {
            "wallpapers": None,
            "palettes": templates_hash,
            "effects": catalog_hash,
            "icons": icons_hash,
        }

    def recorded_inputs(self, layer: DerivationLayer, entry_hash: str) -> str | None:
        """Return the recorded input encoding for one cache entry.

        Reads ``state_root/cache/<layer>/<entry_hash>/meta.json`` and extracts
        the input fields in the recorded field map. Missing ``meta.json`` ->
        ``None`` (stale; regen rebuilds). ``meta.json``-that-is-a-directory ->
        ``ValueError`` (corruption: regen would no-op against the existing
        entry dir, so only doctor quarantine clears it). Other unreadable
        ``meta.json`` -> ``None`` (fail-safe toward regen, never toward fresh).
        Unparseable ``meta.json`` -> ``ValueError`` naming the entry.
        Wrong-type/empty fields -> ``None`` (one representation of missing).
        """
        if layer not in ("wallpapers", "palettes", "effects", "icons"):
            raise ValueError(f"unknown derivation layer: {layer!r}")
        meta_path = cache_entry_path(self._state_root, layer, entry_hash) / "meta.json"
        try:
            raw = meta_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except IsADirectoryError as exc:
            raise ValueError(f"corrupt meta.json for {layer}/{entry_hash}: not a file") from exc
        except OSError:
            return None
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt meta.json for {layer}/{entry_hash}: {exc}") from exc
        if not isinstance(meta, dict):
            raise ValueError(f"corrupt meta.json for {layer}/{entry_hash}: not an object")
        if layer == "wallpapers":
            # Content-addressed: never stale-by-input; compare drops this key.
            # Nothing to compare, so nothing to return.
            return None
        if layer == "palettes":
            return _present_str(meta.get("input_template_hash"))
        if layer == "effects":
            return _present_str(meta.get("input_catalog_hash"))
        templates_hash = _present_str(meta.get("input_templates_hash"))
        mappings_hash = _present_str(meta.get("input_mappings_hash"))
        if templates_hash is None or mappings_hash is None:
            return None
        return f"{templates_hash}{_INPUT_JOINER}{mappings_hash}"

    def compare_against_meta(
        self,
        recorded: Mapping[DerivationLayer, str | None],
        recomputed: Mapping[DerivationLayer, str | None],
    ) -> frozenset[DerivationLayer]:
        """Return the cascade-closed stale set for recorded vs recomputed inputs.

        The ``wallpapers`` layer is excluded from both mappings before
        delegating to the domain helpers (convention 2: content-addressed
        invariant). Everything else compares by hash-equality only (AD-21).
        """
        recorded_layers: dict[DerivationLayer, str | None] = {
            k: v for k, v in recorded.items() if k != "wallpapers"
        }
        recomputed_layers: dict[DerivationLayer, str | None] = {
            k: v for k, v in recomputed.items() if k != "wallpapers"
        }
        return close_stale_set(diff_input_hashes(recorded_layers, recomputed_layers))

    def stale_set_with_cascade(
        self,
        directly_stale: frozenset[DerivationLayer]
        | set[DerivationLayer]
        | Collection[DerivationLayer],
    ) -> frozenset[DerivationLayer]:
        """Close a directly-stale layer set under the cascade rule."""
        return close_stale_set(directly_stale)
