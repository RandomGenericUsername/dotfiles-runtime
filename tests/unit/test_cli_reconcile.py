"""Unit tests for the ``reconcile`` CLI command (Story 2.1).

Covers: exit code, summary rendering, absent-state failure mapping,
--format json object shape. Mirrors test_cli_wallpaper_set.py patterns.
"""

from __future__ import annotations

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

import runtime.cli.main as cli_main

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


def _reconcile_result() -> Any:
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
        repointed=[Path("/state/current/wallpaper-DP-1.png")],
        skipped=[],
        state=state,
        cache_regenerated=[],
    )


@pytest.fixture(autouse=True)
def _quiet_seed_hook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Point first-run seeding at an empty spine so it skips quietly."""
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


def _fake_composition(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> None:
    def _run() -> Any:
        return behavior()

    monkeypatch.setattr("runtime.cli.main._run_reconcile", _run)


class TestReconcileCliSuccess:
    def test_success_exits_zero_and_renders_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _reconcile_result())
        result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 0
        assert "desktop reconciled" in result.output
        assert "1 symlink(s) repointed" in result.output

    def test_json_format_renders_structured_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda: _reconcile_result())
        result = runner.invoke(app, ["reconcile", "--format", "json"])

        assert result.exit_code == 0
        assert '"repointed"' in result.output
        assert '"skipped"' in result.output
        assert '"cache_regenerated"' in result.output

    def test_skipped_count_in_summary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        res = _reconcile_result()
        res = type(res)(
            repointed=res.repointed,
            skipped=["effects (layer is null; nothing to repoint)"],
            state=res.state,
            cache_regenerated=res.cache_regenerated,
        )
        _fake_composition(monkeypatch, lambda: res)
        result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 0
        assert "1 skipped" in result.output


class TestReconcileCompositionRootWiring:
    def test_composition_root_wires_all_reloaders(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC 5 — the reconcile composition root wires Hyprland, AGS,
        the terminal palette applier (each with the reconcile
        ``state_root``), then the kitty reloader, in that order, plus a
        separate Hyprpaper wallpaper applier (wallpaper application is
        its own stage, not a palette/UI reload)."""
        captured: dict[str, Any] = {}

        class _FakeUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["reloaders"] = kwargs.get("reloaders")
                captured["state_root"] = kwargs.get("state_root")
                captured["wallpaper_applier"] = kwargs.get("wallpaper_applier")

            def run(self) -> Any:
                return _reconcile_result()

        monkeypatch.setattr(
            "runtime.application.reconcile.ReconcileDesktopStateUseCase", _FakeUseCase
        )
        cli_main._run_reconcile()

        from runtime.adapters.ags_reloader import AgsReloader
        from runtime.adapters.gtk4_app_reloader import Gtk4AppReloader
        from runtime.adapters.hyprland_reloader import HyprlandReloader
        from runtime.adapters.hyprpaper_reloader import HyprpaperWallpaperApplier
        from runtime.adapters.kitty_reloader import KittyReloader
        from runtime.adapters.terminal_color_applier import TerminalColorApplier

        reloaders = captured["reloaders"]
        assert reloaders is not None
        assert [type(r) for r in reloaders] == [
            HyprlandReloader,
            AgsReloader,
            TerminalColorApplier,
            KittyReloader,
        ]
        assert not any(type(r) is Gtk4AppReloader for r in reloaders)
        assert reloaders[2]._state_root == captured["state_root"]  # type: ignore[attr-defined]
        assert type(captured["wallpaper_applier"]) is HyprpaperWallpaperApplier

    def test_composition_root_excludes_terminal_when_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The daemon passes ``include_terminal=False``: no
        ``TerminalColorApplier`` (no controlling tty), kitty still present."""
        captured: dict[str, Any] = {}

        class _FakeUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["reloaders"] = kwargs.get("reloaders")

            def run(self) -> Any:
                return _reconcile_result()

        monkeypatch.setattr(
            "runtime.application.reconcile.ReconcileDesktopStateUseCase", _FakeUseCase
        )
        cli_main._run_reconcile(include_terminal=False)

        from runtime.adapters.kitty_reloader import KittyReloader

        names = [type(r).__name__ for r in captured["reloaders"]]
        assert "TerminalColorApplier" not in names
        assert names[-1] == KittyReloader.__name__

    def test_composition_root_passes_spine_templates_dir_to_csg(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Regression: the CSG adapter must receive the spine-resolved
        templates dir. Its own discovery walks up from ``__file__``, which only
        finds the repo templates when the runtime runs from a checkout; the
        installed uv tool (what the daemon executes) finds nothing, so a bare
        ``CsgAdapter()`` makes every reactive converge die with ``CSG templates
        dir not found: no default templates dir discovered``."""
        spine = tmp_path / "spine"
        templates_dir = spine / "config" / "color-scheme-generator" / "templates"
        templates_dir.mkdir(parents=True)
        (templates_dir / "colors.kitty.j2").write_text("background #000000\n")
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(spine))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

        captured: dict[str, Any] = {}

        class _FakeUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["csg"] = kwargs.get("csg")

            def run(self) -> Any:
                return _reconcile_result()

        monkeypatch.setattr(
            "runtime.application.reconcile.ReconcileDesktopStateUseCase", _FakeUseCase
        )
        cli_main._run_reconcile()

        csg = captured["csg"]
        assert csg is not None
        assert csg._templates_dir == templates_dir.resolve()  # type: ignore[attr-defined]

    def test_build_reloaders_include_terminal_flag(self) -> None:
        from runtime.adapters.gtk4_app_reloader import Gtk4AppReloader
        from runtime.adapters.kitty_reloader import KittyReloader
        from runtime.adapters.terminal_color_applier import TerminalColorApplier

        with_terminal = cli_main._build_reloaders(Path("/tmp/state"), include_terminal=True)
        without_terminal = cli_main._build_reloaders(Path("/tmp/state"), include_terminal=False)

        assert type(with_terminal[-2]) is TerminalColorApplier
        assert type(with_terminal[-1]) is KittyReloader
        assert not any(type(r) is TerminalColorApplier for r in without_terminal)
        assert type(without_terminal[-1]) is KittyReloader
        assert not any(type(r) is Gtk4AppReloader for r in with_terminal)
        assert not any(type(r) is Gtk4AppReloader for r in without_terminal)


