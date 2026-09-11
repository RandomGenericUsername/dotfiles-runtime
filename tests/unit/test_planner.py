"""Unit tests for the convergence planner (Phase 4, Story 4.5)."""

from __future__ import annotations

import pytest

from runtime.application.planner import ConvergenceReport, ConvergeUseCase, plan_convergence
from runtime.domain.models import ActualState, ChangeSet, DesiredState

WALL = "/img/wall.png"
PIN_A = "aa" * 32
PIN_B = "bb" * 32


def _changeset(**kwargs: object) -> ChangeSet:
    base: dict[str, object] = {
        "wallpaper_target": None,
        "pins_to_add": (),
        "pins_absent": (),
        "keep_target": None,
    }
    base.update(kwargs)
    return ChangeSet(**base)  # type: ignore[arg-type]


def _actual(prunable: dict[str, tuple[str, ...]]) -> ActualState:
    return ActualState(
        current_wallpaper=WALL,
        monitors=(),
        prunable_hashes=prunable,
        pinned_hashes=(),
        undated_hashes=(),
    )


class _Spies:
    def __init__(self, remove_results: dict[tuple[str, str], bool] | None = None) -> None:
        self.set_calls: list[str] = []
        self.remove_calls: list[tuple[str, str]] = []
        self.refresh_calls = 0
        self._remove_results = remove_results or {}

    def set_wallpaper(self, target: str) -> None:
        self.set_calls.append(target)

    def remove_entry(self, layer: str, entry_hash: str) -> bool:
        self.remove_calls.append((layer, entry_hash))
        return self._remove_results.get((layer, entry_hash), True)

    def refresh(self, prunable: dict[str, tuple[str, ...]]) -> ActualState:
        self.refresh_calls += 1
        return _actual(prunable)


def test_empty_gap_makes_no_calls() -> None:
    spies = _Spies()
    report = ConvergeUseCase(
        spies.set_wallpaper, spies.remove_entry, lambda: spies.refresh({})
    ).run(_changeset())
    assert report == ConvergenceReport(
        wallpaper_set=None, deleted={}, pins_pending=(), keep_target=None
    )
    assert spies.set_calls == []
    assert spies.remove_calls == []


def test_wallpaper_converge_then_deletes() -> None:
    order: list[str] = []
    spies = _Spies()

    def _set(target: str) -> None:
        order.append("set")
        spies.set_wallpaper(target)

    def _refresh() -> ActualState:
        order.append("refresh")
        return spies.refresh({"wallpapers": ("01" * 32,)})

    report = ConvergeUseCase(_set, spies.remove_entry, _refresh).run(
        _changeset(wallpaper_target=WALL, pins_to_add=(PIN_A,), keep_target=3)
    )
    assert order == ["set", "refresh"]
    assert report.wallpaper_set == WALL
    assert report.deleted == {"wallpapers": ("01" * 32,)}
    assert report.pins_pending == (PIN_A,)
    assert report.keep_target == 3


def test_remove_false_excluded_from_deleted() -> None:
    gone, stayed = "01" * 32, "02" * 32
    spies = _Spies(remove_results={("wallpapers", stayed): False})
    report = ConvergeUseCase(
        spies.set_wallpaper,
        spies.remove_entry,
        lambda: spies.refresh({"wallpapers": (gone, stayed)}),
    ).run(_changeset())
    assert report.deleted == {"wallpapers": (gone,)}
    assert ("wallpapers", stayed) in spies.remove_calls


def test_pins_absent_never_triggers_calls() -> None:
    spies = _Spies()
    report = ConvergeUseCase(
        spies.set_wallpaper, spies.remove_entry, lambda: spies.refresh({})
    ).run(_changeset(pins_absent=(PIN_B,)))
    assert spies.set_calls == []
    assert spies.remove_calls == []
    assert report.pins_pending == ()


def test_plan_convergence_delegates_to_diff() -> None:
    from runtime.application.diff import diff_states

    desired = DesiredState(wallpaper=WALL, keep=5, pinned=())
    actual = _actual({})
    assert plan_convergence(desired, actual) == diff_states(desired, actual)
    with pytest.raises(ValueError, match="current_keep"):
        plan_convergence(desired, actual, current_keep=-1)


def test_empty_changeset_still_enforces_prunable() -> None:
    """AD-30 deletes run regardless of is_empty (Item 5)."""
    spies = _Spies()
    report = ConvergeUseCase(
        spies.set_wallpaper,
        spies.remove_entry,
        lambda: spies.refresh({"wallpapers": ("01" * 32,)}),
    ).run(_changeset())
    assert spies.set_calls == []
    assert report.deleted == {"wallpapers": ("01" * 32,)}


def test_remove_oserror_continues_and_reports_counts() -> None:
    """Mirror the _run_prune precedent: per-entry continue + counts (Item 6)."""
    gone, failed = "01" * 32, "02" * 32
    spies = _Spies()

    def _flaky(layer: str, entry_hash: str) -> bool:
        spies.remove_calls.append((layer, entry_hash))
        if entry_hash == failed:
            raise OSError("disk gone")
        return True

    with pytest.raises(RuntimeError, match=r"converge removed 1, failed 1"):
        ConvergeUseCase(
            spies.set_wallpaper,
            _flaky,
            lambda: spies.refresh({"wallpapers": (gone, failed)}),
        ).run(_changeset())
    assert ("wallpapers", gone) in spies.remove_calls
