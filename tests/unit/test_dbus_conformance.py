"""AD-44 conformance for the full 2b-ii-b served surface (no live bus).

The contract XML is the wire source: this test parses
`contracts/event-contract.xml` and asserts the adapter's `METHODS`/`SIGNALS`
tables (names, arg names/types/directions/order) match the methods and
signals blocks exactly, the JSON blocks agree, and the served
`Introspect()` XML describes exactly the served surface. The embedded
topics schema table is pinned against the JSON topics block. A jeepney
import anywhere outside `adapters/` fails.
"""

from __future__ import annotations

import ast
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from runtime.adapters.dbus_event_bus import (
    INTERFACE,
    JOB_INTERFACE,
    JOB_METHODS,
    JOB_OBJECT_PATH,
    METHODS,
    OBJECT_PATH,
    SIGNALS,
    HubService,
    introspect_xml,
    job_introspect_xml,
)


def _interface_named(repo_root: Path, name: str) -> ET.Element:
    root = ET.parse(repo_root / "contracts" / "event-contract.xml").getroot()
    for iface in root.iter("interface"):
        if iface.get("name") == name:
            return iface
    raise AssertionError(f"interface {name!r} not found in contract XML")


def _served_interface(repo_root: Path) -> ET.Element:
    return _interface_named(repo_root, INTERFACE)


def _job_interface(repo_root: Path) -> ET.Element:
    return _interface_named(repo_root, JOB_INTERFACE)


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


def _xml_signals(iface: ET.Element) -> dict[str, list[str]]:
    signals: dict[str, list[str]] = {}
    for signal in iface.findall("signal"):
        signals[signal.get("name") or ""] = [
            f"{a.get('name')}:{a.get('type')}" for a in signal.findall("arg")
        ]
    return signals


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


def test_job_interface_matches_contract_xml_json(repo_root: Path) -> None:
    """JOB_METHODS == XML Job node == JSON job_interface (5-4)."""
    iface = _job_interface(repo_root)
    data = json.loads((repo_root / "contracts" / "event-contract.json").read_text())
    assert isinstance(data, dict)
    job = data["job_interface"]
    assert isinstance(job, dict)
    assert iface.get("name") == job["interface"] == JOB_INTERFACE
    assert JOB_OBJECT_PATH == job["object_path"]
    root = ET.parse(repo_root / "contracts" / "event-contract.xml").getroot()
    assert f"{root.get('name')}/Job" == job["object_path"]
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


def test_job_introspect_xml_describes_served_job_surface() -> None:
    """The job client's served Introspect() matches JOB_METHODS at the path."""
    root = ET.fromstring(job_introspect_xml())
    assert root.tag == "node"
    assert root.get("name") == JOB_OBJECT_PATH
    iface = root.find("interface")
    assert iface is not None and iface.get("name") == JOB_INTERFACE
    assert {m.get("name") for m in iface.findall("method")} == set(JOB_METHODS)
    control = iface.find("method[@name='Control']")
    assert control is not None
    assert [
        (a.get("name"), a.get("type"), a.get("direction")) for a in control.findall("arg")
    ] == [("action", "s", "in")]


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


def test_emit_and_topic_state_served(repo_root: Path) -> None:
    """ii-a serves Emit/GetTopicState (methods block covers them now)."""
    xml_methods = _xml_methods(_served_interface(repo_root))
    assert "Emit" in xml_methods and "GetTopicState" in xml_methods
    assert "Emit" in METHODS and "GetTopicState" in METHODS

    service = _harness_service()
    assert isinstance(service, HubService)
    _, (state,) = service.dispatch("GetTopicState", ("icme.saved",))
    assert state == {"_epoch": 1, "_seq": 0}


def test_embedded_schema_table_matches_contract_topics(repo_root: Path) -> None:
    """TOPIC_SCHEMAS == JSON topics block (names, required shapes, enums)."""
    from runtime.adapters.emit_validation import TOPIC_SCHEMAS

    data = json.loads((repo_root / "contracts" / "event-contract.json").read_text())
    assert isinstance(data, dict)
    topics = data["topics"]
    assert isinstance(topics, dict)
    assert set(TOPIC_SCHEMAS) == set(topics)
    sig_to_json_type = {"s": "string", "d": "number", "x": "integer"}
    for topic, entry in topics.items():
        assert isinstance(entry, dict)
        payload = entry["payload"]
        assert isinstance(payload, dict)
        schema = TOPIC_SCHEMAS[topic]
        assert sorted(schema["required"]) == sorted(payload)
        for field, sig in payload.items():
            assert schema["properties"][field]["type"] == sig_to_json_type[sig], field
        for field, values in entry.get("enum", {}).items():
            assert schema["properties"][field]["enum"] == values, field


def test_adapter_signals_match_contract_xml(repo_root: Path) -> None:
    """SIGNALS == XML signals block (names, arg names/types, order)."""
    xml_signals = _xml_signals(_served_interface(repo_root))
    table = {name: [f"{arg}:{sig}" for arg, sig in args] for name, args in SIGNALS.items()}
    assert table == xml_signals


def test_adapter_signals_match_contract_json(repo_root: Path) -> None:
    """SIGNALS == JSON signals table (same names and signatures)."""
    data = json.loads((repo_root / "contracts" / "event-contract.json").read_text())
    assert isinstance(data, dict)
    json_signals = data["signals"]
    assert isinstance(json_signals, dict)
    assert set(SIGNALS) == set(json_signals)
    for name, args in SIGNALS.items():
        assert [f"{arg}:{sig}" for arg, sig in args] == list(json_signals[name])


def test_signals_are_bound(repo_root: Path) -> None:
    """All 5 contract signals are emitted (2b-ii-b binds them)."""
    iface = _served_interface(repo_root)
    names = {s.get("name") for s in iface.findall("signal")}
    assert names == {"JobStarted", "JobProgress", "JobFinished", "DomainEvent", "JobsCleared"}
    assert set(SIGNALS) == names
    assert not (names & set(METHODS))


def test_introspect_xml_describes_served_surface() -> None:
    """Served Introspect() output parses and carries exactly METHODS+SIGNALS."""
    root = ET.fromstring(introspect_xml())
    assert root.tag == "node"
    assert root.get("name") == OBJECT_PATH
    iface = root.find("interface")
    assert iface is not None and iface.get("name") == INTERFACE
    described = {m.get("name") for m in iface.findall("method")}
    assert described == set(METHODS)
    described_signals = {s.get("name") for s in iface.findall("signal")}
    assert described_signals == set(SIGNALS)


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
