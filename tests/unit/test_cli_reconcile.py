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
        """AC 5 — the reconcile composition root wires Hyprland, AGS and
        Hyprpaper (with the reconcile ``state_root``), in that order."""
        captured: dict[str, Any] = {}

        class _FakeUseCase:
            def __init__(self, **kwargs: Any) -> None:
                captured["reloaders"] = kwargs.get("reloaders")
                captured["state_root"] = kwargs.get("state_root")

            def run(self) -> Any:
                return _reconcile_result()

        monkeypatch.setattr(
            "runtime.application.reconcile.ReconcileDesktopStateUseCase", _FakeUseCase
        )
        cli_main._run_reconcile()

        from runtime.adapters.ags_reloader import AgsReloader
        from runtime.adapters.hyprland_reloader import HyprlandReloader
        from runtime.adapters.hyprpaper_reloader import HyprpaperReloader

        reloaders = captured["reloaders"]
        assert reloaders is not None
        assert [type(r) for r in reloaders] == [HyprlandReloader, AgsReloader, HyprpaperReloader]
        assert reloaders[2]._state_root == captured["state_root"]  # type: ignore[attr-defined]


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
