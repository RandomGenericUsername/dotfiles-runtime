"""CLI tests for the auto inputs check on ``wallpaper set`` (Story 4.7).

Covers: successful set runs the check and surfaces stale/fresh (AC 1),
failed set runs NO check (AC 2, injected counter), reuse of
``_run_check_inputs`` (AC 3), and informational semantics — stale keeps
exit 0, check failure warns without failing the set (AC 4). The composition
functions are faked at the module boundary; adapter wiring is the
integration suite's job.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.application.check_inputs import CheckInputsResult
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


def _state() -> DesktopState:
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash="f" * 64,
            source_path="/img/wall.png",
            imported_at=_now_z(),
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash="f" * 64,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=PaletteEntry(
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
            ),
            generated_at=_now_z(),
        ),
        effects=None,
        icons=None,
        applied_at=_now_z(),
    )


def _set_result(reload_failures: list[str] | None = None) -> Any:
    from runtime.application.apply_wallpaper import ApplyWallpaperResult
    from runtime.application.reconcile import ReconcileResult
    from runtime.cli.main import _WallpaperSetResult

    reconcile = ReconcileResult(
        repointed=[Path("/state/current/wallpaper-DP-1.png")],
        skipped=[],
        state=_state(),
        cache_regenerated=[],
        reload_failures=reload_failures or [],
    )
    apply = ApplyWallpaperResult(
        wallpaper_hash="f" * 64,
        palette=_state().palette,  # type: ignore[arg-type]
        effects=None,
        icons=None,
        cache_hit_palette=True,
        cache_hit_effects=True,
        cache_hit_icons=True,
        state=_state(),
    )
    return _WallpaperSetResult(apply=apply, reconcile=reconcile)


@pytest.fixture(autouse=True)
def _quiet_seed_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))


class _CheckSpy:
    def __init__(self, result: CheckInputsResult | Exception) -> None:
        self._result = result
        self.calls = 0

    def __call__(self) -> CheckInputsResult:
        self.calls += 1
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _install(
    monkeypatch: pytest.MonkeyPatch,
    set_result: Any | Exception,
    check: _CheckSpy,
) -> None:
    def _run_set(image_path: Path) -> Any:
        if isinstance(set_result, Exception):
            raise set_result
        return set_result

    monkeypatch.setattr(cli_main, "_run_wallpaper_set", _run_set)
    monkeypatch.setattr(cli_main, "_run_check_inputs", check)


def test_success_stale_reports_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(
        CheckInputsResult(stale=frozenset({"palettes"}), fresh=frozenset({"effects", "icons"}))
    )
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])
    assert result.exit_code == 0, result.output
    assert "inputs stale: palettes (fresh: effects, icons)" in result.output
    assert check.calls == 1


def test_success_fresh_reports_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(
        CheckInputsResult(stale=frozenset(), fresh=frozenset({"palettes", "effects", "icons"}))
    )
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])
    assert result.exit_code == 0, result.output
    assert "inputs: all layers fresh" in result.output
    assert check.calls == 1


def test_reload_failure_runs_no_check(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(CheckInputsResult(stale=frozenset({"palettes"}), fresh=frozenset()))
    _install(monkeypatch, _set_result(reload_failures=["Hyprpaper"]), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])
    assert result.exit_code == 1
    assert check.calls == 0
    assert "inputs stale" not in result.output


def test_apply_failure_runs_no_check(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(CheckInputsResult(stale=frozenset({"palettes"}), fresh=frozenset()))
    _install(monkeypatch, RuntimeError("derive failed"), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])
    assert result.exit_code == 1
    assert check.calls == 0


def test_check_failure_warns_without_failing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(OSError("query adapter down"))
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])
    assert result.exit_code == 0, result.output
    assert "inputs check failed: query adapter down" in result.output
    assert check.calls == 1


def test_success_stale_object_view(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(
        CheckInputsResult(stale=frozenset({"palettes"}), fresh=frozenset({"effects", "icons"}))
    )
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png", "--format", "json"])
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output)
    assert obj["inputs_stale"] == ["palettes"]
    assert obj["inputs_fresh"] == ["effects", "icons"]
    assert obj["inputs_check_error"] is None


def test_success_fresh_object_view(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(
        CheckInputsResult(stale=frozenset(), fresh=frozenset({"palettes", "effects", "icons"}))
    )
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png", "--format", "json"])
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output)
    assert obj["inputs_stale"] == []
    assert obj["inputs_fresh"] == ["effects", "icons", "palettes"]


def test_check_failure_object_view_marks_error(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(OSError("query adapter down"))
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png", "--format", "json"])
    assert result.exit_code == 0, result.output
    obj = json.loads(result.output)
    assert "query adapter down" in obj["inputs_check_error"]
    assert obj["inputs_stale"] == []


@pytest.mark.parametrize("exc", [ValueError("bad input discovery"), RuntimeError("adapter boom")])
def test_check_value_error_and_runtime_error_warn(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    check = _CheckSpy(exc)
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])
    assert result.exit_code == 0, result.output
    assert f"inputs check failed: {exc}" in result.output


def test_check_unexpected_exception_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(KeyError("unexpected key"))
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])
    assert result.exit_code == 0, result.output
    assert "inputs check failed unexpectedly; see logs" in result.output


def test_all_stale_reports_fresh_none(monkeypatch: pytest.MonkeyPatch) -> None:
    check = _CheckSpy(
        CheckInputsResult(stale=frozenset({"palettes", "effects", "icons"}), fresh=frozenset())
    )
    _install(monkeypatch, _set_result(), check)
    result = runner.invoke(app, ["wallpaper", "set", "/img/wall.png"])
    assert result.exit_code == 0, result.output
    assert "inputs stale: effects, icons, palettes (fresh: none)" in result.output
