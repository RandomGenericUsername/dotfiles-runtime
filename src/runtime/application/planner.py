"""Declarative convergence planner (Phase 4, Story 4.5).

:func:`plan_convergence` is a thin composition over :func:`diff_states` for
callers that already hold both projections. :class:`ConvergeUseCase`
executes a non-empty :class:`ChangeSet` through injected executors —
testable without disk, mirroring the ``PruneUseCase`` injection precedent.
No I/O here; loader composition stays in the CLI (repo precedent).

Execution order: wallpaper converge FIRST (new derivations may create
entries prune must see), deletes SECOND against a post-converge refresh.
``pins_to_add`` is reported as pending (no pin store exists — Phase 5
question); ``pins_absent`` never triggers removal (AD-30 floor).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from runtime.application.diff import diff_states
from runtime.domain.models import ActualState, ChangeSet, DesiredState


def plan_convergence(
    desired: DesiredState,
    actual: ActualState,
    current_keep: int = 5,
) -> ChangeSet:
    """Compose the declarative gap from two already-loaded projections."""
    return diff_states(desired, actual, current_keep)


@dataclass(frozen=True, slots=True)
class ConvergenceReport:
    """Outcome of one :class:`ConvergeUseCase` run (mirrors ``PrunePlan``)."""

    wallpaper_set: str | None
    deleted: dict[str, tuple[str, ...]]
    pins_pending: tuple[str, ...]
    keep_target: int | None


class ConvergeUseCase:
    """Execute a :class:`ChangeSet`: converge wallpaper, then AD-30 deletes.

    ``set_wallpaper`` converges one wallpaper target (the CLI wires the
    existing ``_run_wallpaper_set`` pipeline — reuse, not re-implementation).
    ``remove_entry`` deletes one ``(layer, hash)`` (the CLI wires the
    ``remove_entry`` seam, which already validates + logs). ``refresh_actual``
    rebuilds the actual projection AFTER wallpaper convergence so deletes see
    fresh derivations; only its ``prunable_hashes`` are consumed (already
    floored by ``PruneUseCase`` — this class never re-implements the floor).
    A ``False`` return from ``remove_entry`` (absent target, idempotent
    no-op) is excluded from ``deleted``.
    """

    def __init__(
        self,
        set_wallpaper: Callable[[str], object],
        remove_entry: Callable[[str, str], bool],
        refresh_actual: Callable[[], ActualState],
    ) -> None:
        self._set_wallpaper = set_wallpaper
        self._remove_entry = remove_entry
        self._refresh_actual = refresh_actual

    def run(self, changeset: ChangeSet) -> ConvergenceReport:
        """Execute the gap.

        Read-only when the gap is empty AND nothing is prunable; AD-30
        deletes run regardless of ``is_empty`` (prunable is computed under
        the declared keep, which is itself intent). Per-entry ``OSError``
        continues (mirroring the ``_run_prune`` precedent) and raises
        ``RuntimeError`` with removed/failed counts at the end.
        """
        wallpaper_set: str | None = None
        if changeset.wallpaper_target is not None:
            self._set_wallpaper(changeset.wallpaper_target)
            wallpaper_set = changeset.wallpaper_target
        actual = self._refresh_actual()
        deleted: dict[str, tuple[str, ...]] = {}
        failures: list[str] = []
        for layer, hashes in actual.prunable_hashes.items():
            kept: list[str] = []
            for entry_hash in hashes:
                try:
                    if self._remove_entry(layer, entry_hash):
                        kept.append(entry_hash)
                except OSError:
                    failures.append(f"{layer}/{entry_hash}")
            deleted[layer] = tuple(kept)
        if failures:
            removed = sum(len(hashes) for hashes in deleted.values())
            raise RuntimeError(
                f"converge removed {removed}, failed {len(failures)}: {', '.join(failures)}"
            )
        return ConvergenceReport(
            wallpaper_set=wallpaper_set,
            deleted=deleted,
            pins_pending=tuple(changeset.pins_to_add),
            keep_target=changeset.keep_target,
        )
