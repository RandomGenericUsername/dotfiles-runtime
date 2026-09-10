"""Pure invalidation computation over the derivation graph.

Zero-I/O helpers for Phase 3 invalidation-awareness (AD-21): given per-layer
derivation-input hashes (recorded in ``meta.json`` vs recomputed from the spine),
compute the stale set closed under the pinned cascade rule.

Cascade rule (AR-2/AD-21):
    wallpaper -> palette + effects; palette -> icons.

Canonical per-layer derivation inputs (shared-data-contract.md):
    palette <- (wallpaper_hash, template_set_hash)
    effects <- (wallpaper_hash, catalog_hash)
    icons   <- (palette_hash, templates_hash, mappings_hash)

STALENESS vs CORRUPTION boundary (pinned 2026-09-10 fold-in): these helpers
classify STALENESS — recorded input hashes differing from recomputed spine
inputs. Artifact-hash mismatch is NOT staleness; it is the corrupt-by-digest
domain of Story 1.5 (``CorruptCacheError`` at populate) and doctor ``--verify``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Final, Literal

DerivationLayer = Literal["wallpapers", "palettes", "effects", "icons"]

#: Layers in canonical pipeline order (also surfaced as ``application.inspect.CACHE_LAYER_ORDER``).
LAYER_ORDER: Final[tuple[DerivationLayer, ...]] = (
    "wallpapers",
    "palettes",
    "effects",
    "icons",
)

#: Downstream closure edges: a stale layer invalidates its downstream set.
#: wallpaper -> palette + effects; palette -> icons (AR-2/AD-21).
CASCADE_DOWNSTREAM: Final[Mapping[DerivationLayer, frozenset[DerivationLayer]]] = {
    "wallpapers": frozenset({"palettes", "effects"}),
    "palettes": frozenset({"icons"}),
    "effects": frozenset(),
    "icons": frozenset(),
}


def close_stale_set(
    directly_stale: frozenset[DerivationLayer] | set[DerivationLayer] | Collection[DerivationLayer],
) -> frozenset[DerivationLayer]:
    """Close ``directly_stale`` under the cascade rule.

    Returns the input set plus every downstream layer reachable via
    ``CASCADE_DOWNSTREAM``. Unknown layer names raise ``ValueError`` loudly
    (no silent fallback).

    Pure: no I/O, deterministic, order-independent input.
    """
    unknown = set(directly_stale) - set(CASCADE_DOWNSTREAM)
    if unknown:
        raise ValueError(f"unknown derivation layer(s): {sorted(unknown, key=repr)}")
    closed: set[DerivationLayer] = set(directly_stale)
    frontier = list(directly_stale)
    while frontier:
        layer = frontier.pop()
        for downstream in CASCADE_DOWNSTREAM[layer]:
            if downstream not in closed:
                closed.add(downstream)
                frontier.append(downstream)
    return frozenset(closed)


def diff_input_hashes(
    recorded: Mapping[DerivationLayer, str | None],
    recomputed: Mapping[DerivationLayer, str | None],
) -> frozenset[DerivationLayer]:
    """Return layers whose derivation inputs changed (NOT cascade-closed).

    A layer is directly stale when its recorded input hash differs from the
    recomputed one, or when either side is missing (``None`` or empty string:
    absent entry or absent spine input — fail loud as stale, never as fresh).
    Non-string values raise ``ValueError`` (corrupt input contract). Feed the
    result into :func:`close_stale_set` for the cascade-closed stale set.

    Pure: no I/O, deterministic.
    """
    layers = set(recorded) | set(recomputed)
    unknown = layers - set(CASCADE_DOWNSTREAM)
    if unknown:
        raise ValueError(f"unknown derivation layer(s): {sorted(unknown, key=repr)}")
    stale: set[DerivationLayer] = set()
    for layer in layers:
        old = recorded.get(layer)
        new = recomputed.get(layer)
        if old is None or new is None or old == "" or new == "":
            stale.add(layer)  # missing input: fail loud as stale, never fresh
            continue
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError(f"invalid derivation input hash for layer {layer!r}")
        if old != new:
            stale.add(layer)
    return frozenset(stale)
