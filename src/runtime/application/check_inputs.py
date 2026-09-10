"""Read-only stale-input report (Phase 3, Story 1.3).

``CheckInputsUseCase`` answers "which derivation layers are stale?" without
mutating anything: no seeder, no mutex, no derivation adapters, no reloaders —
the constructor signature IS the read-only guarantee. Hash walk only, so a
warm cache completes with zero csg/weg/itr invocations (NFR-4).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from runtime.domain.invalidation import DerivationLayer
from runtime.ports.invalidation_query import IInvalidationQuery
from runtime.ports.state_repository import IStateRepository

#: Derived layers covered by the report (wallpapers excluded: content-addressed,
#: freshness is structural — convention pinned in Story 1.2).
REPORT_LAYERS: tuple[DerivationLayer, ...] = ("palettes", "effects", "icons")


@dataclass(frozen=True, slots=True)
class CheckInputsResult:
    """Outcome of a read-only stale-input check."""

    stale: frozenset[DerivationLayer]
    fresh: frozenset[DerivationLayer]


class CheckInputsUseCase:
    """Compare recorded vs recomputed derivation inputs; report stale/fresh.

    Read-only: loads ``current.json`` (absent -> ``ValueError``), reads each
    active entry's recorded inputs through the injected ``recorded_inputs``
    reader (wired to ``InvalidationQueryAdapter.recorded_inputs`` by the
    composition root — pass the bound method of the same adapter instance
    passed as ``invalidation``; the reader is I/O and lives in ``adapters/``,
    this use case only declares the signature it needs), recomputes spine
    hashes via the port, and delegates the stale-set computation to the port. Layers
    absent from ``current.json`` are omitted from ``recorded`` — a missing
    side is stale per p3-1-1 semantics (derivable-but-uncached is truthfully
    stale; Story 1.4 regen covers the miss path).
    """

    def __init__(
        self,
        state_repo: IStateRepository,
        invalidation: IInvalidationQuery,
        recorded_inputs: Callable[[DerivationLayer, str], str | None],
    ) -> None:
        self._state_repo = state_repo
        self._invalidation = invalidation
        self._recorded_inputs = recorded_inputs

    def run(self) -> CheckInputsResult:
        """Run the check; return stale/fresh layer sets. Mutates nothing."""
        state = self._state_repo.load_current()
        if state is None:
            raise ValueError(
                "no runtime state recorded yet (missing current.json); "
                "run `dotfiles-runtime wallpaper set <img>` first"
            )
        entries: Mapping[DerivationLayer, str | None] = {
            "palettes": state.palette.entry_hash if state.palette else None,
            "effects": state.effects.entry_hash if state.effects else None,
            "icons": state.icons.entry_hash if state.icons else None,
        }
        recorded: dict[DerivationLayer, str | None] = {}
        for layer in REPORT_LAYERS:
            entry_hash = entries[layer]
            if entry_hash is None:
                continue
            recorded[layer] = self._recorded_inputs(layer, entry_hash)
        stale = self._invalidation.compare_against_meta(
            recorded, self._invalidation.recompute_input_hashes()
        )
        unexpected = set(stale) - set(REPORT_LAYERS)
        if unexpected:
            raise ValueError(f"unexpected layer(s) in stale set: {sorted(unexpected)}")
        return CheckInputsResult(
            stale=frozenset(stale), fresh=frozenset(set(REPORT_LAYERS) - set(stale))
        )
