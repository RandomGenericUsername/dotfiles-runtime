"""Unit tests for the actual-state projection (Phase 4, Story 4.3)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from runtime.application.actual_state import _LoadedStateRepository, build_actual_state
from runtime.application.prune import LAYERS, PruneUseCase
from runtime.domain.models import (
    ActualState,
    BackendType,
    CacheEntryRef,
    DesktopState,
    EffectsEntry,
    FitMode,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteEntry,
    WallpaperEntry,
)

WH = "dd" * 32
PH = "aa" * 32
EH = "bb" * 32
IH = "cc" * 32


def _desktop_state(**kwargs: Any) -> DesktopState:
    monitors = kwargs.pop("monitors", {})
    base = {
        "schema_version": 2,
        "wallpaper": WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=WH,
            source_path="/img/wall.png",
            imported_at="2026-09-10T00:00:00Z",
        ),
        "monitors": monitors,
        "palette": PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=PH,
            source_wallpaper_hash=WH,
            input_template_hash="t" * 64,
            artifact_hashes={},
            generated_at="2026-09-10T00:00:00Z",
        ),
        "effects": EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=EH,
            source_wallpaper_hash=WH,
            input_catalog_hash="c" * 64,
            artifact_hashes={},
            generated_at="2026-09-10T00:00:00Z",
        ),
        "icons": IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=IH,
            source_palette_hash=PH,
            input_templates_hash="i" * 64,
            input_mappings_hash="m" * 64,
            artifact_hashes={},
            generated_at="2026-09-10T00:00:00Z",
        ),
        "applied_at": "2026-09-10T00:00:00Z",
    }
    base.update(kwargs)
    return DesktopState(**base)  # type: ignore[arg-type]


def _ref(entry_hash: str, day: int | None) -> CacheEntryRef:
    ts = f"2026-09-{day:02d}T00:00:00Z" if day is not None else None
    return CacheEntryRef(entry_hash=entry_hash, timestamp=ts)


class _CountingEntries:
    """entries_for fake recording per-layer call counts (no double listing)."""

    def __init__(self, refs: Mapping[str, Sequence[CacheEntryRef]]) -> None:
        self._refs = refs
        self.calls: dict[str, int] = {layer: 0 for layer in LAYERS}

    def __call__(self, layer: str) -> Sequence[CacheEntryRef]:
        self.calls[layer] += 1
        return self._refs[layer]


OLD_WALL = "01" * 32
NEW_WALL = "02" * 32
UNDATED_WALL = "03" * 32
PINNED_PAL = "04" * 32
UNDATED_FX = "05" * 32
PINNED_FX = "06" * 32

REFS: dict[str, list[CacheEntryRef]] = {
    "wallpapers": [_ref(WH, 10), _ref(NEW_WALL, 11), _ref(OLD_WALL, 1), _ref(UNDATED_WALL, None)],
    "palettes": [_ref(PH, 10), _ref(PINNED_PAL, 2)],
    "effects": [_ref(EH, 10), _ref(UNDATED_FX, None)],
    "icons": [_ref(IH, 10)],
}
PINS: dict[str, set[str]] = {
    "wallpapers": set(),
    "palettes": {PINNED_PAL},
    "effects": {PINNED_FX},
    "icons": set(),
}


class _CountingPins:
    """seed_pins fake recording invocation count (must be exactly one)."""

    def __init__(self, pins: Mapping[str, set[str]]) -> None:
        self._pins = pins
        self.calls = 0

    def __call__(self) -> Mapping[str, set[str]]:
        self.calls += 1
        return self._pins


def _monitors() -> dict[str, MonitorWallpaperConfig]:
    # Inserted unsorted on purpose: the builder must sort.
    return {
        "HDMI-1": MonitorWallpaperConfig(
            backend=BackendType.swww,
            source_hash=WH,
            fit_mode=FitMode.cover,
            mpv_options=None,
            ipc_socket=None,
        ),
        "DP-1": MonitorWallpaperConfig(
            backend=BackendType.swww,
            source_hash=WH,
            fit_mode=FitMode.cover,
            mpv_options=None,
            ipc_socket=None,
        ),
    }


def test_full_state_exact_projection() -> None:
    entries = _CountingEntries(REFS)
    pins = _CountingPins(PINS)
    actual = build_actual_state(_desktop_state(monitors=_monitors()), entries, pins)
    assert isinstance(actual, ActualState)
    assert actual.current_wallpaper == "/img/wall.png"
    assert actual.monitors == ("DP-1", "HDMI-1")
    assert actual.pinned_hashes == tuple(sorted((PINNED_PAL, PINNED_FX)))
    assert actual.undated_hashes == tuple(sorted((UNDATED_WALL, UNDATED_FX)))
    # Single-source rule: prunable must equal an independent PruneUseCase plan.
    assert entries.calls == {layer: 1 for layer in LAYERS}
    assert pins.calls == 1
    expected = (
        PruneUseCase(
            _LoadedStateRepository(_desktop_state(monitors=_monitors())), entries, lambda: PINS
        )
        .run()
        .removals
    )
    assert actual.prunable_hashes == expected


def test_absent_current_yields_empty_projection() -> None:
    entries = _CountingEntries(REFS)
    actual = build_actual_state(None, entries, lambda: PINS, keep=1)
    assert actual.current_wallpaper is None
    assert actual.monitors == ()
    # Classification still works from the injected callables alone.
    assert actual.pinned_hashes == tuple(sorted((PINNED_PAL, PINNED_FX)))
    assert actual.undated_hashes == tuple(sorted((UNDATED_WALL, UNDATED_FX)))
    # keep=1: newest dated (NEW_WALL) protected by recency; active absent so
    # WH is NOT protected here — only recency/pins/undated apply.
    assert actual.prunable_hashes["wallpapers"] == tuple(sorted((WH, OLD_WALL)))


def test_keep_zero_prunes_all_dated_unprotected() -> None:
    entries = _CountingEntries(REFS)
    actual = build_actual_state(_desktop_state(), entries, lambda: PINS, keep=0)
    assert NEW_WALL in actual.prunable_hashes["wallpapers"]
    assert OLD_WALL in actual.prunable_hashes["wallpapers"]
    # Active + pinned + undated stay protected regardless.
    assert WH not in actual.prunable_hashes["wallpapers"]
    assert PINNED_PAL not in actual.prunable_hashes["palettes"]
    assert UNDATED_WALL not in actual.prunable_hashes["wallpapers"]


def test_negative_keep_raises_before_any_io() -> None:
    entries = _CountingEntries(REFS)
    pins = _CountingPins(PINS)
    with pytest.raises(ValueError, match="keep"):
        build_actual_state(_desktop_state(), entries, pins, keep=-1)
    assert entries.calls == {layer: 0 for layer in LAYERS}
    assert pins.calls == 0


def test_model_is_frozen() -> None:
    import dataclasses

    actual = build_actual_state(_desktop_state(), _CountingEntries(REFS), lambda: PINS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        actual.monitors = ()  # type: ignore[misc]


def test_repo_stub_save_never_writes() -> None:
    with pytest.raises(NotImplementedError, match="never writes"):
        _LoadedStateRepository(None).save(_desktop_state())  # type: ignore[arg-type]
