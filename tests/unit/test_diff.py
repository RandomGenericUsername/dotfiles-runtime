"""Unit tests for the desired-vs-actual diff (Phase 4, Story 4.4)."""

from __future__ import annotations

import dataclasses

import pytest

from runtime.application.diff import diff_states
from runtime.domain.models import ActualState, ChangeSet, DesiredState

WALL = "/img/wall.png"
OTHER_WALL = "/img/other.png"
PIN_A = "aa" * 32
PIN_B = "bb" * 32
PIN_C = "cc" * 32


def _desired(**kwargs: object) -> DesiredState:
    base: dict[str, object] = {"wallpaper": WALL, "keep": 5, "pinned": (PIN_A, PIN_B)}
    base.update(kwargs)
    return DesiredState(**base)  # type: ignore[arg-type]


def _actual(**kwargs: object) -> ActualState:
    base: dict[str, object] = {
        "current_wallpaper": WALL,
        "monitors": ("DP-1",),
        "prunable_hashes": {},
        "pinned_hashes": (PIN_B, PIN_A),
        "undated_hashes": (),
    }
    base.update(kwargs)
    return ActualState(**base)  # type: ignore[arg-type]


def test_converged_pair_is_empty() -> None:
    changeset = diff_states(_desired(), _actual())
    assert changeset == ChangeSet(
        wallpaper_target=None, pins_to_add=(), pins_absent=(), keep_target=None
    )
    assert changeset.is_empty is True


def test_fresh_machine_diverges() -> None:
    actual = _actual(current_wallpaper=None, pinned_hashes=(), monitors=())
    changeset = diff_states(_desired(), actual)
    assert changeset.wallpaper_target == WALL
    assert changeset.pins_to_add == tuple(sorted((PIN_A, PIN_B)))
    assert changeset.pins_absent == ()
    assert changeset.keep_target is None
    assert changeset.is_empty is False


def test_wallpaper_only_change() -> None:
    changeset = diff_states(_desired(), _actual(current_wallpaper=OTHER_WALL))
    assert changeset.wallpaper_target == WALL
    assert changeset.pins_to_add == ()
    assert changeset.pins_absent == ()
    assert changeset.keep_target is None
    assert changeset.is_empty is False


def test_pins_added_only() -> None:
    changeset = diff_states(_desired(pinned=(PIN_A, PIN_B, PIN_C)), _actual())
    assert changeset.wallpaper_target is None
    assert changeset.pins_to_add == (PIN_C,)
    assert changeset.pins_absent == ()
    assert changeset.is_empty is False


def test_pins_absent_is_informational() -> None:
    # Seed-pin present in actual but absent from desired is REPORTED, never
    # removed by the diff — protection policy lives with the planner (AD-30).
    # It does NOT block convergence: nothing actionable remains.
    changeset = diff_states(_desired(pinned=(PIN_A,)), _actual())
    assert changeset.pins_absent == (PIN_B,)
    assert changeset.pins_to_add == ()
    assert changeset.is_empty is True


def test_keep_only_change() -> None:
    changeset = diff_states(_desired(keep=3), _actual())
    assert changeset.keep_target == 3
    assert changeset.wallpaper_target is None
    assert changeset.pins_to_add == ()
    assert changeset.pins_absent == ()
    assert changeset.is_empty is False


def test_current_keep_override() -> None:
    assert diff_states(_desired(), _actual(), current_keep=3).keep_target == 5
    assert diff_states(_desired(keep=3), _actual(), current_keep=3).keep_target is None


@pytest.mark.parametrize("bad_keep", [-1, True, False, "5", 5.0, None])
def test_bad_current_keep_raises(bad_keep: object) -> None:
    with pytest.raises(ValueError, match="current_keep"):
        diff_states(_desired(), _actual(), current_keep=bad_keep)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"current_wallpaper": OTHER_WALL},
        {"pinned_hashes": (PIN_A,)},
    ],
)
def test_single_divergence_flips_is_empty(kwargs: object) -> None:
    assert diff_states(_desired(), _actual(**kwargs)).is_empty is False  # type: ignore[arg-type]


def test_pins_absent_alone_still_converged() -> None:
    assert diff_states(_desired(), _actual(pinned_hashes=(PIN_A, PIN_B, PIN_C))).is_empty is True


def test_keep_divergence_flips_is_empty() -> None:
    assert diff_states(_desired(keep=7), _actual()).is_empty is False


def test_model_is_frozen() -> None:
    changeset = diff_states(_desired(), _actual())
    with pytest.raises(dataclasses.FrozenInstanceError):
        changeset.keep_target = 1  # type: ignore[misc]
