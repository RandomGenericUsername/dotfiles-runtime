"""Prune keep-policy core (Story 3.2).

``PruneUseCase`` computes which cache entries are evictable under the AD-24
policy: keep ACTIVE (referenced by ``current.json``) ∪ SEED-PINNED (derived
from the seed history line) ∪ the last-N most recent per layer ∪ UNDATED
(unclassifiable → protected). Everything else is a removal candidate.

Plan only: this module never deletes. Story 3.3 owns the CLI, dry-run,
removal I/O, and logging. No I/O here — state comes through
``IStateRepository``; entries + pins are injected callables implemented in
``adapters/``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from runtime.domain.models import CacheEntryRef
from runtime.ports.state_repository import IStateRepository

#: Canonical layers (mirrors the cache layout / inspect order).
LAYERS: tuple[str, str, str, str] = ("wallpapers", "palettes", "effects", "icons")


@dataclass(frozen=True, slots=True)
class PrunePlan:
    """Removal candidates per layer + keep counts. Deleting is Story 3.3's job."""

    removals: dict[str, tuple[str, ...]]
    kept: dict[str, int]
    total_removable: int


class PruneUseCase:
    """Compute the evictable set under active + last-N + seed-pin + undated policy."""

    def __init__(
        self,
        state_repo: IStateRepository,
        entries_for: Callable[[str], Sequence[CacheEntryRef]],
        seed_pins: Callable[[], Mapping[str, set[str]]],
        keep: int = 5,
    ) -> None:
        if keep < 0:
            raise ValueError(f"keep must be >= 0, got {keep}")
        self._state_repo = state_repo
        self._entries_for = entries_for
        self._seed_pins = seed_pins
        self._keep = keep

    def run(self, prune_pinned: bool = False) -> PrunePlan:
        """Compute the removal plan. Read-only; mutates nothing.

        ``prune_pinned=True`` drops seed-pin protection (explicit opt-in);
        active + top-N + undated stay protected regardless.
        """
        active = self._active_hashes()
        pins = self._seed_pins()
        removals: dict[str, tuple[str, ...]] = {}
        kept: dict[str, int] = {}
        total = 0
        for layer in LAYERS:
            refs = list(self._entries_for(layer))
            dated = [ref for ref in refs if ref.timestamp is not None]
            undated = [ref for ref in refs if ref.timestamp is None]
            # ISO-8601 UTC timestamps sort lexicographically; newest first.
            dated.sort(key=lambda ref: (ref.timestamp, ref.entry_hash), reverse=True)
            protected = {ref.entry_hash for ref in dated[: self._keep]}
            protected.update(ref.entry_hash for ref in undated)
            protected.update(active.get(layer, set()))
            if not prune_pinned:
                protected.update(pins.get(layer, set()))
            candidates = sorted(ref.entry_hash for ref in refs if ref.entry_hash not in protected)
            removals[layer] = tuple(candidates)
            kept[layer] = len(refs) - len(candidates)
            total += len(candidates)
        return PrunePlan(removals=removals, kept=kept, total_removable=total)

    def _active_hashes(self) -> dict[str, set[str]]:
        """Active entries per layer from current.json. Absent state -> empty."""
        active: dict[str, set[str]] = {layer: set() for layer in LAYERS}
        state = self._state_repo.load_current()
        if state is None:
            return active
        active["wallpapers"].add(state.wallpaper.content_hash)
        active["wallpapers"].update(config.source_hash for config in state.monitors.values())
        if state.palette is not None:
            active["palettes"].add(state.palette.entry_hash)
        if state.effects is not None:
            active["effects"].add(state.effects.entry_hash)
        if state.icons is not None:
            active["icons"].add(state.icons.entry_hash)
        return active
