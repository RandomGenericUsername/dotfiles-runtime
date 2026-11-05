"""Unit tests for the Hyprland monitor catalog adapter and pure projection.

The existing ``detect_monitors()`` suite (``test_hyprland_monitor_source.py``)
stays untouched. This file covers the REQ-009 sibling surface:
``IMonitorCatalog.list_monitors()`` plus the application filter/order
projection.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.adapters.hyprland_monitor_source import HyprlandMonitorSource
from runtime.application.monitors import active_monitors, catalog_payload, monitor_to_dict
from runtime.ports.monitor_catalog import IMonitorCatalog, MonitorGeometry, MonitorInfo


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


def _make_json_hyprctl(tmp_path: Path, payload: object, exit_code: int = 0) -> Path:
    probe = tmp_path / "hyprctl"
    body = json.dumps(payload)
    probe.write_text(f"#!/bin/sh\ncat <<'EOF'\n{body}\nEOF\nexit {exit_code}\n")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return probe


def _info(
    connector: str,
    *,
    x: int = 0,
    y: int = 0,
    disabled: bool = False,
    mirror_of: str | None = None,
    focused: bool = False,
) -> MonitorInfo:
    return MonitorInfo(
        connector=connector,
        label=connector,
        description="",
        make="",
        model="",
        serial="",
        geometry=MonitorGeometry(x=x, y=y, width=1920, height=1080),
        scale=1.0,
        transform=0,
        focused=focused,
        mirror_of=mirror_of,
        disabled=disabled,
    )


class TestHyprlandMonitorCatalogInterface:
    def test_implements_imonitor_catalog(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(tmp_path, [])
        assert isinstance(HyprlandMonitorSource(hyprctl_path=probe), IMonitorCatalog)


class TestHyprlandMonitorCatalogParsing:
    def test_parses_full_payload_including_disabled_and_mirrored(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(
            tmp_path,
            [
                _monitor(
                    "eDP-1",
                    description="BOE 0x0BCA",
                    make="BOE",
                    model="0x0BCA",
                    serial="SN1",
                    x=0,
                    y=0,
                    width=1920,
                    height=1080,
                    scale=1.5,
                    transform=1,
                    focused=True,
                ),
                _monitor("HDMI-A-1", disabled=True),
                _monitor("DP-4", mirrorOf="eDP-1"),
            ],
        )
        monitors = HyprlandMonitorSource(hyprctl_path=probe).list_monitors()

        assert [m.connector for m in monitors] == ["eDP-1", "HDMI-A-1", "DP-4"]
        edp = monitors[0]
        assert edp.label == "BOE 0x0BCA"
        assert edp.description == "BOE 0x0BCA"
        assert edp.make == "BOE"
        assert edp.model == "0x0BCA"
        assert edp.serial == "SN1"
        assert edp.geometry == MonitorGeometry(x=0, y=0, width=1920, height=1080)
        assert edp.scale == 1.5
        assert edp.transform == 1
        assert edp.focused is True
        assert edp.mirror_of is None
        assert edp.disabled is False
        assert monitors[1].disabled is True
        assert monitors[2].mirror_of == "eDP-1"

    def test_label_falls_back_to_connector_when_description_empty(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(tmp_path, [_monitor("DP-2", description="")])
        assert HyprlandMonitorSource(hyprctl_path=probe).list_monitors()[0].label == "DP-2"

    def test_invalid_json_returns_empty_catalog(self, tmp_path: Path) -> None:
        probe = tmp_path / "hyprctl"
        probe.write_text("#!/bin/sh\nprintf 'not json'\n")
        probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
        assert HyprlandMonitorSource(hyprctl_path=probe).list_monitors() == []

    def test_non_zero_exit_returns_empty_catalog(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(tmp_path, [_monitor("eDP-1")], exit_code=1)
        assert HyprlandMonitorSource(hyprctl_path=probe).list_monitors() == []

    def test_missing_binary_returns_empty_catalog(self, tmp_path: Path) -> None:
        missing = tmp_path / "hyprctl-missing"
        assert HyprlandMonitorSource(hyprctl_path=missing).list_monitors() == []

    def test_subprocess_error_returns_empty_catalog(self, tmp_path: Path) -> None:
        probe = _make_json_hyprctl(tmp_path, [_monitor("eDP-1")])
        source = HyprlandMonitorSource(hyprctl_path=probe)
        with patch(
            "runtime.adapters.hyprland_monitor_source.subprocess.run",
            side_effect=subprocess.TimeoutExpired("hyprctl", timeout=10),
        ):
            assert source.list_monitors() == []


class TestMonitorCatalogProjection:
    def test_filters_disabled_and_mirrored(self) -> None:
        active = active_monitors(
            [
                _info("eDP-1"),
                _info("HDMI-A-1", disabled=True),
                _info("DP-4", mirror_of="eDP-1"),
            ]
        )
        assert [m.connector for m in active] == ["eDP-1"]

    def test_orders_by_x_then_y_then_connector(self) -> None:
        active = active_monitors(
            [
                _info("DP-3", x=1920, y=0),
                _info("eDP-1", x=0, y=0),
                _info("DP-2", x=0, y=1080),
                _info("DP-1", x=0, y=0),
            ]
        )
        assert [m.connector for m in active] == ["DP-1", "eDP-1", "DP-2", "DP-3"]

    def test_payload_shape_and_unavailable_degradation(self) -> None:
        payload = catalog_payload([_info("eDP-1", focused=True)])
        assert payload["schema_version"] == 1
        assert payload["source"] == "hyprland"
        assert payload["monitors"] == [
            {
                "connector": "eDP-1",
                "label": "eDP-1",
                "description": "",
                "make": "",
                "model": "",
                "serial": "",
                "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                "scale": 1.0,
                "transform": 0,
                "focused": True,
                "mirror_of": None,
            }
        ]

        unavailable = catalog_payload([_info("HDMI-A-1", disabled=True)])
        assert unavailable == {
            "schema_version": 1,
            "source": "unavailable",
            "monitors": [],
        }

    def test_monitor_to_dict_omits_internal_disabled_flag(self) -> None:
        serialized = monitor_to_dict(_info("eDP-1", disabled=True))
        assert "disabled" not in serialized
        assert serialized["connector"] == "eDP-1"


@pytest.mark.parametrize("payload", [None, {}])
def test_non_list_payload_returns_empty_catalog(tmp_path: Path, payload: object) -> None:
    probe = _make_json_hyprctl(tmp_path, payload)
    assert HyprlandMonitorSource(hyprctl_path=probe).list_monitors() == []
