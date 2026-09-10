"""Unit + hermetic e2e tests for Story 1.4 selective regeneration (RegenerateStaleUseCase).

Unit tests use pure fakes (no FS except tmp wallpaper bytes). The e2e uses
the REAL pipeline/seeder/repo/mutex/reconcile/check/adapter with fake tool
binaries (contract-honest: echo output_dir.name as entry hash, mirroring
test_reconcile.py) and fake reloaders — no live desktop. Covers Story 1.4
ACs: minimality + cascade, reconverge, idempotency, absent loudness.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.application.check_inputs import CheckInputsResult
from runtime.application.regenerate import RegenerateResult, RegenerateStaleUseCase
from runtime.cli.main import app

runner = CliRunner()

PH = "aa" * 32
EH = "bb" * 32
IH = "cc" * 32
WH = "dd" * 32
NEW_PH = "ee" * 32
NEW_IH = "ff" * 32
TS = "2026-09-10T00:00:00Z"


def _entries(
    palette_hash: str | None = PH,
    effects_hash: str | None = EH,
    icons_hash: str | None = IH,
) -> dict[str, Any]:
    from runtime.domain.models import EffectsEntry, IconsEntry, PaletteEntry

    palette = (
        PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=palette_hash,
            source_wallpaper_hash=WH,
            input_template_hash="t" * 64,
            artifact_hashes={
                "colors.yaml": "e" * 64,
                "colors.conf": "e" * 64,
                "colors.gtk.css": "e" * 64,
                "colors.adw.css": "e" * 64,
                "colors.sequences": "e" * 64,
                "colors.rasi": "e" * 64,
            },
            generated_at=TS,
        )
        if palette_hash
        else None
    )
    effects = (
        EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=effects_hash,
            source_wallpaper_hash=WH,
            input_catalog_hash="c" * 64,
            artifact_hashes={},
            generated_at=TS,
        )
        if effects_hash
        else None
    )
    icons = (
        IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=icons_hash,
            source_palette_hash=palette_hash or "",
            input_templates_hash="i" * 64,
            input_mappings_hash="m" * 64,
            artifact_hashes={},
            generated_at=TS,
        )
        if icons_hash
        else None
    )
    return {"palette": palette, "effects": effects, "icons": icons}


def _desktop_state(**kwargs: Any) -> Any:
    from runtime.domain.models import DesktopState, WallpaperEntry

    entries = _entries(**kwargs)
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=WH,
            source_path="/img/wall.png",
            imported_at=TS,
        ),
        monitors={},
        palette=entries["palette"],
        effects=entries["effects"],
        icons=entries["icons"],
        applied_at=TS,
    )


class _FakeCheck:
    """Scripted CheckInputsUseCase double."""

    def __init__(self, script: list[frozenset[str] | Exception]) -> None:
        self._script = list(script)
        self.calls = 0

    def run(self) -> CheckInputsResult:
        self.calls += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return CheckInputsResult(
            stale=frozenset(outcome),
            fresh=frozenset({"palettes", "effects", "icons"} - set(outcome)),
        )


class _FakePipeline:
    """Counting DerivationPipeline double returning scripted entries."""

    def __init__(
        self,
        palette_entry: Any = None,
        effects_entry: Any = None,
        icons_entry: Any = None,
    ) -> None:
        self.palette_calls: list[tuple[Path, str]] = []
        self.effects_calls: list[tuple[Path, str]] = []
        self.icons_calls: list[str] = []
        self._palette_entry = palette_entry
        self._effects_entry = effects_entry
        self._icons_entry = icons_entry

    def ensure_palette(self, wallpaper_path: Path, wallpaper_hash: str) -> tuple[Any, bool]:
        self.palette_calls.append((wallpaper_path, wallpaper_hash))
        assert self._palette_entry is not None
        return self._palette_entry, False

    def ensure_effects(self, wallpaper_path: Path, wallpaper_hash: str) -> tuple[Any, bool]:
        self.effects_calls.append((wallpaper_path, wallpaper_hash))
        assert self._effects_entry is not None
        return self._effects_entry, False

    def ensure_icons(self, palette_entry_hash: str) -> tuple[Any, bool]:
        self.icons_calls.append(palette_entry_hash)
        assert self._icons_entry is not None
        return self._icons_entry, False


class _FakeRepo:
    """In-memory state repository double.

    Returns ``state`` on every load unless ``second`` is given, in which
    case loads after the first return ``second`` (concurrent-modification
    simulation for the CAS path).
    """

    def __init__(self, state: Any, second: Any = None) -> None:
        self._state = state
        self._second = second
        self._use_second = False
        self.saves: list[Any] = []
        self.loads = 0

    def given_second(self, second: Any) -> _FakeRepo:
        self._second = second
        self._use_second = True
        return self

    def load_current(self) -> Any:
        self.loads += 1
        if self._use_second and self.loads > 1:
            return self._second
        return self._state

    def save(self, state: Any) -> None:
        self.saves.append(state)


class _FakeReconcile:
    def __init__(self, state: Any, reload_failures: tuple[str, ...] = ()) -> None:
        from runtime.application.reconcile import ReconcileResult

        self.triggers: list[str] = []
        self._result = ReconcileResult(
            repointed=[],
            skipped=[],
            state=state,
            cache_regenerated=[],
            reload_failures=list(reload_failures),
            consumer_symlinks=[],
        )

    def run(self, trigger: str = "reconcile") -> Any:
        self.triggers.append(trigger)
        return self._result


class _FakeMutex:
    def __init__(self) -> None:
        self.holds: list[bool] = []

    @contextmanager
    def hold(self, blocking: bool = False) -> Any:
        self.holds.append(blocking)
        yield None


def _wallpaper_file(tmp_path: Path, state_root: Path) -> Path:
    entry_dir = state_root / "cache" / "wallpapers" / WH
    entry_dir.mkdir(parents=True, exist_ok=True)
    target = entry_dir / "wallpaper.png"
    target.write_bytes(b"wallpaper bytes")
    return target


def _use_case(
    *,
    stale_script: list[frozenset[str] | Exception],
    state: Any,
    pipeline: _FakePipeline | None = None,
    reconcile_state: Any = None,
    tmp_path: Path,
    state_root: Path,
    with_wallpaper: bool = True,
) -> tuple[RegenerateStaleUseCase, _FakePipeline, _FakeRepo, _FakeReconcile, _FakeMutex]:
    if with_wallpaper:
        _wallpaper_file(tmp_path, state_root)
    check = _FakeCheck(stale_script)
    pipe = pipeline or _FakePipeline()
    repo = _FakeRepo(state)
    recon = _FakeReconcile(reconcile_state or state)
    mutex = _FakeMutex()
    use_case = RegenerateStaleUseCase(
        check=check,  # type: ignore[arg-type]
        pipeline=pipe,  # type: ignore[arg-type]
        state_repo=repo,  # type: ignore[arg-type]
        reconcile=recon,  # type: ignore[arg-type]
        mutex=mutex,  # type: ignore[arg-type]
        state_root=state_root,
    )
    return use_case, pipe, repo, recon, mutex


class TestNoOpAndAbsent:
    def test_empty_stale_is_noop(self, tmp_path: Path) -> None:
        state = _desktop_state()
        use_case, pipe, repo, recon, mutex = _use_case(
            stale_script=[frozenset()],
            state=state,
            tmp_path=tmp_path,
            state_root=tmp_path / "state",
        )
        result = use_case.run()
        assert result.regenerated == frozenset()
        assert result.state == state
        assert pipe.palette_calls == pipe.effects_calls == pipe.icons_calls == []
        assert repo.saves == []
        assert recon.triggers == []
        assert mutex.holds == []

    def test_absent_state_raises_before_touching_pipeline(self, tmp_path: Path) -> None:
        use_case, pipe, repo, recon, _ = _use_case(
            stale_script=[frozenset({"palettes"})],
            state=None,
            tmp_path=tmp_path,
            state_root=tmp_path / "state",
        )
        with pytest.raises(ValueError, match="no runtime state recorded yet"):
            use_case.run()
        assert pipe.palette_calls == pipe.effects_calls == pipe.icons_calls == []
        assert repo.saves == []
        assert recon.triggers == []


class TestMinimalRegen:
    def _new_entries(self) -> dict[str, Any]:
        return _entries(palette_hash=NEW_PH, effects_hash=EH, icons_hash=NEW_IH)

    def test_palette_stale_rebuilds_palette_plus_cascade(self, tmp_path: Path) -> None:
        state = _desktop_state()
        new = self._new_entries()
        pipe = _FakePipeline(
            palette_entry=new["palette"],
            effects_entry=new["effects"],
            icons_entry=new["icons"],
        )
        use_case, pipe, repo, recon, _ = _use_case(
            stale_script=[frozenset({"palettes"})],
            state=state,
            pipeline=pipe,
            reconcile_state=state,
            tmp_path=tmp_path,
            state_root=tmp_path / "state",
        )
        result = use_case.run()
        assert len(pipe.palette_calls) == 1
        assert pipe.effects_calls == []
        assert result.regenerated == frozenset({"palettes", "icons"})
        assert repo.saves and len(repo.saves) == 1
        assert recon.triggers == ["regenerate"]

    def test_icons_receives_new_palette_hash(self, tmp_path: Path) -> None:
        """Cascade-target pin: never the stale recorded hash."""
        state = _desktop_state()
        new = self._new_entries()
        pipe = _FakePipeline(
            palette_entry=new["palette"],
            effects_entry=new["effects"],
            icons_entry=new["icons"],
        )
        use_case, pipe, repo, recon, _ = _use_case(
            stale_script=[frozenset({"palettes"})],
            state=state,
            pipeline=pipe,
            reconcile_state=state,
            tmp_path=tmp_path,
            state_root=tmp_path / "state",
        )
        use_case.run()
        assert pipe.icons_calls == [NEW_PH]
        saved = repo.saves[0]
        assert saved.palette.entry_hash == NEW_PH
        assert saved.effects.entry_hash == EH
        assert saved.icons.entry_hash == NEW_IH

    def test_effects_stale_only(self, tmp_path: Path) -> None:
        state = _desktop_state()
        new = self._new_entries()
        pipe = _FakePipeline(
            palette_entry=new["palette"],
            effects_entry=new["effects"],
            icons_entry=new["icons"],
        )
        use_case, pipe, repo, recon, _ = _use_case(
            stale_script=[frozenset({"effects"})],
            state=state,
            pipeline=pipe,
            reconcile_state=state,
            tmp_path=tmp_path,
            state_root=tmp_path / "state",
        )
        result = use_case.run()
        assert pipe.palette_calls == []
        assert len(pipe.effects_calls) == 1
        assert pipe.icons_calls == []
        assert result.regenerated == frozenset({"effects"})
        assert repo.saves[0].palette.entry_hash == PH


class TestFailureModes:
    def test_missing_wallpaper_bytes_fails_loud(self, tmp_path: Path) -> None:
        import dataclasses

        valid_source = tmp_path / "real-wall.png"
        valid_source.write_bytes(b"real source bytes")
        state = _desktop_state()
        state = dataclasses.replace(
            state,
            wallpaper=dataclasses.replace(state.wallpaper, source_path=str(valid_source)),
        )
        use_case, pipe, repo, recon, _ = _use_case(
            stale_script=[frozenset({"palettes"})],
            state=state,
            tmp_path=tmp_path,
            state_root=tmp_path / "other-state",
            with_wallpaper=False,
        )
        with pytest.raises(ValueError, match="doctor --repair"):
            use_case.run()
        assert pipe.palette_calls == []
        assert repo.saves == []
        assert recon.triggers == []

    def test_stale_layer_without_recorded_entry_heals(self, tmp_path: Path) -> None:
        """Degraded (entry-less) layers heal by derivation — no hard error."""
        state = _desktop_state(palette_hash=None)
        new = _entries(palette_hash=NEW_PH, effects_hash=EH, icons_hash=NEW_IH)
        pipe = _FakePipeline(
            palette_entry=new["palette"],
            effects_entry=new["effects"],
            icons_entry=new["icons"],
        )
        use_case, pipe, repo, recon, _ = _use_case(
            stale_script=[frozenset({"palettes"})],
            state=state,
            pipeline=pipe,
            reconcile_state=state,
            tmp_path=tmp_path,
            state_root=tmp_path / "state",
        )
        result = use_case.run()
        assert len(pipe.palette_calls) == 1
        assert result.regenerated == frozenset({"palettes", "icons"})
        assert repo.saves[0].palette.entry_hash == NEW_PH

    def test_concurrent_modification_aborts_save(self, tmp_path: Path) -> None:
        state = _desktop_state()
        repo = _FakeRepo(state).given_second(_moved_wallpaper_state())
        _wallpaper_file(tmp_path, tmp_path / "state")
        new = _entries(palette_hash=NEW_PH, effects_hash=EH, icons_hash=NEW_IH)
        pipe = _FakePipeline(
            palette_entry=new["palette"],
            effects_entry=new["effects"],
            icons_entry=new["icons"],
        )
        recon = _FakeReconcile(state)
        use_case = RegenerateStaleUseCase(
            check=_FakeCheck([frozenset({"palettes"})]),  # type: ignore[arg-type]
            pipeline=pipe,  # type: ignore[arg-type]
            state_repo=repo,  # type: ignore[arg-type]
            reconcile=recon,  # type: ignore[arg-type]
            mutex=_FakeMutex(),  # type: ignore[arg-type]
            state_root=tmp_path / "state",
        )
        with pytest.raises(RuntimeError, match="concurrent modification"):
            use_case.run()
        assert repo.saves == []
        assert recon.triggers == []


def _moved_wallpaper_state() -> Any:
    from runtime.domain.models import WallpaperEntry

    state = _desktop_state()
    moved_wallpaper = WallpaperEntry(
        hash_algorithm="sha256",
        kind="wallpaper",
        content_hash="00" * 32,
        source_path="/img/other.png",
        imported_at=TS,
    )
    import dataclasses

    return dataclasses.replace(state, wallpaper=moved_wallpaper)


class TestRegenerateEndToEnd:
    """Hermetic e2e: real pipeline/seeder/repo/mutex/reconcile/check/adapter.

    Only tool binaries (counting fakes, contract-honest) and desktop
    reloaders (noop fakes) are doubled — no live desktop, no network.
    """

    def _setup(self, tmp_path: Path) -> dict[str, Any]:
        import hashlib
        from datetime import UTC, datetime

        from runtime.adapters.cache import cache_entry_path
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex
        from runtime.adapters.invalidation import InvalidationQueryAdapter
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.adapters.seeder import CacheSeeder
        from runtime.application.apply_wallpaper import ApplyWallpaperUseCase
        from runtime.application.check_inputs import CheckInputsUseCase
        from runtime.application.derive import (
            DerivationPipeline,
            find_effects_catalog,
            find_icon_mappings,
            find_icon_templates,
            find_templates_dir,
        )
        from runtime.application.reconcile import ReconcileDesktopStateUseCase
        from runtime.domain.models import EffectsEntry, IconsEntry, PaletteEntry
        from runtime.ports.desktop_reloader import IDesktopReloader

        install = tmp_path / "install"
        templates = install / "config" / "color-scheme-generator" / "templates"
        templates.mkdir(parents=True)
        (templates / "a.j2").write_text("template-a", encoding="utf-8")
        catalog = install / "config" / "weg" / "effects.yaml"
        catalog.parent.mkdir(parents=True)
        catalog.write_text("effects: []", encoding="utf-8")
        icon_templates = install / "icon-templates"
        icon_templates.mkdir(parents=True)
        (icon_templates / "icon.svg").write_text("<svg/>", encoding="utf-8")
        icon_mappings = install / "icon-mappings" / "icons.yaml"
        icon_mappings.parent.mkdir(parents=True)
        icon_mappings.write_text("mappings: {}", encoding="utf-8")
        state_root = tmp_path / "state"

        def _now_z() -> str:
            return datetime.now(UTC).isoformat().replace("+00:00", "Z")

        def _h(data: bytes) -> str:
            return hashlib.sha256(data).hexdigest()

        class _Csg:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, wallpaper_path: Path, output_dir: Path) -> PaletteEntry:
                self.calls += 1
                output_dir.mkdir(parents=True, exist_ok=True)
                artifacts = {
                    "colors.yaml": b"yaml",
                    "colors.conf": b"conf",
                    "colors.gtk.css": b"gtk",
                    "colors.adw.css": b"adw",
                    "colors.sequences": b"seq",
                    "colors.rasi": b"rasi",
                }
                hashes = {}
                for name, content in artifacts.items():
                    (output_dir / name).write_bytes(content)
                    hashes[name.replace(".", "_").replace("-", "_")] = _h(content)
                return PaletteEntry(
                    hash_algorithm="sha256",
                    kind="palette",
                    entry_hash=output_dir.name,
                    source_wallpaper_hash="w" * 64,
                    input_template_hash="t" * 64,
                    artifact_hashes=hashes,  # type: ignore[typeddict-item]
                    generated_at=_now_z(),
                )

        class _Weg:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, wallpaper_path: Path, output_dir: Path) -> EffectsEntry:
                self.calls += 1
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
                return EffectsEntry(
                    hash_algorithm="sha256",
                    kind="effects",
                    entry_hash=output_dir.name,
                    source_wallpaper_hash="w" * 64,
                    input_catalog_hash="c" * 64,
                    artifact_hashes={"effect.png": _h(b"\x89PNG\r\n\x1a\n")},
                    generated_at=_now_z(),
                )

        class _Itr:
            def __init__(self) -> None:
                self.calls = 0
                self.palette_hashes: list[str] = []

            def render(
                self,
                palette_hash: str,
                templates_dir: Path,
                mappings_path: Path,
                output_dir: Path,
            ) -> IconsEntry:
                self.calls += 1
                self.palette_hashes.append(palette_hash)
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "icon.svg").write_bytes(b"<svg/>")
                return IconsEntry(
                    hash_algorithm="sha256",
                    kind="icons",
                    entry_hash=output_dir.name,
                    source_palette_hash=palette_hash,
                    input_templates_hash="i" * 64,
                    input_mappings_hash="m" * 64,
                    artifact_hashes={"icon.svg": _h(b"<svg/>")},
                    generated_at=_now_z(),
                )

        class _Reloader(IDesktopReloader):
            def __init__(self) -> None:
                self.calls = 0

            def reload(self) -> bool:
                self.calls += 1
                return True

        return {
            "install": install,
            "state_root": state_root,
            "templates": templates,
            "csg": _Csg(),
            "weg": _Weg(),
            "itr": _Itr(),
            "reloaders": [_Reloader()],
            "helpers": {
                "CacheSeeder": CacheSeeder,
                "JsonStateRepository": JsonStateRepository,
                "FlockSeedMutex": FlockSeedMutex,
                "InvalidationQueryAdapter": InvalidationQueryAdapter,
                "CheckInputsUseCase": CheckInputsUseCase,
                "DerivationPipeline": DerivationPipeline,
                "ReconcileDesktopStateUseCase": ReconcileDesktopStateUseCase,
                "ApplyWallpaperUseCase": ApplyWallpaperUseCase,
                "find_templates_dir": find_templates_dir,
                "find_effects_catalog": find_effects_catalog,
                "find_icon_templates": find_icon_templates,
                "find_icon_mappings": find_icon_mappings,
                "cache_entry_path": cache_entry_path,
            },
        }

    def test_regenerate_after_template_edit_end_to_end(self, tmp_path: Path) -> None:
        env = self._setup(tmp_path)
        helpers = env["helpers"]
        state_root: Path = env["state_root"]
        install: Path = env["install"]
        img = tmp_path / "wall.png"
        img.write_bytes(b"wallpaper bytes")

        repo = helpers["JsonStateRepository"](state_root=state_root)
        seeder = helpers["CacheSeeder"](state_root)
        mutex = helpers["FlockSeedMutex"](state_root / ".seed.lock")
        helpers["ApplyWallpaperUseCase"](
            state_repo=repo,
            csg=env["csg"],
            weg=env["weg"],
            itr=env["itr"],
            install_spine=install,
            state_root=state_root,
            seeder=seeder,
            mutex=mutex,
        ).run(img)
        old_palette = repo.load_current().palette.entry_hash
        env["csg"].calls = env["weg"].calls = env["itr"].calls = 0
        env["itr"].palette_hashes = []

        (env["templates"] / "b.j2").write_text("template-b", encoding="utf-8")

        invalidation = helpers["InvalidationQueryAdapter"](
            state_root=state_root,
            templates_dir=helpers["find_templates_dir"](install),
            catalog_path=helpers["find_effects_catalog"](install),
            icon_templates=helpers["find_icon_templates"](install),
            icon_mappings=helpers["find_icon_mappings"](install),
        )
        use_case = RegenerateStaleUseCase(
            check=helpers["CheckInputsUseCase"](
                state_repo=repo,
                invalidation=invalidation,
                recorded_inputs=invalidation.recorded_inputs,
            ),
            pipeline=helpers["DerivationPipeline"](
                state_root=state_root,
                seeder=seeder,
                csg=env["csg"],
                weg=env["weg"],
                itr=env["itr"],
                install_spine=install,
            ),
            state_repo=repo,
            reconcile=helpers["ReconcileDesktopStateUseCase"](
                state_repo=repo,
                csg=env["csg"],
                weg=env["weg"],
                itr=env["itr"],
                install_spine=install,
                state_root=state_root,
                seeder=seeder,
                mutex=mutex,
                reloaders=env["reloaders"],
            ),
            mutex=mutex,
            state_root=state_root,
        )
        history_path = state_root / "history.jsonl"
        history_before = history_path.read_text(encoding="utf-8") if history_path.exists() else ""

        result = use_case.run()

        assert result.regenerated == frozenset({"palettes", "icons"})
        assert env["csg"].calls == 1
        assert env["weg"].calls == 0
        assert env["itr"].calls == 1
        current = repo.load_current()
        assert current is not None and current.palette.entry_hash != old_palette
        assert env["itr"].palette_hashes == [current.palette.entry_hash]
        assert (state_root / "cache" / "palettes" / old_palette).is_dir()
        history_after = (state_root / "history.jsonl").read_text(encoding="utf-8")
        new_lines = history_after[len(history_before) :].strip().splitlines()
        assert len(new_lines) == 1
        assert json.loads(new_lines[0])["trigger"] == "regenerate"
        assert env["reloaders"][0].calls >= 1
        assert len(result.repointed) > 0
        current_dir = state_root / "current"
        targets = [p.readlink() for p in current_dir.iterdir() if p.is_symlink()]
        assert targets, "expected current/ symlinks after reconverge"
        assert any(current.palette.entry_hash in str(t) for t in targets)

        env["csg"].calls = env["weg"].calls = env["itr"].calls = 0
        again = use_case.run()
        assert again.regenerated == frozenset()
        assert env["csg"].calls == env["weg"].calls == env["itr"].calls == 0

    def test_reload_failure_propagates_without_rollback(self, tmp_path: Path) -> None:
        """R5 passthrough: failures surface, the save stands (no rollback)."""
        state = _desktop_state()
        new = _entries(palette_hash=NEW_PH, effects_hash=EH, icons_hash=NEW_IH)
        pipe = _FakePipeline(
            palette_entry=new["palette"],
            effects_entry=new["effects"],
            icons_entry=new["icons"],
        )
        recon = _FakeReconcile(state, reload_failures=("hyprland",))
        _wallpaper_file(tmp_path, tmp_path / "state")
        use_case = RegenerateStaleUseCase(
            check=_FakeCheck([frozenset({"palettes"})]),  # type: ignore[arg-type]
            pipeline=pipe,  # type: ignore[arg-type]
            state_repo=_FakeRepo(state),  # type: ignore[arg-type]
            reconcile=recon,  # type: ignore[arg-type]
            mutex=_FakeMutex(),  # type: ignore[arg-type]
            state_root=tmp_path / "state",
        )
        result = use_case.run()
        assert result.reload_failures == ("hyprland",)
        assert result.regenerated == frozenset({"palettes", "icons"})
        assert len(use_case._state_repo.saves) == 1


class TestCliShape:
    def _result(
        self,
        regenerated: set[str] | frozenset[str] = frozenset(),
        failures: list[str] | None = None,
    ) -> RegenerateResult:
        return RegenerateResult(
            regenerated=frozenset(regenerated),
            state=_desktop_state(),
            repointed=(),
            reload_failures=tuple(failures or []),
        )

    def test_success_renders_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli_main, "_run_regenerate_stale", lambda: self._result({"palettes", "icons"})
        )
        result = runner.invoke(app, ["reconcile", "--regenerate-stale"])
        assert result.exit_code == 0
        assert "regenerated: icons, palettes" in result.output

    def test_noop_renders_converged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_main, "_run_regenerate_stale", lambda: self._result())
        result = runner.invoke(app, ["reconcile", "--regenerate-stale"])
        assert result.exit_code == 0
        assert "already converged" in result.output

    def test_mutually_exclusive_flags_exit_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called: list[None] = []
        monkeypatch.setattr(
            cli_main,
            "_run_regenerate_stale",
            lambda: (called.append(None), self._result())[1],
        )
        result = runner.invoke(app, ["reconcile", "--check-inputs", "--regenerate-stale"])
        assert result.exit_code == 2
        assert called == []

    def test_absent_state_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _raise() -> RegenerateResult:
            raise ValueError("no runtime state recorded yet")

        monkeypatch.setattr(cli_main, "_run_regenerate_stale", _raise)
        result = runner.invoke(app, ["reconcile", "--regenerate-stale"])
        assert result.exit_code == 1

    def test_reload_failure_exits_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            cli_main,
            "_run_regenerate_stale",
            lambda: self._result({"palettes"}, ["hyprland"]),
        )
        result = runner.invoke(app, ["reconcile", "--regenerate-stale"])
        assert result.exit_code == 1
        assert "hyprland" in result.output
