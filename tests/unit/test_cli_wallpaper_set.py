"""Unit tests for the ``wallpaper set`` CLI command (review P2).

Covers the CLI-layer logic the use-case tests cannot reach: exception →
ErrorView mapping, exit codes, the summary/cache-hit rendering. The
composition function is faked at the module boundary — adapter wiring is
exercised by the integration suite; first-run seeding is pointed at an
empty spine so the root callback skips quietly.
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
        _fake_composition(monkeypatch, lambda img: _result())
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 0
        assert "wallpaper applied" in result.output
        assert "palette generated" in result.output
        assert "effects unavailable" in result.output

    def test_cache_hits_reflected_in_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        res = _result(
            cache_hit_palette=True, cache_hit_effects=True, cache_hit_icons=True
        )
        _fake_composition(monkeypatch, lambda img: res)
        result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])

        assert result.exit_code == 0
        assert "palette cache hit" in result.output

    def test_json_format_renders_structured_object(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_composition(monkeypatch, lambda img: _result())
        result = runner.invoke(
            app, ["wallpaper", "set", "/img/wall.png", "--format", "json"]
        )

        assert result.exit_code == 0
        assert '"palette"' in result.output
        assert '"cache_hits"' in result.output


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