class TestReconcileCliErrorMapping:
    def test_runtime_error_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise RuntimeError("nothing to reconcile: no current state")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 1
        assert "nothing to reconcile" in result.output

    def test_value_error_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise ValueError("current.json is not valid JSON")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 1
        assert "not valid JSON" in result.output

    def test_unexpected_exception_maps_to_exit_1(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> Any:
            raise KeyError("surprise")

        _fake_composition(monkeypatch, _boom)
        result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 1
        assert "unexpectedly" in result.output


class _RecordingClient:
    """Fake IJobClient recording ``wallpaper.state`` publishes."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, topic: str, payload: Any) -> None:
        self.published.append((topic, dict(payload)))


@pytest.fixture(autouse=True)
def _no_bus_wallpaper_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests stay off the bus; tests that assert clicks re-patch."""
    monkeypatch.setattr(cli_main, "_build_wallpaper_client", _RecordingClient)


class TestReconcilePublishCoverage:
    def test_done_publishes_trigger_reconcile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _RecordingClient()
        monkeypatch.setattr(cli_main, "_build_wallpaper_client", lambda: client)
        _fake_composition(monkeypatch, lambda: _reconcile_result())

        result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 0
        assert client.published == [
            (
                "wallpaper.state",
                {"state": "applying", "wallpaper_hash": "", "trigger": "reconcile"},
            ),
            (
                "wallpaper.state",
                {"state": "done", "wallpaper_hash": "f" * 64, "trigger": "reconcile"},
            ),
        ]

    def test_failure_publishes_error_trigger_reconcile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _RecordingClient()
        monkeypatch.setattr(cli_main, "_build_wallpaper_client", lambda: client)

        def _boom() -> Any:
            raise RuntimeError("nothing to reconcile: no current state")

        _fake_composition(monkeypatch, _boom)

        result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 1
        assert client.published == [
            (
                "wallpaper.state",
                {"state": "applying", "wallpaper_hash": "", "trigger": "reconcile"},
            ),
            (
                "wallpaper.state",
                {"state": "error", "wallpaper_hash": "", "trigger": "reconcile"},
            ),
        ]

    def test_publish_failure_never_fails_the_command(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _BoomClient:
            def publish(self, topic: str, payload: Any) -> None:
                raise RuntimeError("bus down")

        monkeypatch.setattr(cli_main, "_build_wallpaper_client", lambda: _BoomClient())
        _fake_composition(monkeypatch, lambda: _reconcile_result())

        result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 0
        assert "desktop reconciled" in result.output
