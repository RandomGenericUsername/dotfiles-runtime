"""Unit tests for ReconcileDesktopStateUseCase (Story 2.1).

Covers: happy-path swap (all consumer symlinks created with correct
cache targets); NO-COPY assertion (every current/ entry is a symlink);
delegation (the whole repoint routes through CacheSeeder.repoint_current_
symlinks — reconcile asserts delegation + arguments, not the seeder's
internal ordering); idempotent re-run (zero tool invocations, one new
history line per run); absent current.json → RuntimeError; corrupt store
ValueError propagates; null effects/icons → skipped with warning, other
symlinks still repointed; empty monitors → default DP-1; missing palette
artifact → skipped, never dangling; cache-miss regeneration (state
rebuilt from real meta.json — no sentinels); hash-mismatch guard;
wallpaper re-import from source_path (and loud failure without one);
history schema (trigger "reconcile"); structural scope lock (no reload
channel in the constructor); mutex double-checked ordering.
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.flock_seed_mutex import FlockSeedMutex
from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.domain.models import DesktopState
from runtime.ports.state_repository import IStateRepository

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
WALLPAPER_PNG = FIXTURES / "wallpaper.png"


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeMutex:
    """Fake ISeedMutex: records acquire/release + blocking flag into events."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.holds = 0

    def hold(self, blocking: bool = False) -> Any:
        self.holds += 1
        self.events.append(f"acquire(blocking={blocking})")

        class _Hold:
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, *exc: object) -> None:
                self.events.append("release")

        return _Hold()


class _FakeCsg:
    """Contract-honest fake: echoes the output dir's name as entry hash."""

    def __init__(self, *, fail: bool = False, events: list[str] | None = None) -> None:
        self.fail = fail
        self.calls = 0
        self.events = events

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        if self.events is not None:
            self.events.append("csg")
        if self.fail:
            raise RuntimeError("csg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml=hash_file(output_dir / "colors.yaml"),
                colors_conf=hash_file(output_dir / "colors.conf"),
                colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("weg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        effect_hash = hash_file(output_dir / "effect.png")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_catalog_hash="2" * 64,
            artifact_hashes=EffectsArtifacts(**{"effect.png": effect_hash}),
            generated_at=_now_z(),
        )


class _FakeItr:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> Any:
        from runtime.domain.models import IconsArtifacts, IconsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("itr exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=output_dir.name,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes=IconsArtifacts(**{"icon.svg": hash_file(output_dir / "icon.svg")}),
            generated_at=_now_z(),
        )


class _SpySeeder(CacheSeeder):
    """Real seeder that records repoint_current_symlinks arguments."""

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root)
        self.repoint_calls: list[dict[str, Any]] = []

    def repoint_current_symlinks(
        self,
        wallpaper_target: Path,
        monitor_names: list[str],
        palette_entry_hash: str | None = None,
        effects_entry_hash: str | None = None,
        icons_entry_hash: str | None = None,
    ) -> list[Path]:
        self.repoint_calls.append(
            {
                "wallpaper_target": wallpaper_target,
                "monitor_names": list(monitor_names),
                "palette_entry_hash": palette_entry_hash,
                "effects_entry_hash": effects_entry_hash,
                "icons_entry_hash": icons_entry_hash,
            }
        )
        return super().repoint_current_symlinks(
            wallpaper_target=wallpaper_target,
            monitor_names=monitor_names,
            palette_entry_hash=palette_entry_hash,
            effects_entry_hash=effects_entry_hash,
            icons_entry_hash=icons_entry_hash,
        )


class _EventRecorder:
    """Wraps a real JsonStateRepository, recording load/save events."""

    def __init__(self, inner: IStateRepository, events: list[str]) -> None:
        self._inner = inner
        self._events = events

    def load_current(self) -> DesktopState | None:
        self._events.append("load")
        return self._inner.load_current()

    def save(self, state: DesktopState) -> None:
        self._events.append("save")
        self._inner.save(state)


