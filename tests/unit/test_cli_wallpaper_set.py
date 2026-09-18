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
        repointed=repointed if repointed is not None else [Path("/state/current/wallpaper-DP-1.png")],
        skipped=[],
        state=state,
        cache_regenerated=[],
        reload_failures=reload_failures or [],
    )


def _set_result(
    *,
    reconcile: Any | None = None,
    **overrides: Any,
) -> Any:
    from runtime.cli.main import _WallpaperSetResult

    return _WallpaperSetResult(apply=_result(**overrides), reconcile=reconcile or _reconcile_result())


@pytest.fixture(autouse=True)
def _quiet_seed_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Point first-run seeding at an empty spine so it skips quietly."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _fake_composition(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> None:
    def _run(image_path: Path) -> Any:
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
    def test_composition_root_wires_apply_then_reconcile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``wallpaper set`` composition root constructs
        ``ReconcileDesktopStateUseCase`` with the five reloaders and the
        same ``state_root``, and runs it with trigger ``"set"`` (mirrors
        TestReconcileCompositionRootWiring in test_cli_reconcile.py)."""
        captured: dict[str, Any] = {}

        class _FakeApplyUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["apply_kwargs"] = kwargs

            def run(self, image_path: Path) -> Any:
                return _result()

        class _FakeReconcileUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["reconcile_kwargs"] = kwargs

            def run(self, trigger: str = "reconcile") -> Any:
                captured["trigger"] = trigger
                return _reconcile_result()

        monkeypatch.setattr(
            "runtime.application.apply_wallpaper.ApplyWallpaperUseCase", _FakeApplyUseCase
        )
        monkeypatch.setattr(
            "runtime.application.reconcile.ReconcileDesktopStateUseCase",
            _FakeReconcileUseCase,
        )
        import runtime.cli.main as cli_main

        cli_main._run_wallpaper_set(Path("/img/wall.png"))

        from runtime.adapters.ags_reloader import AgsReloader
        from runtime.adapters.csg_adapter import CsgAdapter
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex
        from runtime.adapters.gtk4_app_reloader import Gtk4AppReloader
        from runtime.adapters.hyprland_reloader import HyprlandReloader
        from runtime.adapters.hyprpaper_reloader import HyprpaperReloader
        from runtime.adapters.itr_adapter import ItrAdapter
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.adapters.kitty_reloader import KittyReloader
        from runtime.adapters.seeder import CacheSeeder
        from runtime.adapters.terminal_color_applier import TerminalColorApplier
        from runtime.adapters.weg_adapter import WegAdapter

        assert captured["trigger"] == "set"
        # The SAME real adapter family is wired into BOTH use cases — a
        # swapped/missing/forgotten adapter (e.g. weg=CsgAdapter()) fails here.
        for side in ("apply_kwargs", "reconcile_kwargs"):
            kwargs = captured[side]
            assert isinstance(kwargs["csg"], CsgAdapter)
            assert isinstance(kwargs["weg"], WegAdapter)
            assert isinstance(kwargs["itr"], ItrAdapter)
            assert isinstance(kwargs["state_repo"], JsonStateRepository)
            assert isinstance(kwargs["seeder"], CacheSeeder)
            assert isinstance(kwargs["mutex"], FlockSeedMutex)
        assert captured["apply_kwargs"]["state_root"] == captured["reconcile_kwargs"]["state_root"]
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
) -> None:
    """Route the real ``_run_wallpaper_set`` through canned use cases."""

    class _FakeApplyUseCase:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, image_path: Path) -> Any:
            if apply_error is not None:
                raise apply_error
            return apply_result

    class _FakeReconcileUseCase:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, trigger: str = "reconcile") -> Any:
            return _reconcile_result()

    monkeypatch.setattr(
        "runtime.application.apply_wallpaper.ApplyWallpaperUseCase", _FakeApplyUseCase
    )
    monkeypatch.setattr(
        "runtime.application.reconcile.ReconcileDesktopStateUseCase",
        _FakeReconcileUseCase,
    )


class TestWallpaperStateEmission:
    """``wallpaper.state`` transitions around the real composition root."""

    def test_success_publishes_applying_then_done(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from runtime.adapters.hashing import hash_file
        import runtime.cli.main as cli_main

        img = tmp_path / "wall.png"
        img.write_bytes(b"state emit bytes")
        _fake_set_use_cases(monkeypatch, apply_result=_result())
        client = _RecordingClient()

        cli_main._run_wallpaper_set(img, client=client)

        assert client.published == [
            ("wallpaper.state", {"state": "applying", "wallpaper_hash": hash_file(img)}),
            ("wallpaper.state", {"state": "done", "wallpaper_hash": "f" * 64}),
        ]

    def test_apply_failure_publishes_error_and_reraises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from runtime.adapters.hashing import hash_file
        import runtime.cli.main as cli_main

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
            ("wallpaper.state", {"state": "error", "wallpaper_hash": hash_file(img)}),
        ]

    def test_missing_input_publishes_error_with_empty_hash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import runtime.cli.main as cli_main

        _fake_set_use_cases(
            monkeypatch,
            apply_result=_result(),
            apply_error=ValueError("wallpaper image not found"),
        )
        client = _RecordingClient()

        with pytest.raises(ValueError, match="not found"):
            cli_main._run_wallpaper_set(tmp_path / "missing.png", client=client)

        assert client.published == [
            ("wallpaper.state", {"state": "applying", "wallpaper_hash": ""}),
            ("wallpaper.state", {"state": "error", "wallpaper_hash": ""}),
        ]

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
