"""REQ-004 composition-root coverage: config wins over stale selector env.

Covers the three operator-visible surfaces — ``wallpaper set`` JSON,
``inspect producer``, and the daemon/reconcile composition — plus a real
subprocess launched with a selector-shaped stale environment.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import runtime.cli.main as cli_main
from runtime.adapters.tpr_adapter import TprAdapter
from runtime.application.palette_producer import producer_config_path, tpr_templates_dir
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
TS = "2026-10-29T00:00:00Z"
WP = "a" * 64
PH = "b" * 64


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _write_config(spine: Path) -> Path:
    path = producer_config_path(spine)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "producer": "tpr",
                "tpr_engine": "native",
                "tpr_runtime": "container",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _spine(tmp_path: Path) -> Path:
    spine = tmp_path / "install"
    csg = spine / "config" / "color-scheme-generator" / "templates"
    csg.mkdir(parents=True)
    (csg / "default.yaml").write_text("window: {}\n", encoding="utf-8")
    tpr = tpr_templates_dir(spine)
    tpr.mkdir(parents=True)
    (tpr / "colors.yaml.j2").write_text("background: {{ bg }}\n", encoding="utf-8")
    return spine


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    spine = _spine(tmp_path)
    monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(spine))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    return spine


def _palette() -> PaletteEntry:
    return PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash=PH,
        source_wallpaper_hash=WP,
        input_template_hash="c" * 64,
        artifact_hashes=PaletteArtifacts(
            colors_yaml="1" * 64,
            colors_conf="2" * 64,
            colors_gtk_css="3" * 64,
            colors_adw_css="4" * 64,
            colors_sequences="5" * 64,
            colors_rasi="6" * 64,
            colors_kitty="7" * 64,
            colors_btop="8" * 64,
        ),
        generated_at=TS,
    )


def _state() -> DesktopState:
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=WP,
            source_path="/img/wall.png",
            imported_at=TS,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=WP,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=_palette(),
        effects=None,
        icons=None,
        applied_at=TS,
    )


def test_inspect_producer_json_reports_config_source_and_ignored_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spine = _isolate(monkeypatch, tmp_path)
    config = _write_config(spine)
    monkeypatch.setenv("RUNTIME__PALETTE__PRODUCER", "csg")
    monkeypatch.setenv("RUNTIME__PALETTE__TPR_ENGINE", "matugen")

    result = runner.invoke(app=cli_main.app, args=["inspect", "producer", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["producer"] == "tpr"
    assert payload["source"] == "config"
    assert payload["engine"] == "native"
    assert payload["runtime"] == "container"
    assert payload["config_path"] == str(config)
    assert payload["ambient_env"] == {
        "RUNTIME__PALETTE__PRODUCER": "csg",
        "RUNTIME__PALETTE__TPR_ENGINE": "matugen",
    }
    assert set(payload["ignored_env"]) == {
        "RUNTIME__PALETTE__PRODUCER",
        "RUNTIME__PALETTE__TPR_ENGINE",
    }
    assert payload["warnings"]


def test_selector_shaped_stale_env_subprocess_applies_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real CLI process with a stale selector env still reports/configures tpr."""
    spine = _isolate(monkeypatch, tmp_path)
    _write_config(spine)
    env = dict(os.environ)
    env["RUNTIME__PALETTE__PRODUCER"] = "csg"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(Path(__file__).resolve().parents[2] / "src"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    completed = subprocess.run(
        [sys.executable, "-m", "runtime.cli.main", "inspect", "producer", "--format", "json"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["producer"] == "tpr"
    assert payload["source"] == "config"
    assert payload["ignored_env"]["RUNTIME__PALETTE__PRODUCER"] == "csg"


def test_daemon_reconcile_composition_uses_config_tpr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spine = _isolate(monkeypatch, tmp_path)
    _write_config(spine)
    monkeypatch.setenv("RUNTIME__PALETTE__PRODUCER", "csg")
    captured: dict[str, Any] = {}

    class _FakeReconcile:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def run(self) -> object:
            return "reconciled"

    monkeypatch.setattr(
        "runtime.application.reconcile.ReconcileDesktopStateUseCase", _FakeReconcile
    )
    monkeypatch.setattr(cli_main, "_build_reloaders", lambda *a, **k: [])
    monkeypatch.setattr(cli_main, "_build_wallpaper_applier", lambda *a, **k: object())
    monkeypatch.setattr(cli_main, "_build_app_refresher", lambda *a, **k: None)

    assert cli_main._run_reconcile() == "reconciled"
    producer = captured["csg"]
    assert isinstance(producer, TprAdapter)
    assert producer.runtime == "container"
    assert producer.engine == "native"


def test_wallpaper_set_json_includes_resolved_producer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    spine = _isolate(monkeypatch, tmp_path)
    _write_config(spine)
    monkeypatch.setenv("RUNTIME__PALETTE__PRODUCER", "csg")
    image = tmp_path / "wall.png"
    image.write_bytes(b"not a real image")

    from runtime.application.apply_wallpaper import (
        ApplyWallpaperResult,
        PreparedPalette,
    )
    from runtime.application.reconcile import ReconcileResult
    from runtime.application.swap_visible import SwapVisibleResult

    class _FakeApply:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def prepare_palette(self, image_path: Path) -> PreparedPalette:
            return PreparedPalette(wallpaper_hash=WP, palette=_palette(), cache_hit=False)

        def run(self, image_path: Path, **kwargs: Any) -> ApplyWallpaperResult:
            return ApplyWallpaperResult(
                wallpaper_hash=WP,
                palette=_palette(),
                effects=None,
                icons=None,
                cache_hit_palette=False,
                cache_hit_effects=False,
                cache_hit_icons=False,
                state=_state(),
            )

    class _FakeSwap:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, image_path: Path, **kwargs: Any) -> SwapVisibleResult:
            return SwapVisibleResult(
                wallpaper_hash=WP,
                wallpaper_path=tmp_path / "cache" / WP / "wallpaper.png",
                repointed=[],
                state=_state(),
            )

    class _FakeReconcile:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def run(self, **kwargs: Any) -> ReconcileResult:
            return ReconcileResult(
                repointed=[],
                skipped=[],
                state=_state(),
                cache_regenerated=[],
                reload_failures=[],
            )

    monkeypatch.setattr("runtime.application.apply_wallpaper.ApplyWallpaperUseCase", _FakeApply)
    monkeypatch.setattr("runtime.application.swap_visible.SwapVisibleUseCase", _FakeSwap)
    monkeypatch.setattr(
        "runtime.application.reconcile.ReconcileDesktopStateUseCase", _FakeReconcile
    )
    monkeypatch.setattr(cli_main, "_build_wallpaper_applier", lambda *a, **k: object())
    monkeypatch.setattr(cli_main, "_build_reloaders", lambda *a, **k: [])
    monkeypatch.setattr(cli_main, "_build_app_refresher", lambda *a, **k: None)
    monkeypatch.setattr(cli_main, "_append_inputs_check", lambda summary, obj: summary)

    result = cli_main._run_wallpaper_set(image, client=type("C", (), {"publish": lambda *a: None})())

    assert result.producer == "tpr"
    assert result.producer_source == "config"
    assert result.producer_engine == "native"

    rendered = runner.invoke(
        cli_main.app, ["wallpaper", "set", str(image), "--format", "json"]
    )
    # The second invocation re-runs composition; only assert the shape once the
    # fake classes are still patched (they are monkeypatched for this test).
    assert rendered.exit_code == 0, rendered.output
    payload = json.loads(rendered.output)
    assert payload["producer"] == "tpr"
    assert payload["producer_source"] == "config"
    assert payload["producer_engine"] == "native"
