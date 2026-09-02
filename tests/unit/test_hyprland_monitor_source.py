"""Unit tests for the Hyprland monitor source adapter (real monitor detection).

Covers: active-output parsing with disabled/mirror filtering and
deterministic ordering; empty/invalid JSON → []; non-zero exit → [];
missing binary → []; subprocess error → []. Mirrors the reloader-family
unit style: a real executable ``hyprctl`` probe passed explicitly, plus
``unittest.mock.patch`` for the error path.
"""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.adapters.hyprland_monitor_source import HyprlandMonitorSource
from runtime.ports.monitor_source import IMonitorSource


def _monitor(name: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "disabled": False,
        "mirrorOf": "none",
        "width": 1920,
        "height": 1080,
    }
    entry.update(overrides)
    return entry


@pytest.fixture
def hyprctl_bin(tmp_path: Path) -> Path:
    """An executable ``hyprctl`` probe the adapter can resolve explicitly."""
    probe = tmp_path / "hyprctl"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return probe


def _make_json_hyprctl(tmp_path: Path, payload: object, exit_code: int = 0) -> Path:
    """A real ``hyprctl`` that emits ``payload`` as JSON (or exits non-zero)."""
    import json

    probe = tmp_path / "hyprctl"
    body = json.dumps(payload)
    probe.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}\nEOF\nexit {exit_code}\n")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return probe


class TestHyprlandMonitorSourceInterface:
    def test_implements_imonitor_source(self, hyprctl_bin: Path) -> None:
        assert isinstance(HyprlandMonitorSource(hyprctl_path=hyprctl_bin), IMonitorSource)


class TestHyprlandMonitorSourceDetection:
    def test_returns_active_outputs_filtered_and_sorted(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(
            tmp_path,
            [
                _monitor("HDMI-A-1"),
                _monitor("DP-3", disabled=True),
                _monitor("eDP-1"),
                _monitor("DP-2", mirrorOf="eDP-1"),
            ],
        )
        source = HyprlandMonitorSource(hyprctl_path=probe)
        assert source.detect_monitors() == ["HDMI-A-1", "eDP-1"]

    def test_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(tmp_path, "definitely not json")
        source = HyprlandMonitorSource(hyprctl_path=probe)
        assert source.detect_monitors() == []

    def test_non_list_payload_returns_empty(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(tmp_path, {"name": "eDP-1"})
        source = HyprlandMonitorSource(hyprctl_path=probe)
        assert source.detect_monitors() == []

    def test_non_zero_exit_returns_empty(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(tmp_path, [_monitor("eDP-1")], exit_code=1)
        source = HyprlandMonitorSource(hyprctl_path=probe)
        assert source.detect_monitors() == []

    def test_missing_binary_returns_empty(self, tmp_path: Path) -> None:
        missing = tmp_path / "hyprctl-missing"
        source = HyprlandMonitorSource(hyprctl_path=missing)
        assert source.detect_monitors() == []

    def test_subprocess_error_returns_empty(self, tmp_path: Path, hyprctl_bin: Path) -> None:
        source = HyprlandMonitorSource(hyprctl_path=hyprctl_bin)
        with patch(
            "runtime.adapters.hyprland_monitor_source.subprocess.run",
            side_effect=subprocess.TimeoutExpired("hyprctl", timeout=10),
        ):
            assert source.detect_monitors() == []

    def test_os_error_returns_empty(self, tmp_path: Path, hyprctl_bin: Path) -> None:
        source = HyprlandMonitorSource(hyprctl_path=hyprctl_bin)
        with patch(
            "runtime.adapters.hyprland_monitor_source.subprocess.run",
            side_effect=OSError("boom"),
        ):
            assert source.detect_monitors() == []

    def test_empty_output_list_returns_empty(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(tmp_path, [])
        source = HyprlandMonitorSource(hyprctl_path=probe)
        assert source.detect_monitors() == []
