"""Unit tests for Story 3.2 prune keep-policy core.

Pure use-case tests with fakes (no FS) + adapter tests on tmp_path. Covers
active/last-N/seed-pin/undated protection, boundary, overrides, ordering.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.prune_source import entries_for, seed_pins
from runtime.application.prune import LAYERS, PruneUseCase
from runtime.domain.models import (
    CacheEntryRef,
    DesktopState,
    WallpaperEntry,
)

WP = "a" * 64
PH = "b" * 64
EH = "c" * 64
IH = "d" * 64


def _state(
    wallpaper: str = WP,
    palette: str | None = PH,
    effects: str | None = EH,
    icons: str | None = IH,
) -> DesktopState:
    from runtime.domain.models import EffectsEntry, IconsEntry, PaletteEntry

    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wallpaper,
            source_path="/img/w.png",
            imported_at="2026-09-10T00:00:00Z",
        ),
        monitors={},
        palette=(
            PaletteEntry(
                hash_algorithm="sha256",
                kind="palette",
                entry_hash=palette,
                source_wallpaper_hash=wallpaper,
                input_template_hash="t" * 64,
                artifact_hashes={},
                generated_at="2026-09-10T00:00:00Z",
            )
            if palette
            else None
        ),
        effects=(
            EffectsEntry(
                hash_algorithm="sha256",
                kind="effects",
                entry_hash=effects,
                source_wallpaper_hash=wallpaper,
                input_catalog_hash="c" * 64,
                artifact_hashes={},
                generated_at="2026-09-10T00:00:00Z",
            )
            if effects
            else None
        ),
        icons=(
            IconsEntry(
                hash_algorithm="sha256",
                kind="icons",
                entry_hash=icons,
                source_palette_hash=palette or "",
                input_templates_hash="i" * 64,
                input_mappings_hash="m" * 64,
                artifact_hashes={},
                generated_at="2026-09-10T00:00:00Z",
            )
            if icons
            else None
        ),
        applied_at="2026-09-10T00:00:00Z",
    )


class _Repo:
    def __init__(self, state: DesktopState | None) -> None:
        self._state = state

    def load_current(self) -> DesktopState | None:
        return self._state

    def save(self, state: DesktopState) -> None:
        raise AssertionError("prune must never save")


def _ts(n: int) -> str:
    return f"2026-09-{n:02d}T00:00:00Z"


def _use_case(
    *,
    state: DesktopState | None = None,
    entries: dict[str, list[CacheEntryRef]] | None = None,
    pins: dict[str, set[str]] | None = None,
    keep: int = 5,
) -> PruneUseCase:
    entries = entries or {}
    pins = pins or {}
    return PruneUseCase(
        state_repo=_Repo(state),  # type: ignore[arg-type]
        entries_for=lambda layer: entries.get(layer, []),
        seed_pins=lambda: pins,
        keep=keep,
    )


class TestPolicy:
    def test_active_excluded(self) -> None:
        uc = _use_case(
            state=_state(),
            entries={
                "palettes": [
                    CacheEntryRef("b" * 64, _ts(1)),  # active
                    CacheEntryRef("e" * 64, _ts(2)),  # recent only
                ]
            },
        )
        plan = uc.run()
        assert plan.removals["palettes"] == ()  # both kept (active + top-5)

    def test_old_unreferenced_selected(self) -> None:
        entries = {"effects": [CacheEntryRef(f"{i:064x}", _ts(i)) for i in range(1, 8)]}
        # newest = day 7..3 are top-5; days 1,2 removable
        uc = _use_case(state=None, entries=entries)
        plan = uc.run()
        assert plan.removals["effects"] == tuple(sorted(f"{i:064x}" for i in (1, 2)))
        assert plan.kept["effects"] == 5

    def test_seed_pinned_excluded(self) -> None:
        old = "e" * 64
        entries = {
            "palettes": [
                CacheEntryRef(old, _ts(1)),
                *[CacheEntryRef(f"{i:064x}", _ts(i)) for i in range(2, 8)],
            ]
        }
        uc = _use_case(state=None, entries=entries, pins={"palettes": {old}})
        plan = uc.run()
        assert old not in plan.removals["palettes"]

    def test_undated_protected(self) -> None:
        undated = "f" * 64
        entries = {"icons": [CacheEntryRef(undated, None), CacheEntryRef("e" * 64, _ts(1))]}
        plan = _use_case(state=None, entries=entries).run()
        assert plan.removals["icons"] == ()

    def test_boundary_sixth_oldest_removed(self) -> None:
        entries = {"effects": [CacheEntryRef(f"{i:064x}", _ts(i)) for i in range(1, 7)]}
        plan = _use_case(state=None, entries=entries, keep=5).run()
        # 6 entries, top-5 kept, day-1 removed
        assert plan.removals["effects"] == (f"{1:064x}",)
        assert plan.kept["effects"] == 5

    def test_undated_does_not_consume_keep(self) -> None:
        undated = "f" * 64
        entries = {
            "effects": [
                CacheEntryRef(undated, None),
                *[CacheEntryRef(f"{i:064x}", _ts(i)) for i in range(1, 7)],
            ]
        }
        plan = _use_case(state=None, entries=entries, keep=5).run()
        # undated protected + day 2..6 (5 dated) kept; day 1 removed
        assert plan.removals["effects"] == (f"{1:064x}",)
        assert undated not in plan.removals["effects"]

    def test_equal_timestamps_deterministic(self) -> None:
        entries = {
            "effects": [
                CacheEntryRef("a" * 64, _ts(1)),
                CacheEntryRef("b" * 64, _ts(1)),
                CacheEntryRef("c" * 64, _ts(1)),
            ]
        }
        plan = _use_case(state=None, entries=entries, keep=1).run()
        # Hash tiebreak is deterministic (descending); 2 removed, stable.
        assert plan.removals["effects"] == ("a" * 64, "b" * 64)

    def test_monitor_wallpaper_hashes_protected(self) -> None:
        from dataclasses import replace

        from runtime.domain.models import BackendType, FitMode, MonitorWallpaperConfig

        state = replace(
            _state(wallpaper="a" * 64),
            monitors={
                "DP-1": MonitorWallpaperConfig(
                    backend=BackendType.hyprpaper,
                    source_hash="9" * 64,
                    fit_mode=FitMode.cover,
                    mpv_options=None,
                    ipc_socket=None,
                )
            },
        )
        entries = {"wallpapers": [CacheEntryRef("9" * 64, _ts(1))]}
        plan = _use_case(state=state, entries=entries, keep=0).run()
        assert plan.removals["wallpapers"] == ()

    def test_keep_override(self) -> None:
        entries = {"effects": [CacheEntryRef(f"{i:064x}", _ts(i)) for i in range(1, 7)]}
        plan = _use_case(state=None, entries=entries, keep=2).run()
        assert plan.kept["effects"] == 2
        assert set(plan.removals["effects"]) == {f"{i:064x}" for i in (1, 2, 3, 4)}

    def test_deterministic_order(self) -> None:
        entries = {"effects": [CacheEntryRef(f"{i:064x}", _ts(i)) for i in range(1, 4)]}
        plan = _use_case(state=None, entries=entries, keep=0).run()
        assert plan.removals["effects"] == tuple(sorted(f"{i:064x}" for i in (1, 2, 3)))

    def test_cross_layer_independence(self) -> None:
        same = "e" * 64
        entries = {
            "effects": [CacheEntryRef(same, _ts(1))],
            "icons": [CacheEntryRef(same, _ts(1))],
        }
        plan = _use_case(state=None, entries=entries, keep=0).run()
        assert plan.removals["effects"] == (same,)
        assert plan.removals["icons"] == (same,)
        assert plan.total_removable == 2

    def test_absent_state_still_keeps_recent(self) -> None:
        entries = {"palettes": [CacheEntryRef(f"{i:064x}", _ts(i)) for i in range(1, 4)]}
        plan = _use_case(state=None, entries=entries, keep=1).run()
        assert plan.kept["palettes"] == 1
        assert len(plan.removals["palettes"]) == 2

    def test_negative_keep_rejected(self) -> None:
        with pytest.raises(ValueError, match="keep"):
            _use_case(keep=-1)

    def test_all_layers_present_in_plan(self) -> None:
        plan = _use_case(state=None).run()
        assert set(plan.removals) == set(LAYERS)
        assert plan.total_removable == 0


class TestAdapterEntries:
    def _entry(self, root: Path, layer: str, name: str, ts_key: str, ts: str | None) -> None:
        d = root / "cache" / layer / name
        d.mkdir(parents=True, exist_ok=True)
        meta: dict[str, object] = {"hash_algorithm": "sha256", "kind": layer.rstrip("s")}
        if ts is not None:
            meta[ts_key] = ts
        (d / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    def test_reads_timestamps_per_layer(self, tmp_path: Path) -> None:
        self._entry(tmp_path, "wallpapers", "a" * 64, "imported_at", _ts(3))
        self._entry(tmp_path, "palettes", "b" * 64, "generated_at", _ts(4))
        walls = entries_for(tmp_path, "wallpapers")
        pals = entries_for(tmp_path, "palettes")
        # Timestamps are canonicalized to fixed-width UTC for correct sorting.
        assert walls == [CacheEntryRef("a" * 64, "2026-09-03T00:00:00.000000Z")]
        assert pals == [CacheEntryRef("b" * 64, "2026-09-04T00:00:00.000000Z")]

    def test_fractional_second_normalized(self, tmp_path: Path) -> None:
        self._entry(tmp_path, "palettes", "a" * 64, "generated_at", "2026-09-03T12:34:56.9Z")
        assert entries_for(tmp_path, "palettes")[0].timestamp == ("2026-09-03T12:34:56.900000Z")

    def test_offset_normalized_to_utc(self, tmp_path: Path) -> None:
        self._entry(tmp_path, "palettes", "a" * 64, "generated_at", "2026-09-03T12:00:00+02:00")
        assert entries_for(tmp_path, "palettes")[0].timestamp == ("2026-09-03T10:00:00.000000Z")

    def test_symlinked_cache_root_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real-cache"
        (real / "palettes").mkdir(parents=True)
        (tmp_path / "cache").symlink_to(real, target_is_directory=True)
        assert entries_for(tmp_path, "palettes") == []

    def test_symlinked_layer_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "elsewhere"
        real.mkdir()
        cache = tmp_path / "cache"
        cache.mkdir()
        (cache / "palettes").symlink_to(real, target_is_directory=True)
        assert entries_for(tmp_path, "palettes") == []

    def test_symlinked_entry_dir_skipped(self, tmp_path: Path) -> None:
        layer = tmp_path / "cache" / "icons"
        layer.mkdir(parents=True)
        real = tmp_path / "real-entry"
        real.mkdir()
        (layer / ("a" * 64)).symlink_to(real, target_is_directory=True)
        assert entries_for(tmp_path, "icons") == []

    def test_symlinked_meta_is_undated(self, tmp_path: Path) -> None:
        layer = tmp_path / "cache" / "effects"
        entry = layer / ("c" * 64)
        entry.mkdir(parents=True)
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"generated_at": _ts(9)}), encoding="utf-8")
        (entry / "meta.json").symlink_to(outside)
        assert entries_for(tmp_path, "effects") == [CacheEntryRef("c" * 64, None)]

    def test_corrupt_meta_is_undated(self, tmp_path: Path) -> None:
        d = tmp_path / "cache" / "effects" / ("c" * 64)
        d.mkdir(parents=True)
        (d / "meta.json").write_text("{bad", encoding="utf-8")
        assert entries_for(tmp_path, "effects") == [CacheEntryRef("c" * 64, None)]

    def test_skips_non_hex_and_files(self, tmp_path: Path) -> None:
        layer = tmp_path / "cache" / "icons"
        layer.mkdir(parents=True)
        (layer / "not-a-hash").mkdir()
        (layer / ("d" * 64)).write_text("file squat")
        (layer / ("e" * 64)).mkdir()
        (layer / ("e" * 64) / "meta.json").write_text(
            json.dumps({"generated_at": _ts(1)}), encoding="utf-8"
        )
        assert [r.entry_hash for r in entries_for(tmp_path, "icons")] == ["e" * 64]

    def test_unknown_layer_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown cache layer"):
            entries_for(tmp_path, "bogus")

    def test_absent_layer_empty(self, tmp_path: Path) -> None:
        assert entries_for(tmp_path, "palettes") == []


class TestSeedPins:
    def _line(self, trigger: str, **fields: str | None) -> str:
        payload = {
            "ts": "2026-09-10T00:00:00Z",
            "trigger": trigger,
            "wallpaper": fields.get("wallpaper"),
            "palette": fields.get("palette"),
            "effects": fields.get("effects"),
            "icons": fields.get("icons"),
            "source_path": "",
        }
        return json.dumps(payload)

    def test_oldest_seed_line_wins(self, tmp_path: Path) -> None:
        first = self._line("seed", wallpaper="1" * 64, palette="2" * 64)
        second = self._line("seed", wallpaper="8" * 64)
        (tmp_path / "history.jsonl").write_text(first + "\n" + second + "\n", encoding="utf-8")
        pins = seed_pins(tmp_path)
        assert pins["wallpapers"] == {"1" * 64}
        assert pins["palettes"] == {"2" * 64}  # not the later seed's absence

    def test_non_seed_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "history.jsonl").write_text(
            self._line("set", wallpaper="3" * 64) + "\n", encoding="utf-8"
        )
        assert all(not v for v in seed_pins(tmp_path).values())

    def test_absent_history_empty(self, tmp_path: Path) -> None:
        assert all(not v for v in seed_pins(tmp_path).values())

    def test_torn_line_skipped(self, tmp_path: Path) -> None:
        content = self._line("seed", wallpaper="4" * 64) + "\n" + "{torn"
        (tmp_path / "history.jsonl").write_text(content, encoding="utf-8")
        assert seed_pins(tmp_path)["wallpapers"] == {"4" * 64}

    def test_corrupt_first_seed_line_refuses(self, tmp_path: Path) -> None:
        """Mid-file corruption: the oldest seed is unknowable → fail CLOSED."""
        content = "{corrupt}\n" + self._line("seed", wallpaper="8" * 64) + "\n"
        (tmp_path / "history.jsonl").write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match="not valid JSON"):
            seed_pins(tmp_path)

    def test_prune_line_with_details_before_seed_is_ignored(self, tmp_path: Path) -> None:
        """R-1: a prune audit line (with details) parses and is skipped."""
        prune = json.dumps(
            {
                "ts": "2026-01-01T00:00:00Z",
                "trigger": "prune",
                "wallpaper": "0" * 64,
                "palette": None,
                "effects": None,
                "icons": None,
                "source_path": "",
                "details": {"removed": 3, "layers": {"palettes": 3}},
            }
        )
        content = prune + "\n" + self._line("seed", wallpaper="1" * 64, palette="2" * 64) + "\n"
        (tmp_path / "history.jsonl").write_text(content, encoding="utf-8")
        pins = seed_pins(tmp_path)
        assert pins["wallpapers"] == {"1" * 64}
        assert pins["palettes"] == {"2" * 64}
