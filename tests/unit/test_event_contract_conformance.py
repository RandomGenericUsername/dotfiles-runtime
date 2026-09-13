"""AD-44 conformance: the D-Bus wire XML is the source; the JSON agrees.

`contracts/event-contract.xml` is the interface source (parsed by GJS natively
and by Python). `contracts/event-contract.json` keeps topics/payloads/delivery
semantics; its `methods`/`signals` blocks must match the XML — checked by
executing a parser, never by reading prose.

The XML is a `/org/dotfiles` tree with two children: `Events`
(`org.dotfiles.Events1`, the hub) and `Job` (`org.dotfiles.Job1`, served by
each job and called only by the hub). Both are pinned here against the JSON.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

_HUB_INTERFACE = "org.dotfiles.Events1"
_JOB_INTERFACE = "org.dotfiles.Job1"


def _interface_named(root: ET.Element, name: str) -> ET.Element:
    for iface in root.iter("interface"):
        if iface.get("name") == name:
            return iface
    raise AssertionError(f"interface {name!r} not found in contract XML")


def _parse_xml(path: Path) -> tuple[str, dict[str, dict[str, list[str]]], dict[str, list[str]]]:
    root = ET.parse(path).getroot()
    iface = _interface_named(root, _HUB_INTERFACE)
    name = iface.get("name")
    assert name is not None

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

    signals: dict[str, list[str]] = {}
    for signal in iface.findall("signal"):
        signals[signal.get("name") or ""] = [
            f"{a.get('name')}:{a.get('type')}" for a in signal.findall("arg")
        ]
    return name, methods, signals


def _load_json(path: Path) -> dict[str, object]:
    data: object = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_event_xml_and_json_agree(repo_root: Path) -> None:
    iface, methods, signals = _parse_xml(repo_root / "contracts" / "event-contract.xml")
    data = _load_json(repo_root / "contracts" / "event-contract.json")
    assert iface == data["interface"]
    assert methods == data["methods"]
    assert signals == data["signals"]


def test_edited_xml_is_detected(repo_root: Path, tmp_path: Path) -> None:
    # A mutation (renaming a signal) must make the conformance check fail.
    original = (repo_root / "contracts" / "event-contract.xml").read_text(encoding="utf-8")
    mutated = original.replace(
        '      <signal name="JobsCleared">', '      <signal name="JobsGone">'
    )
    assert mutated != original
    path = tmp_path / "event-contract.xml"
    path.write_text(mutated, encoding="utf-8")
    _, _, signals = _parse_xml(path)
    data = _load_json(repo_root / "contracts" / "event-contract.json")
    assert signals != data["signals"]


def test_job_interface_xml_and_json_agree(repo_root: Path) -> None:
    """The Job node/interface in the XML matches the JSON `job_interface`."""
    root = ET.parse(repo_root / "contracts" / "event-contract.xml").getroot()
    job_node = root.find("node[@name='Job']")
    assert job_node is not None, "Job node missing from the contract XML"
    iface = _interface_named(job_node, _JOB_INTERFACE)
    methods: dict[str, dict[str, list[str]]] = {}
    for method in iface.findall("method"):
        methods[method.get("name") or ""] = {
            "in": [
                f"{a.get('name')}:{a.get('type')}"
                for a in method.findall("arg")
                if a.get("direction") == "in"
            ]
        }
    data = _load_json(repo_root / "contracts" / "event-contract.json")
    job = data["job_interface"]
    assert isinstance(job, dict)
    assert iface.get("name") == job["interface"] == _JOB_INTERFACE
    assert root.get("name") == "/org/dotfiles"
    assert f"{root.get('name')}/{job_node.get('name')}" == job["object_path"]
    assert methods == job["methods"]
