"""AD-44 per-language drift gate for the event contract (P5-2, no bus).

The machine definitions are the only source of truth: XML for the D-Bus
wire, JSON for methods/signals/topics/delivery. This test executes a parser
over both and asserts every baked code constant agrees, on **both** sides of
the contract:

- the Python hub adapter's ``METHODS``/``SIGNALS`` tables (the served wire),
- the port's well-known name,
- the bar consumer binding's constants and structural caps.

The shell (GJS/AGS) world has no JS test runner (schema-evaluation §2), so
its leg is a Python textual scan of the bar sources: any ``org.dotfiles.*``
name or object path they hardcode must be a contract value. The scanner is
self-tested so it cannot silently pass a drifted literal.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from runtime.adapters import bar_subscriber as bs
from runtime.adapters.dbus_event_bus import (
    INTERFACE as HUB_INTERFACE,
)
from runtime.adapters.dbus_event_bus import (
    JOB_INTERFACE,
    JOB_METHODS,
    JOB_OBJECT_PATH,
    METHODS,
    SIGNALS,
)
from runtime.adapters.dbus_event_bus import (
    OBJECT_PATH as HUB_OBJECT_PATH,
)
from runtime.adapters.dbus_job_client import JOB_INTERFACE as CLIENT_JOB_INTERFACE
from runtime.adapters.dbus_job_client import JOB_OBJECT_PATH as CLIENT_JOB_OBJECT_PATH
from runtime.adapters.emit_validation import (
    MAX_PAYLOAD_BYTES as HUB_MAX_PAYLOAD_BYTES,
)
from runtime.adapters.emit_validation import (
    MAX_PAYLOAD_DEPTH as HUB_MAX_PAYLOAD_DEPTH,
)
from runtime.ports.bus_name_owner import BUS_NAME as PORT_BUS_NAME


def _json(repo_root: Path) -> dict[str, object]:
    data: object = json.loads(
        (repo_root / "contracts" / "event-contract.json").read_text(encoding="utf-8")
    )
    assert isinstance(data, dict)
    return data


def _xml_interface(repo_root: Path, name: str = "org.dotfiles.Events1") -> ET.Element:
    root = ET.parse(repo_root / "contracts" / "event-contract.xml").getroot()
    for iface in root.iter("interface"):
        if iface.get("name") == name:
            return iface
    raise AssertionError(f"interface {name!r} not found in contract XML")


def _xml_methods(iface: ET.Element) -> dict[str, dict[str, list[str]]]:
    """XML methods in the JSON encoding's shape (empty directions omitted)."""
    methods: dict[str, dict[str, list[str]]] = {}
    for method in iface.findall("method"):
        entry: dict[str, list[str]] = {}
        ins = [
            f"{a.get('name')}:{a.get('type')}"
            for a in method.findall("arg")
            if a.get("direction") == "in"
        ]
        outs = [
            f"{a.get('name')}:{a.get('type')}"
            for a in method.findall("arg")
            if a.get("direction") == "out"
        ]
        if ins:
            entry["in"] = ins
        if outs:
            entry["out"] = outs
        methods[method.get("name") or ""] = entry
    return methods


def _xml_signals(iface: ET.Element) -> dict[str, list[str]]:
    signals: dict[str, list[str]] = {}
    for signal in iface.findall("signal"):
        signals[signal.get("name") or ""] = [
            f"{a.get('name')}:{a.get('type')}" for a in signal.findall("arg")
        ]
    return signals


def test_contract_xml_and_json_agree(repo_root: Path) -> None:
    iface = _xml_interface(repo_root)
    data = _json(repo_root)
    assert iface.get("name") == data["interface"]
    assert _xml_methods(iface) == data["methods"]
    assert _xml_signals(iface) == data["signals"]


def test_hub_adapter_tables_match_contract(repo_root: Path) -> None:
    """The served Python tables byte-match the contract on both encodings."""
    iface = _xml_interface(repo_root)
    data = _json(repo_root)
    xml_methods = _xml_methods(iface)
    xml_signals = _xml_signals(iface)
    table_methods: dict[str, dict[str, list[str]]] = {}
    for name, spec in METHODS.items():
        entry: dict[str, list[str]] = {}
        if spec["in"]:
            entry["in"] = [f"{arg}:{sig}" for arg, sig in spec["in"]]
        if spec["out"]:
            entry["out"] = [f"{arg}:{sig}" for arg, sig in spec["out"]]
        table_methods[name] = entry
    assert table_methods == xml_methods == data["methods"]
    assert HUB_INTERFACE == data["interface"] == bs.INTERFACE
    assert HUB_OBJECT_PATH == data["object_path"] == bs.OBJECT_PATH
    table_signals = {name: [f"{arg}:{sig}" for arg, sig in args] for name, args in SIGNALS.items()}
    assert table_signals == xml_signals == data["signals"]