@dataclass
class _Applied:
    """Post-apply environment: cache entries + current.json exist on disk."""

    repo: JsonStateRepository
    state_root: Path
    install_spine: Path
    img: Path
    csg: _FakeCsg
    weg: _FakeWeg
    itr: _FakeItr


def _setup_spine(install_spine: Path) -> None:
    """Create spine config inputs (templates/catalog/icon assets)."""
    csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    csg_templates.mkdir(parents=True)
    (csg_templates / "default.yaml").write_text("window: {}\n")
    weg_config = install_spine / "config" / "weg"
    weg_config.mkdir(parents=True)
    (weg_config / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "icon-templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text(
        "icons: {}\n"
    )


def _apply_state(
    tmp_path: Path,
    *,
    img_bytes: bytes = b"user wallpaper bytes",
    weg_fail: bool = False,
    itr_fail: bool = False,
) -> _Applied:
    """Establish the reconcile Given: apply produced current.json (ahead of
    the symlinks) plus real cache entries, via the REAL JsonStateRepository
    so load_current() returns the sentinel projection — exactly production."""
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    csg, weg, itr = _FakeCsg(), _FakeWeg(fail=weg_fail), _FakeItr(fail=itr_fail)
    img = tmp_path / "wall.png"
    img.write_bytes(img_bytes)

    ApplyWallpaperUseCase(
        state_repo=repo,
        csg=csg,
        weg=weg,
        itr=itr,
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),
    ).run(img)

    csg.calls = weg.calls = itr.calls = 0  # count reconcile invocations only
    return _Applied(
        repo=repo,
        state_root=state_root,
        install_spine=install_spine,
        img=img,
        csg=csg,
        weg=weg,
        itr=itr,
    )


def _make_reconcile(
    applied: _Applied,
    *,
    seeder: CacheSeeder | None = None,
    mutex: Any | None = None,
    repo: Any | None = None,
) -> Any:
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    return ReconcileDesktopStateUseCase(
        state_repo=repo if repo is not None else applied.repo,
        csg=applied.csg,
        weg=applied.weg,
        itr=applied.itr,
        install_spine=applied.install_spine,
        state_root=applied.state_root,
        seeder=seeder if seeder is not None else CacheSeeder(applied.state_root),
        mutex=mutex if mutex is not None else _FakeMutex(),
    )


def _symlink_map(current_dir: Path) -> dict[str, str]:
    return {p.name: os_readlink(p) for p in sorted(current_dir.iterdir())}


def os_readlink(p: Path) -> str:
    import os

    return os.readlink(p)


