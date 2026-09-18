"""Unit tests for the ``wallpaper set`` CLI command (review P2).

Covers the CLI-layer logic the use-case tests cannot reach: exception →
ErrorView mapping, exit codes, the summary/cache-hit rendering. The
composition function is faked at the module boundary — adapter wiring is
exercised by the integration suite; first-run seeding is pointed at an
empty spine so the root callback skips quietly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from runtime.cli.main import app
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    PaletteArtifacts,
    PaletteEntry,
    WallpaperEntry,
)

runner = CliRunner()


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _palette() -> PaletteEntry:
    return PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash="e" * 64,
        source_wallpaper_hash="f" * 64,
        input_template_hash="c" * 64,
        artifact_hashes=PaletteArtifacts(
            colors_yaml="a" * 64,
            colors_conf="b" * 64,
            colors_gtk_css="d" * 64,
            colors_adw_css="e" * 64,
            colors_sequences="f" * 64,
            colors_rasi="a" * 64,
            colors_kitty="a" * 64,
        ),
        generated_at=_now_z(),
    )


def _result(**overrides: Any) -> Any:
    from runtime.application.apply_wallpaper import ApplyWallpaperResult

    wh = "f" * 64
    now = _now_z()
    state = DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wh,
            source_path="/img/wall.png",
            imported_at=now,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wh,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=_palette(),
        effects=None,
        icons=None,
        applied_at=now,
    )
    kwargs: dict[str, Any] = {
        "wallpaper_hash": wh,
        "palette": state.palette,
        "effects": None,
        "icons": None,
        "cache_hit_palette": False,
        "cache_hit_effects": False,
        "cache_hit_icons": False,
        "state": state,
    }
    kwargs.update(overrides)
    return ApplyWallpaperResult(**kwargs)


def _reconcile_result(
    *, repointed: list[Path] | None = None, reload_failures: list[str] | None = None
) -> Any:
    from runtime.application.reconcile import ReconcileResult

    wh = "f" * 64
    now = _now_z()
    state = DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wh,
            source_path="/img/wall.png",
            imported_at=now,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wh,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=_palette(),
        effects=None,
        icons=None,
        applied_at=now,
    )
    return ReconcileResult(
        repointed=(
            repointed if repointed is not None else [Path("/state/current/wallpaper-DP-1.png")]
        ),
        skipped=[],
        state=state,
        cache_regenerated=[],
        reload_failures=reload_failures or [],
    )


def _swap_result(wallpaper_hash: str | None = None) -> Any:
    from runtime.application.swap_visible import SwapVisibleResult

    wh = wallpaper_hash or "f" * 64
    now = _now_z()
    state = DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wh,
            source_path="/img/wall.png",
            imported_at=now,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wh,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=None,
        effects=None,
        icons=None,
        applied_at=now,
    )
    return SwapVisibleResult(
        wallpaper_hash=wh,
        wallpaper_path=Path(f"/state/cache/wallpapers/{wh}/wallpaper.png"),
        repointed=[Path("/state/current/wallpaper-DP-1.png")],
        state=state,
    )


def _set_result(
    *,
    reconcile: Any | None = None,
    **overrides: Any,
) -> Any:
    from runtime.cli.main import _WallpaperSetResult

    return _WallpaperSetResult(
        apply=_result(**overrides), reconcile=reconcile or _reconcile_result()
    )


@pytest.fixture(autouse=True)
def _quiet_seed_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Point first-run seeding at an empty spine so it skips quietly."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _fake_composition(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> None:
    def _run(image_path: Path, **kwargs: Any) -> Any:
        return behavior(image_path)

    monkeypatch.setattr("runtime.cli.main._run_wallpaper_set", _run)


class TestWallpaperSetCliSuccess:
    def test_success_exits_zero_and_renders_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda img: _set_result())
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 0
        assert "wallpaper applied" in result.output
        assert "palette generated" in result.output
        assert "effects unavailable" in result.output
        # visible-first phases line (D1)
        assert "visible in" in result.output
        assert "themed in" in result.output

    def test_cache_hits_reflected_in_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = _set_result(
            cache_hit_palette=True, cache_hit_effects=True, cache_hit_icons=True
        )
        _fake_composition(monkeypatch, lambda img: res)
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 0
        assert "palette cache hit" in result.output

    def test_json_format_renders_structured_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda img: _set_result())
        result = runner.invoke(
            app, ["wallpaper", "set", "/img/wall.png", "--format", "json"]
        )

        assert result.exit_code == 0
        obj = json.loads(result.output)
        # apply-pass descriptors
        assert obj["palette"] == "e" * 64
        assert obj["cache_hits"] == {
            "palette": False,
            "effects": False,
            "icons": False,
        }
        # authoritative post-swap reconcile state + swap/reload data — with
        # VALUES, not key-presence (a mis-shaped/mis-keyed render fails here)
        assert obj["wallpaper"] == "f" * 64
        assert obj["repointed"] == ["/state/current/wallpaper-DP-1.png"]
        assert obj["skipped"] == []
        assert obj["cache_regenerated"] == []
        assert obj["reload_failures"] == []
        # additive phase timestamps (visible-first D1)
        assert "visible_at" in obj and "themed_at" in obj

    def test_reload_success_renders_repointed_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = _set_result(
            reconcile=_reconcile_result(
                repointed=[
                    Path("/state/current/wallpaper-DP-1.png"),
                    Path("/state/current/colors.conf"),
                ]
            )
        )
        _fake_composition(monkeypatch, lambda img: res)
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 0
        assert "2 symlink(s) repointed" in result.output


class TestWallpaperSetReloadFailureExit:
    def test_reload_failures_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = _set_result(
            reconcile=_reconcile_result(reload_failures=["HyprpaperReloader"])
        )
        _fake_composition(monkeypatch, lambda img: res)
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 1
        assert "reload failed for: HyprpaperReloader" in result.output


class TestWallpaperSetCompositionRootWiring:
    def test_composition_root_wires_swap_then_apply_then_reconcile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``wallpaper set`` composition root constructs
        ``SwapVisibleUseCase`` + ``ApplyWallpaperUseCase`` +
        ``ReconcileDesktopStateUseCase`` with the pinned adapter family,
        holds ONE non-blocking flock across both phases, and runs
        reconcile with trigger ``"set"`` (mirrors
        TestReconcileCompositionRootWiring in test_cli_reconcile.py)."""
        captured: dict[str, Any] = {}
        holds: list[bool] = []

        class _FakeSwapUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["swap_kwargs"] = kwargs

            def run(self, image_path: Path) -> Any:
                captured["swap_image"] = image_path
                return _swap_result()

        class _FakeApplyUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["apply_kwargs"] = kwargs

            def run(
                self,
                image_path: Path,
                *,
                wallpaper_hash: str | None = None,
                contrast_enabled: bool = True,
                contrast_source: str = "default",
            ) -> Any:
                captured["apply_hash"] = wallpaper_hash
                captured["apply_contrast"] = (contrast_enabled, contrast_source)
                return _result()

        class _FakeReconcileUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["reconcile_kwargs"] = kwargs

            def run(
                self,
                trigger: str = "reconcile",
                *,
                contrast_enabled: bool = True,
                contrast_source: str = "default",
            ) -> Any:
                captured["trigger"] = trigger
                captured["reconcile_contrast"] = (contrast_enabled, contrast_source)
                return _reconcile_result()

        monkeypatch.setattr("runtime.application.swap_visible.SwapVisibleUseCase", _FakeSwapUseCase)
        monkeypatch.setattr(
            "runtime.application.apply_wallpaper.ApplyWallpaperUseCase", _FakeApplyUseCase
        )
        monkeypatch.setattr(
            "runtime.application.reconcile.ReconcileDesktopStateUseCase",
            _FakeReconcileUseCase,
        )
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex as _RealMutex

        class _RecordingMutex(_RealMutex):
            def hold(self, blocking: bool = False) -> Any:
                holds.append(blocking)
                return super().hold(blocking=blocking)

        monkeypatch.setattr("runtime.adapters.flock_seed_mutex.FlockSeedMutex", _RecordingMutex)
        import runtime.cli.main as cli_main

        cli_main._run_wallpaper_set(Path("/img/wall.png"))

        from runtime.adapters.ags_reloader import AgsReloader
        from runtime.adapters.csg_adapter import CsgAdapter
        from runtime.adapters.flock_seed_mutex import PassThroughSeedMutex
        from runtime.adapters.gtk4_app_reloader import Gtk4AppReloader
        from runtime.adapters.hyprland_monitor_source import HyprlandMonitorSource
        from runtime.adapters.hyprland_reloader import HyprlandReloader
        from runtime.adapters.hyprpaper_reloader import HyprpaperReloader
        from runtime.adapters.itr_adapter import ItrAdapter
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.adapters.kitty_reloader import KittyReloader
        from runtime.adapters.seeder import CacheSeeder
        from runtime.adapters.terminal_color_applier import TerminalColorApplier
        from runtime.adapters.weg_adapter import WegAdapter

        # ONE outer acquire, non-blocking (fail-busy), across both phases.
        assert holds == [False]
        assert captured["trigger"] == "set"
        # Default policy threads through both phases (guard ON, default source).
        assert captured["apply_contrast"] == (True, "default")
        assert captured["reconcile_contrast"] == (True, "default")
        # The swap's hash threads into the derivation-only apply (no re-import).
        assert captured["apply_hash"] == "f" * 64
        assert captured["swap_image"] == Path("/img/wall.png")
        # The SAME real adapter family is wired into ALL THREE use cases — a
        # swapped/missing/forgotten adapter (e.g. weg=CsgAdapter()) fails here.
        for side in ("swap_kwargs", "apply_kwargs", "reconcile_kwargs"):
            kwargs = captured[side]
            assert isinstance(kwargs["state_repo"], JsonStateRepository)
            assert isinstance(kwargs["seeder"], CacheSeeder)
            # Inner use cases take the pass-through: the outer hold is the
            # single serialization point (re-acquiring would fail/deadlock).
            assert isinstance(kwargs["mutex"], PassThroughSeedMutex)
        assert (
            captured["swap_kwargs"]["state_root"]
            == captured["apply_kwargs"]["state_root"]
            == captured["reconcile_kwargs"]["state_root"]
        )
        for side in ("apply_kwargs", "reconcile_kwargs"):
            kwargs = captured[side]
            assert isinstance(kwargs["csg"], CsgAdapter)
            assert isinstance(kwargs["weg"], WegAdapter)
            assert isinstance(kwargs["itr"], ItrAdapter)
        assert isinstance(captured["swap_kwargs"]["hyprpaper"], HyprpaperReloader)
        assert isinstance(captured["swap_kwargs"]["monitor_source"], HyprlandMonitorSource)
        assert isinstance(captured["apply_kwargs"]["monitor_source"], HyprlandMonitorSource)
        reloaders = captured["reconcile_kwargs"]["reloaders"]
        assert reloaders is not None
        assert [type(r) for r in reloaders] == [
            HyprlandReloader,
            AgsReloader,
            HyprpaperReloader,
            Gtk4AppReloader,
            TerminalColorApplier,
            KittyReloader,
        ]
        assert reloaders[2]._state_root == captured["reconcile_kwargs"]["state_root"]  # type: ignore[attr-defined]
        assert reloaders[4]._state_root == captured["reconcile_kwargs"]["state_root"]  # type: ignore[attr-defined]


