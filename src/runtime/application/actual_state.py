"""Actual-state projection (Phase 4, Story 4.3).

:func:`build_actual_state` collapses everything the desktop has actually got
into one :class:`ActualState` value object for the future diff engine.
Pure orchestration over injected callables — the same shapes ``PruneUseCase``
takes, so Epic 3 can pass the same adapter callables to both. No I/O here;
the caller supplies the already-loaded ``current`` (disk wiring is Epic 3
CLI scope).

Prune policy has exactly one source: ``prunable_hashes`` is computed by
delegating to ``PruneUseCase.run()``, never by re-implementing the
active/last-N/seed-pin/undated rules.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from runtime.application.prune import LAYERS, PruneUseCase
from runtime.domain.models import ActualState, CacheEntryRef, DesktopState
from runtime.ports.state_repository import IStateRepository


class _LoadedStateRepository(IStateRepository):
    """Projection-local repo stub serving an already-loaded ``current``.

    ``PruneUseCase`` needs a repository; this builder takes loaded state, so
    the stub bridges the two. ``save`` is ``NotImplementedError`` by design —
    projections never write (documented, not accidental).
    """

    def __init__(self, current: DesktopState | None) -> None:
        self._current = current

    def load_current(self) -> DesktopState | None:
        return self._current

    def save(self, state: DesktopState) -> None:
        raise NotImplementedError("actual-state projection never writes")


def build_actual_state(
    current: DesktopState | None,
    entries_for: Callable[[str], Sequence[CacheEntryRef]],
    seed_pins: Callable[[], Mapping[str, set[str]]],
    keep: int = 5,
    prune_pinned: bool = False,
) -> ActualState:
    """Project observed reality into an :class:`ActualState`.

    ``current=None`` (fresh machine) yields empty wallpaper/monitors while
    entries are still classified from the injected callables alone. Each
    layer is listed at most once. ``keep`` validation is delegated to
    ``PruneUseCase`` (``keep < 0`` → ``ValueError``) — one source for the rule.
    ``prune_pinned`` is forwarded to the prune plan (Story 4.5 Item 1).
    """
    refs_by_layer: dict[str, list[CacheEntryRef]] = {}
    pins_cache: dict[str, Mapping[str, set[str]]] = {}

    def _memo_pins() -> Mapping[str, set[str]]:
        if "pins" not in pins_cache:
            pins_cache["pins"] = seed_pins()
        return pins_cache["pins"]

    # Constructed before any listing: __init__ validates keep (keep < 0
    # raises here) without invoking the injected callables — fail-fast with
    # no duplicated rule. The lambdas capture the containers by default-arg;
    # both are populated before run().
    usecase = PruneUseCase(
        _LoadedStateRepository(current),
        lambda layer, _c=refs_by_layer: _c[layer],
        _memo_pins,
        keep,
    )
    refs_by_layer.update({layer: list(entries_for(layer)) for layer in LAYERS})
    pins = _memo_pins()
    pinned = sorted({entry_hash for layer in LAYERS for entry_hash in pins.get(layer, set())})
    undated = sorted(
        {
            ref.entry_hash
            for layer in LAYERS
            for ref in refs_by_layer[layer]
            if ref.timestamp is None
        }
    )
    plan = usecase.run(prune_pinned=prune_pinned)
    if current is None:
        current_wallpaper: str | None = None
        monitors: tuple[str, ...] = ()
    else:
        current_wallpaper = current.wallpaper.source_path
        monitors = tuple(sorted(current.monitors))
    return ActualState(
        current_wallpaper=current_wallpaper,
        monitors=monitors,
        prunable_hashes=dict(plan.removals),
        pinned_hashes=tuple(pinned),
        undated_hashes=tuple(undated),
    )
