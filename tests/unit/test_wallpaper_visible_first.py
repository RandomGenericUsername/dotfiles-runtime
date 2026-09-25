"""Unit tests for the visible-first ``wallpaper set`` (openspec D1–D5).

Covers the layers the composition tests cannot reach in isolation:

- ``SwapVisibleUseCase``: wallpaper-only persist (null layers,
  schema-valid), wallpaper-only repoint, hyprpaper reload, no history,
  monitor preservation, input/state/hyprpaper failure modes.
- ``ApplyWallpaperUseCase`` derivation-only duty: a pre-imported
  ``wallpaper_hash`` skips the seeder re-import (spied), while the
  standalone path still imports.
- ``PassThroughSeedMutex``: the inner no-op the outer hold requires.
- Converge-composition regression (task 1.2): ``suppress_history``
  threads to all three seeders, and ``_run_converge`` still composes
  ``_run_wallpaper_set`` with the same parameters.
- Consumer audit (task 2.2): the wallpaper selector treats ``visible``
  as busy (no unlock, no crash) — scanned, never edited (other agents
  own the GUI); Phase-5 consumers ignore the topic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.application.swap_visible import SwapVisibleUseCase
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    WallpaperEntry,
)


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeMutex:
    """Fake ISeedMutex: records blocking flags, never contends."""

    def __init__(self) -> None:
        self.holds: list[bool] = []

    def hold(self, blocking: bool = False) -> Any:
        self.holds.append(blocking)

        class _Hold:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *exc: object) -> None:
                pass

        return _Hold()


class _FakeHyprpaper:
    """Fake wallpaper applier: records calls, canned result."""

    def __init__(self, *, ok: bool = True, fail: Exception | None = None) -> None:
        self.ok = ok
        self.fail = fail
        self.calls = 0

    def apply(self) -> bool:
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        return self.ok

    def reload(self) -> bool:
        return self.apply()


class _FakeMonitorSource:
    def __init__(self, monitors: list[str]) -> None:
        self._monitors = monitors

    def detect_monitors(self) -> list[str]:
        return list(self._monitors)


def _make_swap(
    state_root: Path,
    *,
    mutex: Any | None = None,
    hyprpaper: Any | None = None,
    monitor_source: Any | None = None,
    suppress_history: bool = False,
) -> tuple[SwapVisibleUseCase, _FakeMutex, _FakeHyprpaper]:
    from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec

    mutex = mutex if mutex is not None else _FakeMutex()
    hyprpaper = hyprpaper if hyprpaper is not None else _FakeHyprpaper()
    use_case = SwapVisibleUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        seeder=CacheSeeder(
            state_root,
            consumer_spec=StaticConsumerPathSpec(),
            suppress_history=suppress_history,
        ),
        mutex=mutex,
        hyprpaper=hyprpaper,
        monitor_source=monitor_source,
    )
    assert isinstance(mutex, _FakeMutex)
    assert isinstance(hyprpaper, _FakeHyprpaper)
    return use_case, mutex, hyprpaper


class TestSwapVisibleUseCase:
    def test_happy_path_persists_wallpaper_only_and_repoints(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        img = tmp_path / "wall.png"
        img.write_bytes(b"visible swap bytes")
        use_case, mutex, hyprpaper = _make_swap(state_root)

        result = use_case.run(img)

        wh = hash_file(img)
        assert result.wallpaper_hash == wh
        assert result.wallpaper_path == state_root / "cache" / "wallpapers" / wh / "wallpaper.png"
        # Wallpaper-only state: layers null (schema-valid, crash-safe).
        assert result.state.palette is None
        assert result.state.effects is None
        assert result.state.icons is None
        assert result.state.wallpaper.content_hash == wh
        assert result.state.wallpaper.source_path == str(img.expanduser().resolve())
        saved = JsonStateRepository(state_root=state_root).load_current()
        assert saved is not None and saved.wallpaper.content_hash == wh
        assert saved.palette is None and saved.effects is None and saved.icons is None
        # ONLY wallpaper symlinks repointed (plus the alias).
        names = {p.name for p in result.repointed}
        assert names == {"wallpaper-DP-1.png", "wallpaper.png"}
        for link in result.repointed:
            assert link.resolve() == result.wallpaper_path
        # Hyprpaper reloaded exactly once; mutex held blocking for save.
        assert hyprpaper.calls == 1
        assert mutex.holds == [True]
        # No history line — phase 2 owns the single trigger="set" line.
        assert not (state_root / "history.jsonl").exists()

    def test_monitors_preserved_only_source_hash_moves(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        repo = JsonStateRepository(state_root=state_root)
        old_img = tmp_path / "old.png"
        old_img.write_bytes(b"old bytes")
        old_wh = hash_file(old_img)
        repo.save(
            DesktopState(
                schema_version=2,
                wallpaper=WallpaperEntry(
                    hash_algorithm="sha256",
                    kind="wallpaper",
                    content_hash=old_wh,
                    source_path=str(old_img),
                    imported_at=_now_z(),
                ),
                monitors={
                    "DP-1": MonitorWallpaperConfig(
                        backend=BackendType.swww,
                        source_hash=old_wh,
                        fit_mode=FitMode.contain,
                        mpv_options=None,
                        ipc_socket=None,
                    )
                },
                palette=None,
                effects=None,
                icons=None,
                applied_at=_now_z(),
            )
        )
        new_img = tmp_path / "new.png"
        new_img.write_bytes(b"new bytes")
        use_case, _, _ = _make_swap(state_root)

        result = use_case.run(new_img)

        new_wh = hash_file(new_img)
        assert result.state.monitors["DP-1"].backend == BackendType.swww
        assert result.state.monitors["DP-1"].fit_mode == FitMode.contain
        assert result.state.monitors["DP-1"].source_hash == new_wh

    def test_live_detection_reconciles_monitor_set(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        img = tmp_path / "wall.png"
        img.write_bytes(b"detection bytes")
        use_case, _, _ = _make_swap(state_root, monitor_source=_FakeMonitorSource(["HDMI-1"]))

        result = use_case.run(img)

        assert set(result.state.monitors) == {"HDMI-1"}
        assert {p.name for p in result.repointed} == {
            "wallpaper-HDMI-1.png",
            "wallpaper.png",
        }

    def test_missing_input_raises_without_mutation(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        use_case, _, hyprpaper = _make_swap(state_root)

        with pytest.raises(ValueError, match="not found"):
            use_case.run(tmp_path / "missing.png")

        assert hyprpaper.calls == 0
        assert not (state_root / "current.json").exists()

    def test_directory_input_raises_without_mutation(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        use_case, _, hyprpaper = _make_swap(state_root)

        with pytest.raises(ValueError, match="regular file"):
            use_case.run(tmp_path)

        assert hyprpaper.calls == 0

    def test_corrupt_state_propagates_loudly(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        (state_root / "current.json").write_text("{not json", encoding="utf-8")
        img = tmp_path / "wall.png"
        img.write_bytes(b"corrupt guard bytes")
        use_case, _, hyprpaper = _make_swap(state_root)

        with pytest.raises(ValueError, match="not valid JSON"):
            use_case.run(img)

        assert hyprpaper.calls == 0

    def test_hyprpaper_failure_raises_loudly(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        img = tmp_path / "wall.png"
        img.write_bytes(b"hyprpaper down bytes")
        use_case, _, hyprpaper = _make_swap(state_root, hyprpaper=_FakeHyprpaper(ok=False))

        with pytest.raises(RuntimeError, match="visible swap failed"):
            use_case.run(img)

        assert hyprpaper.calls == 1

    def test_hyprpaper_raise_maps_to_swap_failure(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        img = tmp_path / "wall.png"
        img.write_bytes(b"hyprpaper raising bytes")
        use_case, _, _ = _make_swap(state_root, hyprpaper=_FakeHyprpaper(fail=OSError("ipc down")))

        with pytest.raises(RuntimeError, match="visible swap failed"):
            use_case.run(img)


class _SpySeeder(CacheSeeder):
    """CacheSeeder recording import_wallpaper calls (no-re-import proof)."""

    def __init__(self, state_root: Path) -> None:
        super().__init__(state_root)
        self.imports: list[tuple[str, str]] = []

    def import_wallpaper(self, src: Path, wallpaper_hash: str, *, source_mutable: bool) -> Path:
        self.imports.append((str(src), wallpaper_hash))
        return super().import_wallpaper(src, wallpaper_hash, source_mutable=source_mutable)


class _FakeCsg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("csg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
            "colors.kitty",
        ):
            (output_dir / name).write_text(f"{name} content")
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
                colors_adw_css=hash_file(output_dir / "colors.adw.css"),
                colors_sequences=hash_file(output_dir / "colors.sequences"),
                colors_rasi=hash_file(output_dir / "colors.rasi"),
                colors_kitty=hash_file(output_dir / "colors.kitty"),
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


def _setup_spine(install_spine: Path) -> None:
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
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")


class TestApplyDerivationOnlyDuty:
    """``ApplyWallpaperUseCase`` takes the imported wallpaper + hash."""

    def _make_apply(
        self, tmp_path: Path, *, csg: Any | None = None
    ) -> tuple[Any, _SpySeeder, Path, Path]:
        from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

        state_root = tmp_path / "state"
        install_spine = tmp_path / "install"
        _setup_spine(install_spine)
        seeder = _SpySeeder(state_root)
        use_case = ApplyWallpaperUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            csg=csg or _FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=_FakeMutex(),
        )
        return use_case, seeder, state_root, install_spine

    def test_preimported_hash_skips_reimport(self, tmp_path: Path) -> None:
        img = tmp_path / "wall.png"
        img.write_bytes(b"preimported bytes")
        use_case, seeder, _, _ = self._make_apply(tmp_path)
        wh = hash_file(img)

        result = use_case.run(img, wallpaper_hash=wh)

        assert seeder.imports == []
        assert result.wallpaper_hash == wh
        assert result.state.wallpaper.content_hash == wh
        assert result.state.palette is not None

    def test_standalone_path_still_imports(self, tmp_path: Path) -> None:
        img = tmp_path / "wall.png"
        img.write_bytes(b"standalone bytes")
        use_case, seeder, _, _ = self._make_apply(tmp_path)
        wh = hash_file(img)

        result = use_case.run(img)

        assert seeder.imports == [(str(img.expanduser().resolve()), wh)]
        assert result.wallpaper_hash == wh

    def test_preimported_palette_failure_still_hard_fails(self, tmp_path: Path) -> None:
        img = tmp_path / "wall.png"
        img.write_bytes(b"palette down bytes")
        use_case, seeder, state_root, _ = self._make_apply(tmp_path, csg=_FakeCsg(fail=True))

        with pytest.raises(RuntimeError, match="palette apply failed"):
            use_case.run(img, wallpaper_hash=hash_file(img))

        # Save happens only after all layers succeed/degrade: nothing persisted.
        assert not (state_root / "current.json").exists()
        assert seeder.imports == []


class TestPassThroughSeedMutex:
    def test_hold_yields_without_acquiring(self) -> None:
        from runtime.adapters.flock_seed_mutex import PassThroughSeedMutex

        mutex = PassThroughSeedMutex()
        with mutex.hold(blocking=False):
            with mutex.hold(blocking=True):
                pass

    def test_nests_inside_a_held_flock(self, tmp_path: Path) -> None:
        """The production shape: outer flock held, inners pass through."""
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex, PassThroughSeedMutex

        with FlockSeedMutex(tmp_path / ".seed.lock").hold(blocking=False):
            with PassThroughSeedMutex().hold(blocking=True):
                pass


class TestConvergeCompositionRegression:
    """Task 1.2: Phase-5 converge composition is behavior-preserving."""

    def test_suppress_history_reaches_all_three_seeders(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main
        from runtime.cli.main import _WallpaperSetResult

        seeders: list[Any] = []

        class _FakeSwapUseCase:
            def __init__(self, **kwargs: Any) -> None:
                seeders.append(kwargs["seeder"])

            def run(self, image_path: Path) -> Any:
                return SimpleNamespace(wallpaper_hash="f" * 64)

        class _FakeApplyUseCase:
            def __init__(self, **kwargs: Any) -> None:
                seeders.append(kwargs["seeder"])

            def run(
                self,
                image_path: Path,
                *,
                wallpaper_hash: str | None = None,
                contrast_enabled: bool = True,
                contrast_source: str = "default",
                on_progress: Any | None = None,
            ) -> Any:
                return SimpleNamespace(
                    wallpaper_hash="f" * 64,
                    cache_hit_palette=True,
                    cache_hit_effects=True,
                    cache_hit_icons=True,
                )

        class _FakeReconcileUseCase:
            def __init__(self, **kwargs: Any) -> None:
                seeders.append(kwargs["seeder"])

            def run(
                self,
                trigger: str = "reconcile",
                *,
                contrast_enabled: bool = True,
                contrast_source: str = "default",
                wallpaper_already_applied: bool = False,
            ) -> Any:
                return SimpleNamespace(
                    state=SimpleNamespace(wallpaper=SimpleNamespace(content_hash="f" * 64)),
                    reload_failures=[],
                )

        monkeypatch.setattr("runtime.application.swap_visible.SwapVisibleUseCase", _FakeSwapUseCase)
        monkeypatch.setattr(
            "runtime.application.apply_wallpaper.ApplyWallpaperUseCase", _FakeApplyUseCase
        )
        monkeypatch.setattr(
            "runtime.application.reconcile.ReconcileDesktopStateUseCase",
            _FakeReconcileUseCase,
        )

        img = tmp_path / "wall.png"
        img.write_bytes(b"converge composition bytes")

        class _Client:
            def __init__(self) -> None:
                self.published: list[Any] = []

            def publish(self, topic: str, payload: Any) -> None:
                self.published.append((topic, dict(payload)))

        result = cli_main._run_wallpaper_set(img, suppress_history=True, client=_Client())

        assert isinstance(result, _WallpaperSetResult)
        assert len(seeders) == 3
        assert [s._suppress_history for s in seeders] == [True, True, True]

    def test_run_converge_still_composes_wallpaper_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``_run_converge`` threads ``suppress_history``/``include_terminal``
        into ``_run_wallpaper_set`` (same call shape as before the change)."""
        import json as _json

        import runtime.cli.main as cli_main

        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
        intent = tmp_path / "config-home" / "dotfiles" / "desired.json"
        intent.parent.mkdir(parents=True)
        intent.write_text(
            _json.dumps({"version": 1, "wallpaper": "/img/new.png", "keep": 5, "pinned": []}),
            encoding="utf-8",
        )
        state_root = tmp_path / "state-home" / "dotfiles"
        state_root.mkdir(parents=True, exist_ok=True)
        (state_root / "current.json").write_text(
            _json.dumps(
                {
                    "schema_version": 2,
                    "wallpaper": {
                        "hash": "dd" * 32,
                        "source_path": "/img/old.png",
                        "applied_at": "2026-09-10T00:00:00Z",
                    },
                    "monitors": {},
                    "palette": None,
                    "effects": None,
                    "icons": None,
                    "applied_at": "2026-09-10T00:00:00Z",
                }
            ),
            encoding="utf-8",
        )
        seen: dict[str, Any] = {}

        def _fake_set(target: Path, **kwargs: Any) -> Any:
            seen["target"] = target
            seen["kwargs"] = kwargs
            return SimpleNamespace(
                reconcile=SimpleNamespace(reload_failures=[]),
            )

        monkeypatch.setattr(cli_main, "_run_wallpaper_set", _fake_set)

        cli_main._run_converge(suppress_history=True, allow_delete=False, include_terminal=False)

        assert seen["target"] == Path("/img/new.png")
        assert seen["kwargs"]["suppress_history"] is True
        assert seen["kwargs"]["include_terminal"] is False


