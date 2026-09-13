"""AD-44 conformance for the 2b-i served surface (executable, no live bus).

The contract XML is the wire source: this test parses
`contracts/event-contract.xml` and asserts the adapter's `METHODS` table
(names, arg names/types/directions) matches the methods block exactly, the
JSON `methods` table agrees, and the served `Introspect()` XML describes
exactly the served surface. Signals/topics blocks are pinned as
2b-ii-owned (present in contract, absent on the wire — asserted, not
forgotten). A jeepney import anywhere outside `adapters/` fails.
"""

from __future__ import annotations

import ast
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from runtime.adapters.dbus_event_bus import (
    INTERFACE,
    METHODS,
    OBJECT_PATH,
    HubService,
    WireError,
    introspect_xml,
)


def _served_interface(repo_root: Path) -> ET.Element:
    root = ET.parse(repo_root / "contracts" / "event-contract.xml").getroot()
    iface = root.find("interface")
    assert iface is not None
    return iface


def _xml_methods(iface: ET.Element) -> dict[str, dict[str, list[str]]]:
    methods: dict[str, dict[str, list[str]]] = {}
    for method in iface.findall("method"):
        methods[method.get("name") or ""] = {
            "in": [
                f"{a.get('name')}:{a.get('type')}"
                for a in method.findall("arg")
                if a.get("direction") == "in"
            ],
            "out": [
                f"{a.get('name')}:{a.get('type')}"
                for a in method.findall("arg")
                if a.get("direction") == "out"
            ],
        }
    return methods


def test_adapter_methods_match_contract_xml(repo_root: Path) -> None:
    """METHODS == XML methods block (names, arg names/types/directions)."""
    xml_methods = _xml_methods(_served_interface(repo_root))
    table = {
        name: {key: [f"{arg}:{sig}" for arg, sig in spec[key]] for key in ("in", "out")}
        for name, spec in METHODS.items()
    }
    for name, spec in table.items():
        assert name in xml_methods, f"{name} served but absent from contract XML"
        assert spec == xml_methods[name], f"{name}: {spec} != {xml_methods[name]}"


def test_adapter_methods_match_contract_json(repo_root: Path) -> None:
    """METHODS == JSON methods table (same names and signatures)."""
    data = json.loads((repo_root / "contracts" / "event-contract.json").read_text())
    assert isinstance(data, dict)
    json_methods = data["methods"]
    assert isinstance(json_methods, dict)
    assert set(METHODS) <= set(json_methods), "served method missing from JSON"
    for name, spec in METHODS.items():
        entry = json_methods[name]
        assert isinstance(entry, dict)
        assert [f"{a}:{s}" for a, s in spec["in"]] == list(entry.get("in", []))
        assert [f"{a}:{s}" for a, s in spec["out"]] == list(entry.get("out", []))


def test_interface_and_path_match_contract(repo_root: Path) -> None:
    data = json.loads((repo_root / "contracts" / "event-contract.json").read_text())
    assert isinstance(data, dict)
    assert INTERFACE == data["interface"] == "org.dotfiles.Events1"
    assert OBJECT_PATH == data["object_path"] == "/org/dotfiles/Events"
    xml_iface = _served_interface(repo_root)
    assert xml_iface.get("name") == INTERFACE


def _harness_service() -> object:
    """A real HubService over an empty registry (dispatch needs no bus)."""
    import itertools

    from runtime.adapters.dbus_event_bus import HubService
    from runtime.adapters.in_process_hub import InProcessJobRegistry
    from runtime.domain.hub import EventHub

    counter = itertools.count(1)
    events: list[object] = []
    return HubService(
        InProcessJobRegistry(
            EventHub(
                epoch=1,
                clock=lambda: 1000.0,
                id_factory=lambda: f"job-{next(counter)}",
                sink=events.append,  # type: ignore[arg-type]
            )
        )
    )


def test_emit_and_topic_state_pinned_as_2b_ii_owned(repo_root: Path) -> None:
    """Emit/GetTopicState are in the contract but NOT served yet (2b-ii).

    Their absence must be a loud UnknownMethod, never a silent no-op —
    pinned here so 2b-ii cannot forget them either.
    """

    xml_methods = _xml_methods(_served_interface(repo_root))
    assert "Emit" in xml_methods and "GetTopicState" in xml_methods
    assert "Emit" not in METHODS and "GetTopicState" not in METHODS
    service = _harness_service()
    assert isinstance(service, HubService)
    for member in ("Emit", "GetTopicState"):
        try:
            service.dispatch(member, ())
        except WireError as exc:
            assert exc.dbus_name == "org.freedesktop.DBus.Error.UnknownMethod"
        else:
            raise AssertionError(f"{member} unexpectedly dispatched")


def test_signals_pinned_as_2b_ii_owned(repo_root: Path) -> None:
    """All 5 contract signals exist on the wire contract only (2b-ii binds)."""
    iface = _served_interface(repo_root)
    names = {s.get("name") for s in iface.findall("signal")}
    assert names == {"JobStarted", "JobProgress", "JobFinished", "DomainEvent", "JobsCleared"}
    assert not (names & set(METHODS))


def test_introspect_xml_describes_served_surface() -> None:
    """Served Introspect() output parses and carries exactly METHODS."""
    root = ET.fromstring(introspect_xml())
    assert root.tag == "node"
    assert root.get("name") == OBJECT_PATH
    iface = root.find("interface")
    assert iface is not None and iface.get("name") == INTERFACE
    described = {m.get("name") for m in iface.findall("method")}
    assert described == set(METHODS)


def test_jeepney_import_only_in_adapters() -> None:
    """Layering: the transport import lives in adapters/ ONLY."""
    src_root = Path(__file__).resolve().parents[2] / "src" / "runtime"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        rel = path.relative_to(src_root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] == "jeepney" and not rel.startswith("adapters/"):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


def test_no_dasbus_import_anywhere() -> None:
    """dasbus proved unwirable (no gi in the hermetic venv) — jeepney won.
    Nobody may import it."""
    src_root = Path(__file__).resolve().parents[2] / "src" / "runtime"
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".")[0] == "dasbus":
                    offenders.append(str(path))
    assert offenders == []