class TestWallpaperSetContrastFlag:
    """``wallpaper set --contrast`` (icon-contrast-opt-out §2.2).

    Drives the REAL ``_run_wallpaper_set`` through capturing use-case
    fakes: explicit flags persist to the governing hash BEFORE deriving
    and thread ``(enabled, "flag")`` into both phases; ``auto`` resolves
    store → default ON and never writes.
    """

    def _capturing_use_cases(
        self, monkeypatch: pytest.MonkeyPatch, captured: dict[str, Any]
    ) -> None:
        class _FakeSwapUseCase:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def run(self, image_path: Path) -> Any:
                return _swap_result()

        class _FakeApplyUseCase:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def run(
                self,
                image_path: Path,
                *,
                wallpaper_hash: str | None = None,
                contrast_enabled: bool = True,
                contrast_source: str = "default",
            ) -> Any:
                captured["apply"] = (contrast_enabled, contrast_source)
                return _result()

        class _FakeReconcileUseCase:
            def __init__(self, **kwargs: Any) -> None:
                pass

            def run(
                self,
                trigger: str = "reconcile",
                *,
                contrast_enabled: bool = True,
                contrast_source: str = "default",
            ) -> Any:
                captured["reconcile"] = (contrast_enabled, contrast_source)
                return _reconcile_result()

        monkeypatch.setattr(
            "runtime.application.swap_visible.SwapVisibleUseCase", _FakeSwapUseCase
        )
        monkeypatch.setattr(
            "runtime.application.apply_wallpaper.ApplyWallpaperUseCase", _FakeApplyUseCase
        )
        monkeypatch.setattr(
            "runtime.application.reconcile.ReconcileDesktopStateUseCase",
            _FakeReconcileUseCase,
        )

    def _store_file(self, tmp_path: Path) -> Path:
        # Mirrors the autouse ``_quiet_seed_hook`` env (XDG_STATE_HOME).
        return tmp_path / "state-home" / "dotfiles" / "icon-contrast.json"

    def test_off_persists_false_and_threads_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main
        from runtime.adapters.hashing import hash_file
        from runtime.adapters.icon_contrast_prefs_store import read_prefs

        captured: dict[str, Any] = {}
        self._capturing_use_cases(monkeypatch, captured)
        img = tmp_path / "wall.png"
        img.write_bytes(b"contrast off bytes")

        cli_main._run_wallpaper_set(img, contrast="off", client=_RecordingClient())

        assert read_prefs(self._store_file(tmp_path)) == {hash_file(img): False}
        assert captured["apply"] == (False, "flag")
        assert captured["reconcile"] == (False, "flag")

    def test_on_persists_true(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main
        from runtime.adapters.hashing import hash_file
        from runtime.adapters.icon_contrast_prefs_store import read_prefs

        captured: dict[str, Any] = {}
        self._capturing_use_cases(monkeypatch, captured)
        img = tmp_path / "wall.png"
        img.write_bytes(b"contrast on bytes")

        cli_main._run_wallpaper_set(img, contrast="on", client=_RecordingClient())

        assert read_prefs(self._store_file(tmp_path)) == {hash_file(img): True}
        assert captured["apply"] == (True, "flag")

    def test_auto_follows_stored_opt_out_without_writing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main
        from runtime.adapters.hashing import hash_file
        from runtime.adapters.icon_contrast_prefs_store import write_pref

        captured: dict[str, Any] = {}
        self._capturing_use_cases(monkeypatch, captured)
        img = tmp_path / "wall.png"
        img.write_bytes(b"stored opt-out bytes")
        store = self._store_file(tmp_path)
        write_pref(store, hash_file(img), False)
        before = store.read_text(encoding="utf-8")

        cli_main._run_wallpaper_set(img, client=_RecordingClient())

        assert captured["apply"] == (False, "store")
        assert captured["reconcile"] == (False, "store")
        assert store.read_text(encoding="utf-8") == before  # auto never writes

    def test_auto_absent_entry_defaults_on_without_creating_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main

        captured: dict[str, Any] = {}
        self._capturing_use_cases(monkeypatch, captured)
        img = tmp_path / "wall.png"
        img.write_bytes(b"no pref bytes")

        cli_main._run_wallpaper_set(img, client=_RecordingClient())

        assert captured["apply"] == (True, "default")
        assert not self._store_file(tmp_path).exists()

    def test_invalid_flag_rejected_at_cli(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No composition fake: validation fires before any FS/mutex work.
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png", "--contrast", "x"])
        assert result.exit_code == 1
        assert "invalid contrast flag" in result.output

    def test_cli_option_threads_into_composition(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict[str, Any] = {}

        def _run(image_path: Path, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return _set_result()

        monkeypatch.setattr("runtime.cli.main._run_wallpaper_set", _run)
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png", "--contrast", "off"])

        assert result.exit_code == 0
        assert seen.get("contrast") == "off"


class TestWallpaperSetCliErrorMapping:
    def test_value_error_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(img: Path) -> Any:
            raise ValueError("wallpaper image not found: /img/nope.png")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["wallpaper", "set", "/img/nope.png"])

        assert result.exit_code == 1
        assert "wallpaper image not found" in result.output

    def test_runtime_error_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(img: Path) -> Any:
            raise RuntimeError("palette apply failed: csg exploded")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 1
        assert "palette apply failed" in result.output

    def test_os_error_maps_to_exit_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(img: Path) -> Any:
            raise PermissionError("unreadable input")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 1
        assert "unreadable input" in result.output

    def test_unexpected_exception_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(img: Path) -> Any:
            raise KeyError("surprise")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 1
        assert "unexpectedly" in result.output


class _RecordingClient:
    """Fake IJobClient recording ``wallpaper.state`` publishes."""

    def __init__(self, *, fail_publish: bool = False) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []
        self.fail_publish = fail_publish

    def publish(self, topic: str, payload: Any) -> None:
        if self.fail_publish:
            raise RuntimeError("bus down")
        self.published.append((topic, dict(payload)))


def _fake_set_use_cases(
    monkeypatch: pytest.MonkeyPatch,
    *,
    apply_result: Any = None,
    apply_error: Exception | None = None,
    swap_result: Any = None,
    swap_error: Exception | None = None,
    reconcile_error: Exception | None = None,
) -> None:
    """Route the real ``_run_wallpaper_set`` through canned use cases."""

    class _FakeSwapUseCase:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, image_path: Path) -> Any:
            if swap_error is not None:
                raise swap_error
            return swap_result if swap_result is not None else _swap_result()

    class _FakeApplyUseCase:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(
            self,
            image_path: Path,
            *,
            wallpaper_hash: str | None = None,
            contrast_enabled: bool = True,
            contrast_source: str = "default",
        ) -> Any:
            if apply_error is not None:
                raise apply_error
            return apply_result

    class _FakeReconcileUseCase:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(
            self,
            trigger: str = "reconcile",
            *,
            contrast_enabled: bool = True,
            contrast_source: str = "default",
        ) -> Any:
            if reconcile_error is not None:
                raise reconcile_error
            return _reconcile_result()

    monkeypatch.setattr("runtime.application.swap_visible.SwapVisibleUseCase", _FakeSwapUseCase)
    monkeypatch.setattr(
        "runtime.application.apply_wallpaper.ApplyWallpaperUseCase", _FakeApplyUseCase
    )
    monkeypatch.setattr(
        "runtime.application.reconcile.ReconcileDesktopStateUseCase",
        _FakeReconcileUseCase,
    )


class TestWallpaperStateEmission:
    """``wallpaper.state`` transitions around the real composition root."""

    def test_success_publishes_applying_visible_then_done(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main
        from runtime.adapters.hashing import hash_file

        img = tmp_path / "wall.png"
        img.write_bytes(b"state emit bytes")
        _fake_set_use_cases(monkeypatch, apply_result=_result())
        client = _RecordingClient()

        result = cli_main._run_wallpaper_set(img, client=client)

        assert client.published == [
            ("wallpaper.state", {"state": "applying", "wallpaper_hash": hash_file(img)}),
            ("wallpaper.state", {"state": "visible", "wallpaper_hash": "f" * 64}),
            ("wallpaper.state", {"state": "done", "wallpaper_hash": "f" * 64}),
        ]
        # Phase timestamps are additive on the result (CLI --format json).
        assert result.visible_at.endswith("Z") and result.themed_at.endswith("Z")
        assert result.swap_elapsed_s >= 0.0 and result.theme_elapsed_s >= 0.0

    def test_post_visible_palette_failure_publishes_error_with_live_hash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """D5: palette fails AFTER the swap — ``error`` carries the LIVE
        hash (the new wallpaper stays on screen), not the pending hash."""
        import runtime.cli.main as cli_main
        from runtime.adapters.hashing import hash_file

        img = tmp_path / "wall.png"
        img.write_bytes(b"doomed emit bytes")
        _fake_set_use_cases(
            monkeypatch,
            apply_result=_result(),
            apply_error=RuntimeError("palette apply failed: boom"),
        )
        client = _RecordingClient()

        with pytest.raises(RuntimeError, match="palette apply failed"):
            cli_main._run_wallpaper_set(img, client=client)

        assert client.published == [
            ("wallpaper.state", {"state": "applying", "wallpaper_hash": hash_file(img)}),
            ("wallpaper.state", {"state": "visible", "wallpaper_hash": "f" * 64}),
            ("wallpaper.state", {"state": "error", "wallpaper_hash": "f" * 64}),
        ]

    def test_swap_failure_publishes_error_with_pending_hash_and_no_visible(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """D5: the swap itself fails — nothing changed on screen, ``error``
        carries the pending hash and no ``visible`` is emitted."""
        import runtime.cli.main as cli_main
        from runtime.adapters.hashing import hash_file

        img = tmp_path / "wall.png"
        img.write_bytes(b"swap doomed bytes")
        _fake_set_use_cases(
            monkeypatch,
            apply_result=_result(),
            swap_error=RuntimeError("visible swap failed: hyprpaper reload reported failure"),
        )
        client = _RecordingClient()

        with pytest.raises(RuntimeError, match="visible swap failed"):
            cli_main._run_wallpaper_set(img, client=client)

        assert client.published == [
            ("wallpaper.state", {"state": "applying", "wallpaper_hash": hash_file(img)}),
            ("wallpaper.state", {"state": "error", "wallpaper_hash": hash_file(img)}),
        ]

    def test_missing_input_publishes_error_with_empty_hash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main

        _fake_set_use_cases(
            monkeypatch,
            apply_result=_result(),
            swap_error=ValueError("wallpaper image not found"),
        )
        client = _RecordingClient()

        with pytest.raises(ValueError, match="not found"):
            cli_main._run_wallpaper_set(tmp_path / "missing.png", client=client)

        assert client.published == [
            ("wallpaper.state", {"state": "applying", "wallpaper_hash": ""}),
            ("wallpaper.state", {"state": "error", "wallpaper_hash": ""}),
        ]

    def test_busy_second_set_fails_without_mutation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """D1: a set arriving while another holds the mutex fails busy —
        non-zero (``SeedLockedError`` is a ``RuntimeError``), no mutation,
        ``error`` with an empty hash, no ``visible``/``done``."""
        import runtime.cli.main as cli_main
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import SeedLockedError

        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
        state_root = tmp_path / "state-home" / "dotfiles"
        state_root.mkdir(parents=True)
        img = tmp_path / "wall.png"
        img.write_bytes(b"busy bytes")
        _fake_set_use_cases(monkeypatch, apply_result=_result())
        client = _RecordingClient()

        with FlockSeedMutex(state_root / ".seed.lock").hold(blocking=False):
            with pytest.raises(SeedLockedError, match="already in progress"):
                cli_main._run_wallpaper_set(img, client=client)

        assert client.published == [
            ("wallpaper.state", {"state": "applying", "wallpaper_hash": hash_file(img)}),
            ("wallpaper.state", {"state": "error", "wallpaper_hash": ""}),
        ]
        assert not (state_root / "current.json").exists()
        assert not (state_root / "history.jsonl").exists()

    def test_publish_failure_never_fails_the_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main

        img = tmp_path / "wall.png"
        img.write_bytes(b"bus down bytes")
        _fake_set_use_cases(monkeypatch, apply_result=_result())
        client = _RecordingClient(fail_publish=True)

        result = cli_main._run_wallpaper_set(img, client=client)

        assert result.reconcile.state.wallpaper.content_hash == "f" * 64
        assert client.published == []

    def test_build_client_degrades_without_daemon(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import runtime.cli.main as cli_main
        from runtime.adapters.daemon_status import DaemonStatusSnapshot
        from runtime.adapters.local_job_client import LocalJobClient

        monkeypatch.setattr(
            "runtime.adapters.daemon_status.probe_session_bus",
            lambda timeout=5.0: DaemonStatusSnapshot(
                bus_available=False,
                name_owned=False,
                detail="no owner for the hub name",
            ),
        )

        assert isinstance(cli_main._build_wallpaper_client(), LocalJobClient)
