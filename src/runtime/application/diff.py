"""Desired-vs-actual diff (Phase 4, Story 4.4).

:func:`diff_states` computes the gap between declared intent
(:class:`DesiredState`) and observed reality (:class:`ActualState`) as a
:class:`ChangeSet` for the future planner. Pure function of two value
objects plus an int — no ports, no adapters, no ``PruneUseCase``. The diff
consumes projections, never raw sources.

``pins_absent`` is informational only: seed-pins are historical facts and
stay protected per the AD-30 floor; unpinning semantics belong to the
planner (p4-3-2), which defaults to no unpin.
"""

from __future__ import annotations

from runtime.domain.models import ActualState, ChangeSet, DesiredState


def diff_states(
    desired: DesiredState,
    actual: ActualState,
    current_keep: int = 5,
) -> ChangeSet:
    """Diff declared intent against observed reality.

    ``current_keep`` is the keep currently in effect (default 5 = the AD-30
    code default the desired declaration overrides); strictly validated —
    ``bool``/non-``int``/negative → ``ValueError`` (``True`` must not slip
    in as ``1``, mirroring the p4-2-1 strictness precedent).
    """
    if isinstance(current_keep, bool) or not isinstance(current_keep, int):
        raise ValueError(f"current_keep must be an integer >= 0, got {current_keep!r}")
    if current_keep < 0:
        raise ValueError(f"current_keep must be an integer >= 0, got {current_keep!r}")
    wallpaper_target = None if desired.wallpaper == actual.current_wallpaper else desired.wallpaper
    desired_pins = set(desired.pinned)
    actual_pins = set(actual.pinned_hashes)
    keep_target = None if desired.keep == current_keep else desired.keep
    return ChangeSet(
        wallpaper_target=wallpaper_target,
        pins_to_add=tuple(sorted(desired_pins - actual_pins)),
        pins_absent=tuple(sorted(actual_pins - desired_pins)),
        keep_target=keep_target,
    )