class TestVisibleConsumerAudit:
    """Task 2.2: GUI + Phase-5 consumers treat ``visible`` as busy.

    Scanned, never edited — the wallpaper selector is owned by another
    agent (change boundary). These tests pin the safe behavior so a
    future GUI edit cannot silently turn ``visible`` into an unlock.
    """

    def test_selector_event_handler_never_unlocks(self, repo_root: Path) -> None:
        """``onWallpaperEvent`` keeps the selector busy across the WHOLE
        set: ``applying`` and the new ``visible`` both hold the gate; only
        ``done``/``error`` release it. Pins the change-C contract (the
        selector is owned by the GUI workstream, scanned here, never
        edited) so a future edit cannot silently turn ``visible`` into an
        unlock."""
        selector = (
            repo_root
            / "src"
            / "gui-tools"
            / "wallpaper-selector"
            / "ui"
            / "WallpaperSelectorWindow.tsx"
        )
        text = selector.read_text(encoding="utf-8")
        start = text.index("function onWallpaperEvent")
        handler_window = text[start : start + 1200]
        # `visible` is handled explicitly and treated as busy.
        assert '"visible"' in handler_window
        # Every busy state sets eventBusy true; every terminal state clears it.
        assert handler_window.count("eventBusy = true") >= 2  # applying + visible
        assert handler_window.count("eventBusy = false") == 2  # done + error
        # No unlock branch is reachable from applying/visible: the true
        # assignments precede any done/error branch.
        visible_idx = handler_window.index('"visible"')
        first_false = handler_window.index("eventBusy = false")
        assert visible_idx < first_false
        # The gate combines the local in-flight flag with the event state.
        assert "localApply || eventBusy" in text
        # Re-entry is gated on both sources.
        assert "if (localApply || eventBusy) return;" in text

    def test_bar_tracks_no_wallpaper_topic(self) -> None:
        """The bar renders capture/speedtest only — ``wallpaper.state``
        (including ``visible``) is never dispatched to a bar handler."""
        from runtime.adapters.bar_subscriber import BAR_TOPICS

        assert "wallpaper.state" not in BAR_TOPICS

    def test_daemon_converge_ignores_wallpaper_state(self, repo_root: Path) -> None:
        """The reactive converge observes inputs, not ``wallpaper.state``:
        no special-casing is needed for the new event."""
        converge = repo_root / "src" / "runtime" / "src" / "runtime" / "application" / "converge.py"
        assert "wallpaper.state" not in converge.read_text(encoding="utf-8")
        assert "visible" not in converge.read_text(encoding="utf-8")