def test_job_control_interface_matches_contract(repo_root: Path) -> None:
    """The job-side control constants/table match XML Job node + JSON block.

    Pins the 5-4 job interface on both sides of the contract: the hub's
    ``DbusControlChannel`` constants and the job client's serving constants
    must be the same values the XML/JSON declare.
    """
    data = _json(repo_root)
    root = ET.parse(repo_root / "contracts" / "event-contract.xml").getroot()
    job_node = root.find("node[@name='Job']")
    assert job_node is not None, "Job node missing from the contract XML"
    iface = _xml_interface(repo_root, JOB_INTERFACE)
    job = data["job_interface"]
    assert isinstance(job, dict)

    assert JOB_INTERFACE == CLIENT_JOB_INTERFACE == job["interface"]
    assert JOB_OBJECT_PATH == CLIENT_JOB_OBJECT_PATH == job["object_path"]
    assert f"{root.get('name')}/{job_node.get('name')}" == job["object_path"]

    xml_methods = {
        method.get("name"): {
            "in": [
                f"{a.get('name')}:{a.get('type')}"
                for a in method.findall("arg")
                if a.get("direction") == "in"
            ]
        }
        for method in iface.findall("method")
    }
    table = {
        name: {"in": [f"{arg}:{sig}" for arg, sig in spec["in"]]}
        for name, spec in JOB_METHODS.items()
    }
    assert table == xml_methods == job["methods"]


def test_port_bus_name_matches_contract(repo_root: Path) -> None:
    assert PORT_BUS_NAME == _json(repo_root)["well_known_name"] == bs.BUS_NAME


def test_bar_constants_match_contract(repo_root: Path) -> None:
    """Every baked bar constant is derived from the machine definition."""
    data = _json(repo_root)
    iface = _xml_interface(repo_root)
    xml_signals = _xml_signals(iface)

    assert bs.BUS_NAME == data["well_known_name"]
    assert bs.OBJECT_PATH == data["object_path"]
    assert bs.INTERFACE == data["interface"]
    assert list(bs.KNOWN_TOPICS) == list(data["topics"])
    assert set(bs.BAR_TOPICS) <= set(data["topics"])
    assert bs.RESTART_SIGNAL in data["signals"]
    assert bs.DOMAIN_EVENT_SIGNAL in data["signals"]
    assert set(bs.HANDLED_SIGNALS) == {"DomainEvent", "JobsCleared"}
    assert bs.HYDRATION_METHOD in data["methods"]

    expected_args = list(data["signals"][bs.DOMAIN_EVENT_SIGNAL])
    assert [f"{arg}:{sig}" for arg, sig in bs.DOMAIN_EVENT_ARGS] == expected_args
    assert [f"{arg}:{sig}" for arg, sig in bs.DOMAIN_EVENT_ARGS] == xml_signals[
        bs.DOMAIN_EVENT_SIGNAL
    ]


def test_consumer_and_hub_structural_caps_agree() -> None:
    """The bar mirrors the hub's structural caps exactly (64 KiB / depth 8)."""
    assert bs.MAX_PAYLOAD_BYTES == HUB_MAX_PAYLOAD_BYTES == 64 * 1024
    assert bs.MAX_PAYLOAD_DEPTH == HUB_MAX_PAYLOAD_DEPTH == 8


# ── Shell-leg drift scan (no JS runner exists; F8 / schema-evaluation §2) ──

_AG_LITERAL = re.compile(r"org\.dotfiles\.Events\d*|/org/dotfiles/Events[A-Za-z0-9_]*")
_SHELL_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs"})


def _audit_shell_source(text: str) -> list[str]:
    """Return contract-name literals in shell source that are not contract values."""
    allowed = {bs.BUS_NAME, bs.INTERFACE, bs.OBJECT_PATH}
    return sorted(set(_AG_LITERAL.findall(text)) - allowed)


def test_shell_source_scanner_flags_drift_self_test() -> None:
    assert _audit_shell_source("const i = 'org.dotfiles.Events2';") == ["org.dotfiles.Events2"]
    assert _audit_shell_source("const p = '/org/dotfiles/Events1';") == ["/org/dotfiles/Events1"]
    assert (
        _audit_shell_source(
            "const b = 'org.dotfiles.Events'; const i = 'org.dotfiles.Events1';"
            " const p = '/org/dotfiles/Events';"
        )
        == []
    )


def test_shell_sources_use_only_contract_literals(repo_root: Path) -> None:
    shell_root = repo_root / "dotfiles" / "config" / "ags"
    offenders: list[str] = []
    if shell_root.is_dir():
        for path in sorted(shell_root.rglob("*")):
            if path.suffix in _SHELL_SUFFIXES:
                for bad in _audit_shell_source(path.read_text(encoding="utf-8", errors="replace")):
                    offenders.append(f"{path.relative_to(repo_root)}: {bad}")
    assert offenders == []
