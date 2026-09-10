"""Read-side cache verification with lazy legacy annotation (Story 3.1).

``VerifyCacheUseCase`` decorates the existing single-walk listing
(``InspectCacheUseCase``) with a per-entry health verdict. It holds only the
lister (one directory scan, NFR-4) and an injected ``verify_entry`` callable
(the adapter seam — no adapter import in ``application/``). The sole write is
the AD-27 legacy annotation performed inside ``verify_entry``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from runtime.adapters.cache import cache_entry_path
from runtime.application.inspect import InspectCacheUseCase
from runtime.domain.models import EntryHealth


@dataclass(frozen=True, slots=True)
class VerifyCacheResult:
    """Outcome of one ``VerifyCacheUseCase.run`` invocation.

    ``layers`` maps each canonical layer to ``(hash, health)`` pairs aligned
    1:1 with the lister's sorted hashes; ``unhealthy`` counts entries whose
    status is not ``ok``.
    """

    layers: dict[str, tuple[tuple[str, EntryHealth], ...]]
    counts: dict[str, int]
    total: int
    unhealthy: int


class VerifyCacheUseCase:
    """Verify every listed cache entry; annotate legacy entries on read."""

    def __init__(
        self,
        state_root: Path,
        lister: InspectCacheUseCase,
        verify_entry: Callable[[Path], EntryHealth],
    ) -> None:
        self._state_root = state_root
        self._lister = lister
        self._verify_entry = verify_entry

    def run(self) -> VerifyCacheResult:
        """List once, verify each entry, aggregate. Read-only but for annotation."""
        listing = self._lister.run()
        layers: dict[str, tuple[tuple[str, EntryHealth], ...]] = {}
        unhealthy = 0
        for layer, hashes in listing.layers.items():
            items: list[tuple[str, EntryHealth]] = []
            for entry_hash in hashes:
                entry_dir = cache_entry_path(self._state_root, layer, entry_hash)
                health = self._verify_entry(entry_dir)
                if health.status != "ok":
                    unhealthy += 1
                items.append((entry_hash, health))
            layers[layer] = tuple(items)
        return VerifyCacheResult(
            layers=layers,
            counts=listing.counts,
            total=listing.total,
            unhealthy=unhealthy,
        )