class TestReconcileHappyPath:
    """AC 1/2: one run repoints every current/ entry into cache/ — no copies."""

    def test_repoints_all_consumer_symlinks_with_cache_targets(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(applied)
        loaded = applied.repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        assert loaded.palette is not None
        assert loaded.effects is not None
        assert loaded.icons is not None

        result = use_case.run()

        current = applied.state_root / "current"
        links = _symlink_map(current)
        assert set(links) == {
            "wallpaper-DP-1.png",
            "colors.conf",
            "colors.gtk.css",
            "colors.yaml",
            "effects",
            "icons",
        }
        wallpaper_target = applied.state_root / "cache" / "wallpapers" / wh / "wallpaper.png"
        palette_dir = applied.state_root / "cache" / "palettes" / loaded.palette.entry_hash
        assert links["wallpaper-DP-1.png"] == str(wallpaper_target)
        assert links["colors.conf"] == str(palette_dir / "colors.conf")
        assert links["colors.gtk.css"] == str(palette_dir / "colors.gtk.css")
        assert links["colors.yaml"] == str(palette_dir / "colors.yaml")
        assert links["effects"] == str(applied.state_root / "cache" / "effects" / loaded.effects.entry_hash)
        assert links["icons"] == str(applied.state_root / "cache" / "icons" / loaded.icons.entry_hash)
        assert sorted(p.name for p in result.repointed) == sorted(links)
        assert result.skipped == []
        assert result.cache_regenerated == []
        # every target resolves into cache/
        for target in links.values():
            assert target.startswith(str(applied.state_root / "cache"))

    def test_no_copy_every_current_entry_is_a_symlink(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(applied)
        use_case.run()

        current = applied.state_root / "current"
        entries = list(current.iterdir())
        assert entries, "current/ must not be empty"
        for entry in entries:
            assert entry.is_symlink(), f"copied file found in current/: {entry}"
            # symlink target resolves into cache/ — the only legitimate byte
            # placement is step 1's cache-miss regeneration via staging-dir
            target = Path(os_readlink(entry))
            assert str(target).startswith(
                str(applied.state_root / "cache")
            ), f"symlink target outside cache/: {target}"

    def test_repoint_delegated_to_seeder_with_correct_arguments(
        self, tmp_path: Path
    ) -> None:
        applied = _apply_state(tmp_path)
        seeder = _SpySeeder(applied.state_root)
        use_case = _make_reconcile(applied, seeder=seeder)
        loaded = applied.repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash

        use_case.run()

        assert len(seeder.repoint_calls) == 1
        call = seeder.repoint_calls[0]
        assert call["wallpaper_target"] == (
            applied.state_root / "cache" / "wallpapers" / wh / "wallpaper.png"
        )
        assert call["monitor_names"] == ["DP-1"]
        assert call["palette_entry_hash"] == (
            loaded.palette.entry_hash if loaded.palette else None
        )
        assert call["effects_entry_hash"] == (
            loaded.effects.entry_hash if loaded.effects else None
        )
        assert call["icons_entry_hash"] == (loaded.icons.entry_hash if loaded.icons else None)


class TestReconcileIdempotence:
    """AC 5: re-run is a no-op-with-verification, one history line per run."""

    def test_second_run_is_zero_invocation_and_appends_one_history_line(
        self, tmp_path: Path
    ) -> None:
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(applied)
        use_case.run()
        history = applied.state_root / "history.jsonl"
        first_run_lines = history.read_text().splitlines()
        assert len(first_run_lines) == 1
        links_before = _symlink_map(applied.state_root / "current")

        result = use_case.run()

        assert (applied.csg.calls, applied.weg.calls, applied.itr.calls) == (0, 0, 0)
        assert _symlink_map(applied.state_root / "current") == links_before
        second_run_lines = history.read_text().splitlines()
        assert len(second_run_lines) == 2  # append-only: one NEW line per run
        assert result.repointed  # symlinks re-repointed (idempotent atomic replace)


class TestReconcileFailurePolicy:
    """Absent/corrupt state, null layers, missing artifacts, dead wallpaper source."""

    def test_absent_current_json_raises_nothing_to_reconcile(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        empty_repo = JsonStateRepository(state_root=tmp_path / "no-state")
        use_case = _make_reconcile(applied, repo=empty_repo)

        with pytest.raises(RuntimeError, match="nothing to reconcile"):
            use_case.run()
        assert not (tmp_path / "no-state" / "current").exists()

    def test_corrupt_store_value_error_propagates(self, tmp_path: Path) -> None:
        from runtime.domain.models import DesktopState

        applied = _apply_state(tmp_path)
        state_root = applied.state_root
        (state_root / "current.json").write_text("{not json")

        use_case = _make_reconcile(applied)

        with pytest.raises(ValueError, match="not valid JSON"):
            use_case.run()
        assert not (state_root / "current").exists()
        assert not (state_root / "history.jsonl").exists()

    def test_null_effects_is_skipped_with_warning_others_repointed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        applied = _apply_state(tmp_path, weg_fail=True)
        loaded = applied.repo.load_current()
        assert loaded is not None
        assert loaded.effects is None  # apply degraded gracefully
        use_case = _make_reconcile(applied)

        with caplog.at_level(logging.WARNING, logger="runtime.application.reconcile"):
            result = use_case.run()

        current = applied.state_root / "current"
        links = _symlink_map(current)
        assert "effects" not in links  # skipped
        assert "wallpaper-DP-1.png" in links
        assert "colors.conf" in links
        assert "icons" in links
        assert any("effects" in s for s in result.skipped)
        assert any("effects" in r.message.lower() for r in caplog.records)

    def test_null_icons_is_skipped_with_warning(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path, itr_fail=True)
        use_case = _make_reconcile(applied)

        result = use_case.run()

        links = _symlink_map(applied.state_root / "current")
        assert "icons" not in links
        assert "effects" in links
        assert any("icons" in s for s in result.skipped)

    def test_missing_palette_artifact_skipped_never_dangling(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        assert loaded.palette is not None
        palette_dir = applied.state_root / "cache" / "palettes" / loaded.palette.entry_hash
        (palette_dir / "colors.gtk.css").unlink()
        use_case = _make_reconcile(applied)

        result = use_case.run()

        links = _symlink_map(applied.state_root / "current")
        assert "colors.gtk.css" not in links  # not created — never dangling
        assert "colors.conf" in links
        assert "colors.yaml" in links
        assert any("colors.gtk.css" in s for s in result.skipped)

    def test_empty_monitors_default_to_dp1(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        applied.repo.save(
            type(loaded)(
                schema_version=2,
                wallpaper=loaded.wallpaper,
                monitors={},
                palette=loaded.palette,
                effects=loaded.effects,
                icons=loaded.icons,
                applied_at=loaded.applied_at,
            )
        )
        use_case = _make_reconcile(applied)

        use_case.run()

        links = _symlink_map(applied.state_root / "current")
        assert "wallpaper-DP-1.png" in links

    def test_missing_wallpaper_entry_with_usable_source_is_reimported(
        self, tmp_path: Path
    ) -> None:
        applied = _apply_state(tmp_path)
        wh = hash_file(applied.img)
        entry_dir = applied.state_root / "cache" / "wallpapers" / wh
        shutil.rmtree(entry_dir)
        use_case = _make_reconcile(applied)

        result = use_case.run()

        assert "wallpaper" in result.cache_regenerated
        restored = entry_dir / "wallpaper.png"
        assert restored.is_file()
        assert hash_file(restored) == wh
        assert applied.csg.calls == 0  # derived layers were cache hits

    def test_missing_wallpaper_entry_with_empty_source_fails_loud(
        self, tmp_path: Path
    ) -> None:
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        from runtime.domain.models import WallpaperEntry

        applied.repo.save(
            type(loaded)(
                schema_version=2,
                wallpaper=WallpaperEntry(
                    hash_algorithm="sha256",
                    kind="wallpaper",
                    content_hash=loaded.wallpaper.content_hash,
                    source_path="",
                    imported_at=loaded.wallpaper.imported_at,
                ),
                monitors=loaded.monitors,
                palette=loaded.palette,
                effects=loaded.effects,
                icons=loaded.icons,
                applied_at=loaded.applied_at,
            )
        )
        wh = loaded.wallpaper.content_hash
        shutil.rmtree(applied.state_root / "cache" / "wallpapers" / wh)
        use_case = _make_reconcile(applied)

        with pytest.raises(RuntimeError, match="source_path"):
            use_case.run()
        assert not (applied.state_root / "current").exists()
        assert not (applied.state_root / "history.jsonl").exists()

    def test_missing_wallpaper_entry_with_dead_source_fails_loud(
        self, tmp_path: Path
    ) -> None:
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        wh = loaded.wallpaper.content_hash
        shutil.rmtree(applied.state_root / "cache" / "wallpapers" / wh)
        applied.img.unlink()  # source gone
        use_case = _make_reconcile(applied)

        with pytest.raises(RuntimeError, match="no longer exists"):
            use_case.run()
        assert not (applied.state_root / "current").exists()

    def test_palette_regeneration_failure_aborts(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        assert loaded.palette is not None
        shutil.rmtree(applied.state_root / "cache" / "palettes" / loaded.palette.entry_hash)
        applied.csg.fail = True
        use_case = _make_reconcile(applied)

        with pytest.raises(RuntimeError, match="palette reconcile failed:"):
            use_case.run()
        # current.json untouched, nothing repointed, no history line
        assert loaded.applied_at == applied.repo.load_current().applied_at  # type: ignore[union-attr]
        assert not (applied.state_root / "current").exists()
        assert not (applied.state_root / "history.jsonl").exists()


class TestReconcileCacheMissRegeneration:
    """AC 1 step 1 + AC 6b: genuine misses regenerate; hash mismatches fail loud."""

    def test_deleted_palette_entry_regenerates_with_matching_hash(
        self, tmp_path: Path
    ) -> None:
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        assert loaded.palette is not None
        recorded_peh = loaded.palette.entry_hash
        entry_dir = applied.state_root / "cache" / "palettes" / recorded_peh
        shutil.rmtree(entry_dir)
        use_case = _make_reconcile(applied)

        result = use_case.run()

        assert "palette" in result.cache_regenerated
        assert applied.csg.calls == 1
        assert entry_dir.is_dir()
        # state re-saved from real meta.json — recorded hash preserved, no sentinel
        saved_raw = json.loads((applied.state_root / "current.json").read_text())
        assert saved_raw["palette"]["hash"] == recorded_peh
        links = _symlink_map(applied.state_root / "current")
        assert links["colors.conf"] == str(entry_dir / "colors.conf")

    def test_hash_mismatch_fails_loud_and_never_repoints(self, tmp_path: Path) -> None:
        """Spine inputs changed since wallpaper set: regeneration produces a
        different entry hash — reconcile must not converge to unrecorded state."""
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        assert loaded.palette is not None
        recorded_peh = loaded.palette.entry_hash
        entry_dir = applied.state_root / "cache" / "palettes" / recorded_peh
        shutil.rmtree(entry_dir)
        # mutate spine inputs (AD-2 invalidation) so the recomputed hash differs
        templates = (
            applied.install_spine / "config" / "color-scheme-generator" / "templates"
        )
        (templates / "default.yaml").write_text("window: {border: 2}\n")
        use_case = _make_reconcile(applied)

        with pytest.raises(
            RuntimeError, match="cannot be regenerated from current spine inputs"
        ):
            use_case.run()

        assert applied.csg.calls == 1  # regeneration ran, produced a different hash
        assert not (applied.state_root / "current").exists()  # no repoint
        assert not (applied.state_root / "history.jsonl").exists()
        assert applied.repo.load_current().applied_at == loaded.applied_at  # type: ignore[union-attr]

    def test_deleted_effects_entry_regenerates(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        assert loaded.effects is not None
        eeh = loaded.effects.entry_hash
        shutil.rmtree(applied.state_root / "cache" / "effects" / eeh)
        use_case = _make_reconcile(applied)

        result = use_case.run()

        assert "effects" in result.cache_regenerated
        assert applied.weg.calls == 1
        assert (applied.state_root / "cache" / "effects" / eeh).is_dir()


class TestReconcileHistory:
    """AC 1 step 4: one reconcile line with the contract schema."""

    def test_history_line_schema(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        loaded = applied.repo.load_current()
        assert loaded is not None
        use_case = _make_reconcile(applied)

        use_case.run()

        lines = (applied.state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1
        line = json.loads(lines[0])
        assert line["trigger"] == "reconcile"
        assert line["wallpaper"] == loaded.wallpaper.content_hash
        assert line["palette"] == (loaded.palette.entry_hash if loaded.palette else None)
        assert line["effects"] == (loaded.effects.entry_hash if loaded.effects else None)
        assert line["icons"] == (loaded.icons.entry_hash if loaded.icons else None)
        assert line["source_path"] == str(applied.img)
        assert line["ts"].endswith("Z")


class TestReconcileHistoryTriggerParam:
    """Story 2.7: run(trigger=...) threads the history trigger; the
    default stays "reconcile"; the pinned enum is enforced fail-loud
    INSIDE run() only (CacheSeeder.append_history stays trigger-agnostic)."""

    @pytest.mark.parametrize("trigger", ["seed", "set", "reconcile", "force"])
    def test_run_valid_trigger_appends_history_line(self, tmp_path: Path, trigger: str) -> None:
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(applied)

        use_case.run(trigger=trigger)

        lines = (applied.state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["trigger"] == trigger

    def test_run_default_trigger_is_reconcile(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(applied)

        use_case.run()

        lines = (applied.state_root / "history.jsonl").read_text().splitlines()
        assert json.loads(lines[0])["trigger"] == "reconcile"

    @pytest.mark.parametrize("trigger", ["apply", "Apply", "SET", "", "set ", "restart"])
    def test_run_invalid_trigger_raises_value_error(
        self, tmp_path: Path, trigger: str
    ) -> None:
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(applied)

        with pytest.raises(ValueError, match="invalid history trigger"):
            use_case.run(trigger=trigger)
        assert not (applied.state_root / "history.jsonl").exists()

    def test_run_unhashable_trigger_raises_value_error(self, tmp_path: Path) -> None:
        """A non-str trigger must fail loud as ValueError (not TypeError) —
        the fail-loud contract is the documented surface even for a
        non-conforming caller."""
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(applied)

        with pytest.raises(ValueError, match="invalid history trigger"):
            use_case.run(trigger=["set"])  # type: ignore[arg-type]
        assert not (applied.state_root / "history.jsonl").exists()


class TestReconcileStructuralScopeLock:
    """AC: the reload channel is constructor-injected only — the accepted
    parameter set is pinned exactly (tripwire against unexpected reloader/
    backend/factory parameters), and nothing outside state_root is written."""

    def test_constructor_reload_channel_is_injected_only(self) -> None:
        from runtime.application.reconcile import ReconcileDesktopStateUseCase

        params = set(inspect.signature(ReconcileDesktopStateUseCase.__init__).parameters)
        assert params == {
            "self",
            "state_repo",
            "csg",
            "weg",
            "itr",
            "install_spine",
            "state_root",
            "seeder",
            "mutex",
            "reloaders",
        }
        assert not any("backend" in p or "factory" in p for p in params)

    def test_writes_confined_to_state_root(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(applied)

        use_case.run()

        # current/ + current.json + history.jsonl + cache/ all under state_root;
        # nothing appeared next to it (outside state_root)
        siblings = sorted(p.name for p in tmp_path.iterdir())
        assert siblings == ["install", "state", "wall.png"]


class TestReconcileMutex:
    """D1 protocol: reconcile serializes on the seed mutex, double-checked."""

    def test_reload_and_save_inside_the_critical_section(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        events: list[str] = []
        mutex = _FakeMutex(events=events)
        recorder = _EventRecorder(applied.repo, events)
        use_case = _make_reconcile(applied, mutex=mutex, repo=recorder)

        use_case.run()

        # Derivation (ensure_entries) runs outside the lock.  The mutex
        # critical section encloses: load → save.  The fast-path load
        # (fail-fast absent/corrupt guard) precedes derivation.
        assert events == [
            "load",
            "acquire(blocking=True)",
            "load",
            "save",
            "release",
        ]
        assert mutex.holds == 1

    def test_real_flock_mutex_is_usable(self, tmp_path: Path) -> None:
        applied = _apply_state(tmp_path)
        use_case = _make_reconcile(
            applied, mutex=FlockSeedMutex(applied.state_root / ".seed.lock")
        )

        result = use_case.run()

        assert result.state.applied_at
        # lock file is the SAME one apply/seeder serialize on
        assert (applied.state_root / ".seed.lock").exists()
