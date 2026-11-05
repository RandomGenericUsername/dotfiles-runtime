"""Unit tests for ``dotfiles-runtime monitors list`` (REQ-009).

Covers the full composition path (real adapter + application projection +
CLI renderer) against a fake ``hyprctl``: filtering/order/focus/geometry/
scale/transform, empty/unavailable, invalid JSON, non-zero exit, missing
binary, subprocess error, schema validation, and no state/XDG writes.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import fastjsonschema
import pytest
from cli_output.domain.enums import OutputFormat
from typer.testing import CliRunner

import runtime.adapters.hyprland_monitor_source as adapter_module
import runtime.cli.main as cli_main
from runtime.cli.main import app

runner = CliRunner()


def _monitor(name: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "description": "",
        "make": "",
        "model": "",
        "serial": "",
        "x": 0,
        "y": 0,
        "width": 1920,
        "height": 1080,
        "scale": 1.0,
        "transform": 0,
        "focused": False,
        "disabled": False,
        "mirrorOf": "none",
    }
    entry.update(overrides)
    return entry


def _write_hyprctl(tmp_path: Path, body: str, exit_code: int = 0) -> Path:
    probe = tmp_path / "hyprctl"
    probe.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}\nEOF\nexit {exit_code}\n")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return probe


@pytest.fixture
def install_hyprctl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    """Install a fake ``hyprctl`` and make the adapter resolve it."""

    def _install(payload: object, exit_code: int = 0) -> Path:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        probe = _write_hyprctl(tmp_path, body, exit_code)
        monkeypatch.setattr(adapter_module, "_resolve_hyprctl", lambda _path=None: probe)
        return probe

    return _install


def _run(*args: str) -> Any:
    return runner.invoke(app, ["monitors", "list", *args])


def _payload() -> list[dict[str, object]]:
    """Shuffled input: filtering and canonical order must be applied."""
    return [
        _monitor(
            "DP-3",
            description="Dell U2720Q",
            make="Dell",
            model="U2720Q",
            serial="SN3",
            x=1920,
            y=0,
            scale=1.25,
            transform=2,
        ),
        _monitor("HDMI-A-1", disabled=True),
        _monitor(
            "eDP-1",
            description="BOE 0x0BCA",
            make="BOE",
            model="0x0BCA",
            serial="SN1",
            x=0,
            y=0,
            scale=1.5,
            transform=1,
            focused=True,
        ),
        _monitor("DP-4", mirrorOf="eDP-1"),
        _monitor("DP-2", x=0, y=1080),
    ]


class TestMonitorsListJson:
    def test_filters_disabled_and_mirrored_and_orders_by_xy_connector(
        self, install_hyprctl: Any
    ) -> None:
        install_hyprctl(_payload())
        result = _run("--format", "json")

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["source"] == "hyprland"
        assert [m["connector"] for m in payload["monitors"]] == ["eDP-1", "DP-2", "DP-3"]
        assert all(m["mirror_of"] is None for m in payload["monitors"])

    def test_projects_geometry_scale_transform_focus_and_metadata(
        self, install_hyprctl: Any
    ) -> None:
        install_hyprctl(_payload())
        result = _run("--format", "json")

        assert result.exit_code == 0
        edp = json.loads(result.stdout)["monitors"][0]
        assert edp == {
            "connector": "eDP-1",
            "label": "BOE 0x0BCA",
            "description": "BOE 0x0BCA",
            "make": "BOE",
            "model": "0x0BCA",
            "serial": "SN1",
            "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
            "scale": 1.5,
            "transform": 1,
            "focused": True,
            "mirror_of": None,
        }

    def test_label_falls_back_to_connector_when_description_empty(
        self, install_hyprctl: Any
    ) -> None:
        install_hyprctl(_payload())
        result = _run("--format", "json")

        assert result.exit_code == 0
        by_connector = {m["connector"]: m for m in json.loads(result.stdout)["monitors"]}
        assert by_connector["DP-2"]["label"] == "DP-2"
        assert by_connector["DP-2"]["description"] == ""

    def test_connector_tie_break_at_equal_geometry(self, install_hyprctl: Any) -> None:
        install_hyprctl([_monitor("DP-2"), _monitor("DP-1"), _monitor("eDP-1")])
        result = _run("--format", "json")

        assert result.exit_code == 0
        assert [m["connector"] for m in json.loads(result.stdout)["monitors"]] == [
            "DP-1",
            "DP-2",
            "eDP-1",
        ]

    def test_schema_validates_real_command_output(
        self, install_hyprctl: Any, repo_root: Path
    ) -> None:
        install_hyprctl(_payload())
        result = _run("--format", "json")

        assert result.exit_code == 0
        schema = json.loads(
            (repo_root / "contracts" / "schemas" / "monitors-list.schema.json").read_text(
                encoding="utf-8"
            )
        )
        fastjsonschema.compile(schema)(json.loads(result.stdout))

    def test_schema_validates_unavailable_output(
        self, install_hyprctl: Any, repo_root: Path
    ) -> None:
        install_hyprctl([])
        result = _run("--format", "json")

        assert result.exit_code == 0
        schema = json.loads(
            (repo_root / "contracts" / "schemas" / "monitors-list.schema.json").read_text(
                encoding="utf-8"
            )
        )
        fastjsonschema.compile(schema)(json.loads(result.stdout))


class TestMonitorsListUnavailable:
    def test_empty_payload_exits_zero_unavailable(self, install_hyprctl: Any) -> None:
        install_hyprctl([])
        result = _run("--format", "json")

        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "schema_version": 1,
            "source": "unavailable",
            "monitors": [],
        }

    def test_invalid_json_exits_zero_unavailable(self, install_hyprctl: Any) -> None:
        install_hyprctl("definitely not json")
        result = _run("--format", "json")

        assert result.exit_code == 0
        assert json.loads(result.stdout)["source"] == "unavailable"

    def test_non_zero_exit_exits_zero_unavailable(self, install_hyprctl: Any) -> None:
        install_hyprctl([_monitor("eDP-1")], exit_code=1)
        result = _run("--format", "json")

        assert result.exit_code == 0
        assert json.loads(result.stdout)["source"] == "unavailable"

    def test_missing_binary_exits_zero_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(adapter_module, "_resolve_hyprctl", lambda _path=None: None)
        result = _run("--format", "json")

        assert result.exit_code == 0
        assert json.loads(result.stdout)["source"] == "unavailable"

    def test_subprocess_error_exits_zero_unavailable(
        self, install_hyprctl: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        install_hyprctl([_monitor("eDP-1")])
        with patch(
            "runtime.adapters.hyprland_monitor_source.subprocess.run",
            side_effect=subprocess.TimeoutExpired("hyprctl", timeout=10),
        ):
            result = _run("--format", "json")

        assert result.exit_code == 0
        assert json.loads(result.stdout)["source"] == "unavailable"

    def test_plain_output_for_unavailable(self, install_hyprctl: Any) -> None:
        install_hyprctl([])
        result = _run()

        assert result.exit_code == 0
        assert "no monitors available" in result.stdout


class TestMonitorsListReadOnly:
    def test_no_state_or_xdg_writes(
        self, install_hyprctl: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        install_hyprctl([_monitor("eDP-1")])
        state = tmp_path / "state"
        config = tmp_path / "config"
        data = tmp_path / "data"
        cache = tmp_path / "cache"
        monkeypatch.setenv("XDG_STATE_HOME", str(state))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
        monkeypatch.setenv("XDG_DATA_HOME", str(data))
        monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(tmp_path / "install"))

        result = _run("--format", "json")

        assert result.exit_code == 0
        assert not state.exists()
        assert not config.exists()
        assert not data.exists()
        assert not cache.exists()

    def test_monitors_never_auto_seeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        monkeypatch.setattr(cli_main, "_run_seed_if_needed", lambda: calls.append("seed"))

        cli_main.main_callback(
            ctx=SimpleNamespace(invoked_subcommand="monitors"),
            output_format=OutputFormat.PLAIN,
        )

        assert calls == []

    def test_unexpected_composition_error_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> dict[str, object]:
            raise KeyError("surprise")

        monkeypatch.setattr(cli_main, "_run_monitors_list", _boom)
        result = _run("--format", "json")

        assert result.exit_code == 1
        assert "unexpectedly" in result.stderr
