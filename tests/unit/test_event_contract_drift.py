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
import os
import re
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path

import pytest

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
from runtime.domain import contract_generated as generated
from runtime.domain.hub import KNOWN_TOPICS
from runtime.ports.bus_name_owner import BUS_NAME as PORT_BUS_NAME

# ── The canonical source: the VENDORED generated bindings ────────────────
#
# REQ-004/AD-6: the `contracts` repo is canonical and generates the bindings
# this runtime vendors (`runtime.domain.contract_generated`,
# `runtime.adapters.contract_generated.schema.json`). After the repoint the
# runtime's tables MUST equal those bindings — not the older repo-tree
# `contracts/event-contract.{xml,json}` copy, which can lag the pin (it does
# today for `wallpaper.state`'s `stage`/`reload_failures`). The vendored copies
# are kept byte-identical to `contracts/generated/` by `make contract-pin-check`,
# so pinning to them pins to the canonical contract.


def _generated_methods() -> dict[str, dict[str, list[str]]]:
    """Generated methods in the XML encoding's shape (empty directions omitted)."""
    methods: dict[str, dict[str, list[str]]] = {}
    for method in generated.EVENTS_METHODS:
        entry: dict[str, list[str]] = {}
        if method.in_args:
            entry["in"] = [f"{a.name}:{a.dbus}" for a in method.in_args]
        if method.out_args:
            entry["out"] = [f"{a.name}:{a.dbus}" for a in method.out_args]
        methods[method.name] = entry
    return methods


def _generated_signals() -> dict[str, list[str]]:
    return {
        signal.name: [f"{a.name}:{a.dbus}" for a in signal.out_args]
        for signal in generated.EVENTS_SIGNALS
    }


def _generated_topic_names() -> set[str]:
    return {topic.name for topic in generated.EVENTS_TOPICS}


def _generated_topic_schema(topic: str) -> dict[str, object]:
    """The generated ``$defs/Topic_<topic>`` definition (canonical)."""
    resource = files("runtime.adapters").joinpath("contract_generated.schema.json")
    schema = json.loads(resource.read_text(encoding="utf-8"))
    definitions = schema["$defs"]
    assert isinstance(definitions, dict)
    return definitions["Topic_" + topic.replace(".", "_")]


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


def test_hub_adapter_tables_match_generated_bindings() -> None:
    """The served Python tables derive from the vendored generated bindings.

    The canonical source is `runtime.domain.contract_generated` (vendored from
    `contracts/generated/contract.py`, pin-checked). The repo-tree
    `contracts/event-contract.{xml,json}` copy is NOT the pin source here.
    """
    table_methods: dict[str, dict[str, list[str]]] = {}
    for name, spec in METHODS.items():
        entry: dict[str, list[str]] = {}
        if spec["in"]:
            entry["in"] = [f"{arg}:{sig}" for arg, sig in spec["in"]]
        if spec["out"]:
            entry["out"] = [f"{arg}:{sig}" for arg, sig in spec["out"]]
        table_methods[name] = entry
    assert table_methods == _generated_methods()
    assert HUB_INTERFACE == generated.EVENTS_INTERFACE == bs.INTERFACE
    assert HUB_OBJECT_PATH == generated.EVENTS_OBJECT_PATH == bs.OBJECT_PATH
    table_signals = {name: [f"{arg}:{sig}" for arg, sig in args] for name, args in SIGNALS.items()}
    assert table_signals == _generated_signals()


def test_job_control_interface_matches_generated_bindings() -> None:
    """The job-side control constants/table derive from the generated bindings.

    Pins the 5-4 job interface on both sides: the hub's ``DbusControlChannel``
    constants and the job client's serving constants must be the same values the
    vendored generated bindings declare.
    """
    assert JOB_INTERFACE == CLIENT_JOB_INTERFACE == generated.JOB_INTERFACE
    assert JOB_OBJECT_PATH == CLIENT_JOB_OBJECT_PATH == generated.JOB_OBJECT_PATH

    generated_methods = {
        method.name: {"in": [f"{a.name}:{a.dbus}" for a in method.in_args]}
        for method in generated.JOB_METHODS
    }
    table = {
        name: {"in": [f"{arg}:{sig}" for arg, sig in spec["in"]]}
        for name, spec in JOB_METHODS.items()
    }
    assert table == generated_methods


def test_port_bus_name_matches_generated_bindings() -> None:
    assert PORT_BUS_NAME == generated.EVENTS_BUS_NAME == bs.BUS_NAME


def test_bar_constants_match_generated_bindings() -> None:
    """Every baked bar constant derives from the vendored generated bindings."""
    topic_names = _generated_topic_names()
    signal_names = {method.name for method in generated.EVENTS_SIGNALS}
    method_names = {method.name for method in generated.EVENTS_METHODS}

    assert bs.BUS_NAME == generated.EVENTS_BUS_NAME
    assert bs.OBJECT_PATH == generated.EVENTS_OBJECT_PATH
    assert bs.INTERFACE == generated.EVENTS_INTERFACE
    assert set(bs.KNOWN_TOPICS) == topic_names
    assert set(bs.BAR_TOPICS) <= topic_names
    assert bs.RESTART_SIGNAL in signal_names
    assert bs.DOMAIN_EVENT_SIGNAL in signal_names
    assert set(bs.HANDLED_SIGNALS) == {"DomainEvent", "JobsCleared"}
    assert bs.HYDRATION_METHOD in method_names

    assert [f"{arg}:{sig}" for arg, sig in bs.DOMAIN_EVENT_ARGS] == _generated_signals()[
        bs.DOMAIN_EVENT_SIGNAL
    ]


# ── speedtest.state pinning (add-speedtest-notifications, task 0.2) ─────
#
# The wallpaper card reads a `stage` key the contract never pinned — TS is
# AHEAD of the contract there. This block exists so the NEW keys are pinned
# BEFORE any producer/consumer code is written, and so the same class of
# drift cannot recur on `speedtest.state`. The `stage` gap is a pre-existing
# discrepancy in another capability: recorded, deliberately NOT repaired here
# (widening a change to fix another capability's contract is how changes stop
# being reviewable). `_contract_payload_keys` is the shared extractor both legs
# use, so a key added to the contract without a consumer is caught too.

SPEEDTEST_STATE_TOPIC = "speedtest.state"

#: Keys every publisher/consumer of `speedtest.state` must agree on.
SPEEDTEST_STATE_REQUIRED = {"state", "trigger"}
SPEEDTEST_STATE_ENUM_STATE = ["starting", "done", "error"]
SPEEDTEST_STATE_ENUM_TRIGGER = ["scheduled", "manual", "connect"]


#: The two CONTEXT members of a `done` payload — which network was measured and
#: which server it was measured against. Present so a consumer can name what it
#: measured rather than only that something happened. Typed as strings and
#: allowed to be EMPTY (see the contract's `optional` + notes): a wired
#: connection has no SSID and some backends report no server name, and an
#: empty string is the honest value for "unavailable" — omitting the key would
#: make "absent" indistinguishable from "the producer forgot".
SPEEDTEST_STATE_CONTEXT = {"ssid": "s", "server_name": "s"}


def test_speedtest_state_contract_pins_keys_and_enums(repo_root: Path) -> None:
    topic = _json(repo_root)["topics"][SPEEDTEST_STATE_TOPIC]
    payload = topic["payload"]
    assert SPEEDTEST_STATE_REQUIRED <= set(payload)
    assert payload["state"] == "s"
    assert payload["trigger"] == "s"
    # The `done` payload's three measurements and the `error` payload's
    # single-line reason are contract-typed, not free-form.
    assert {payload[k] for k in ("down_mbps", "up_mbps", "latency_ms")} == {"d"}
    assert payload["reason"] == "s"
    # The two context strings (R1) are pinned with their types, and listed as
    # conditionally carried rather than required on every transition.
    for key, signature in SPEEDTEST_STATE_CONTEXT.items():
        assert payload[key] == signature
    assert set(SPEEDTEST_STATE_CONTEXT) <= set(topic["optional"])
    assert list(topic["enum"]["state"]) == SPEEDTEST_STATE_ENUM_STATE
    assert list(topic["enum"]["trigger"]) == SPEEDTEST_STATE_ENUM_TRIGGER
    # The old terminal-only speed-test topic is SUPERSEDED, not extended: two
    # topics meaning "a speed test happened" is the drift this change exists to
    # avoid. The name is assembled rather than written literally so this guard
    # does not itself reintroduce the dead string into a tree-wide scan.
    superseded_speedtest_topic = "speedtest" + ".finished"
    assert superseded_speedtest_topic not in _json(repo_root)["topics"]
    assert SPEEDTEST_STATE_TOPIC in KNOWN_TOPICS
    assert SPEEDTEST_STATE_TOPIC in bs.BAR_TOPICS


def test_speedtest_state_embedded_schema_matches_generated_bindings() -> None:
    """The hub's validator schema derives from the generated topic definition."""
    from runtime.adapters.emit_validation import TOPIC_SCHEMAS

    schema = TOPIC_SCHEMAS[SPEEDTEST_STATE_TOPIC]
    expected = dict(_generated_topic_schema(SPEEDTEST_STATE_TOPIC))
    # The generated definition is closed; the hub opens it for additive fields.
    expected["additionalProperties"] = True
    assert schema == expected

    assert set(schema["required"]) == SPEEDTEST_STATE_REQUIRED
    props = schema["properties"]
    assert props["state"]["enum"] == SPEEDTEST_STATE_ENUM_STATE
    assert props["trigger"]["enum"] == SPEEDTEST_STATE_ENUM_TRIGGER
    for key in ("down_mbps", "up_mbps", "latency_ms"):
        assert props[key]["type"] == "number"
    assert props["reason"]["type"] == "string"
    # The hub must ACCEPT the two context strings, not merely tolerate them as
    # unknown additional properties: a validator missing them here would reject
    # every `done` payload the producer now sends, and the rejection would look
    # like a silent missing card rather than a schema failure.
    for key in SPEEDTEST_STATE_CONTEXT:
        assert props[key]["type"] == "string"
        assert key in props


# ── TS-ahead scan: a payload key read in TypeScript but absent from the
# contract is exactly the drift the wallpaper `stage` key already demonstrates.
# The scanner is SELF-TESTED below (a drift gate that cannot fail is worse
# than no gate), and it only applies to `speedtest.state` — extending it to
# `wallpaper.state` would force this change to repair another capability.

#: The ags-apps monorepo checkout that now hosts the notification consumer
#: (slice 4 moved src/gui-tools/notifications/ into the unit). Override with
#: $AGS_APPS_REPO; mirror of test_compositor_configs_role's pattern. The
#: checkout is external, so the scan includes it only when present.
_AGS_APPS_REPO = Path(
    os.environ.get("AGS_APPS_REPO", "/home/inumaki/Development/ags-apps")
)

_TS_SCAN_ROOTS = (
    # The in-repo bar bridge (a dev/typecheck checkout; ABSENT after the
    # ags-apps-bar-first retirement — kept so the scan still covers it if the
    # bridge reappears).
    Path("dotfiles") / "config" / "ags",
    # The notification consumer, which moved into the ags-apps unit (slice 4).
    # It reads the most `speedtest.state` keys; scanning only the bar bridge
    # would audit nothing.
    _AGS_APPS_REPO / "notifications",
)
#: A file is IN SCOPE for this scan if it names the topic literal OR reaches the
#: topic through the client module. Both are needed: the topic string lives in
#: `speedtest-events.ts`, while `NotificationsWindow.tsx` imports the client and
#: reads `payload["..."]` off the handler argument — so matching only the literal
#: would skip the very file that reads the most keys. That bug shipped in the
#: first version of this scan: it passed while covering nothing, which is
#: precisely the "passes vacuously" failure the self-test exists to prevent, so
#: the scope test below now pins BOTH routes as in-scope.
_TS_TOPIC_RE = re.compile(r"speedtest\.state|speedtest-events")
_TS_KEY_READ_RE = re.compile(r"""payload\[\s*["']([A-Za-z_][A-Za-z0-9_]*)["']\s*\]""")


#: The handler that reads `speedtest.state` payloads. A file can legitimately
#: consume SEVERAL topics: `NotificationsWindow.tsx` holds both
#: `wallpaperUpdate` and `speedtestUpdate`, and the wallpaper side reads keys
#: this leg must not judge (`stage` is the known pre-existing gap; see
#: openspec/changes/add-speedtest-notifications/known-issues.md). So the scan is
#: scoped to the SPEEDTEST handler's body, not to the file.
_TS_HANDLER_RE = re.compile(r"^(\s*)(?:export\s+)?function\s+speedtestUpdate\b")
_TS_FUNCTION_DECL_RE = re.compile(r"^(\s*)(?:export\s+)?function\s+\w+")


def _speedtest_handler_span(text: str) -> tuple[int, int] | None:
    """Line range of the `speedtestUpdate` body, or None when absent.

    Ends at the next function declared at an indentation no deeper than the
    handler's own — i.e. the next SIBLING declaration. Keying on a fixed indent
    would be wrong in both directions: a helper declared at column 0 (a
    module-level function in the same file) would not terminate the span and
    its reads would be judged as speed-test keys, while a helper declared
    deeper than the handler would truncate the span early and hide real reads.
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if _TS_HANDLER_RE.match(line)), None)
    if start is None:
        return None
    base_indent = len(_TS_HANDLER_RE.match(lines[start]).group(1))  # type: ignore[union-attr]
    for i in range(start + 1, len(lines)):
        match = _TS_FUNCTION_DECL_RE.match(lines[i])
        if match is not None and len(match.group(1)) <= base_indent:
            return start, i
    return start, len(lines)


def _audit_speedtest_state_keys(text: str, contract_keys: set[str]) -> list[str]:
    """Keys read off a speedtest payload that the contract does not pin.

    Returns ``["line N: key"]`` entries: the offending key plus its line, so a
    failure names WHERE the unpinned read is.
    """
    span = _speedtest_handler_span(text)
    if span is None:
        return []  # this file has no speedtest handler; another topic's keys are not ours
    offenders: list[str] = []
    for lineno, line in enumerate(text.splitlines()[span[0] : span[1]], start=span[0] + 1):
        for key in _TS_KEY_READ_RE.findall(line):
            if key not in contract_keys:
                offenders.append(f"line {lineno}: {key}")
    return offenders


def test_ts_speedtest_key_scanner_flags_drift_self_test() -> None:
    pinned = {"state", "trigger"}
    src = (
        "function wallpaperUpdate() {\n"
        '  payload["stage"]\n'
        "}\n"
        "function speedtestUpdate() {\n"
        '  payload["state"]\n'
        '  payload["elapsed"]\n'
        "}\n"
        "function after() {\n"
        '  payload["elsewhere"]\n'
        "}\n"
    )
    # Only the speed-test handler's reads are judged: `stage` is another
    # capability's known gap and `elsewhere` is outside every handler.
    assert _audit_speedtest_state_keys(src, pinned) == ["line 6: elapsed"]
    # A file with no speed-test handler is out of scope entirely.
    assert _audit_speedtest_state_keys('payload["elapsed"]\n', pinned) == []


def test_ts_speedtest_scan_scope_covers_the_importing_consumer(repo_root: Path) -> None:
    """The scan must reach the file that IMPORTS the client, not only the one
    holding the topic literal.

    Regression guard. `NotificationsWindow.tsx` — the only file that reads the
    speed-test payload keys — never contains `"speedtest.state"`; it imports
    the event client. A scope test keyed on the literal alone therefore audited
    nothing while reporting success, which is the worst outcome a drift gate can
    have.

    The consumer moved into the ags-apps unit (slice 4), so the guard resolves
    it from the unit checkout and SKIPS when that checkout is absent (CI), rather
    than failing on a machine without a sibling ags-apps clone.
    """
    window = _AGS_APPS_REPO / "notifications" / "ui" / "NotificationsWindow.tsx"
    if not window.is_file():
        pytest.skip(
            f"ags-apps notifications consumer not found at {window} "
            "(set AGS_APPS_REPO); the consumer moved into the unit"
        )
    text = window.read_text(encoding="utf-8")
    assert "speedtest-events" in text, "consumer no longer reaches the event client"
    assert _speedtest_handler_span(text) is not None, (
        "the speed-test handler disappeared — the scan would now audit nothing "
        "and still report success"
    )
    # The span must actually CONTAIN the handler's body, not just its
    # declaration line; a span that stopped at the first `}` would look in
    # scope while reading nothing.
    span = _speedtest_handler_span(text)
    assert span is not None
    body = "\n".join(text.splitlines()[span[0] : span[1]])
    assert 'payload["ssid"]' in body, "handler span no longer covers the payload reads"


def test_ts_speedtest_keys_are_all_pinned(repo_root: Path) -> None:
    contract_keys = set(_json(repo_root)["topics"][SPEEDTEST_STATE_TOPIC]["payload"])
    offenders: list[str] = []
    for rel in _TS_SCAN_ROOTS:
        root = rel if rel.is_absolute() else repo_root / rel
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.suffix in _SHELL_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
                for bad in _audit_speedtest_state_keys(text, contract_keys):
                    offenders.append(f"{path}: {bad}")
    assert offenders == []


def test_consumer_and_hub_structural_caps_agree() -> None:
    """The bar mirrors the hub's structural caps exactly (64 KiB / depth 8)."""
    assert bs.MAX_PAYLOAD_BYTES == HUB_MAX_PAYLOAD_BYTES == 64 * 1024
    assert bs.MAX_PAYLOAD_DEPTH == HUB_MAX_PAYLOAD_DEPTH == 8


# ── Shell-leg drift scan (no JS runner exists; F8 / schema-evaluation §2) ──

_AG_LITERAL = re.compile(r"org\.dotfiles\.Events\d*|/org/dotfiles/Events[A-Za-z0-9_]*")
_SHELL_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs"})

#: The VENDORED generated bindings (REQ-004, AD-6) ARE the contract, copied
#: verbatim into the bar tree for the future platform client. They legitimately
#: name a FUTURE interface -- `org.dotfiles.Events2` appears in the versioning
#: string -- which this scan would otherwise flag as a hand-baked consumer
#: literal. They are not consumer code, so they are excluded here; the
#: cross-repo pin gate (`make contract-pin-check`) is what pins them.
_VENDORED_GENERATED = Path("dotfiles") / "config" / "ags" / "lib" / "generated"


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
    vendored_dir = repo_root / _VENDORED_GENERATED
    offenders: list[str] = []
    if shell_root.is_dir():
        for path in sorted(shell_root.rglob("*")):
            if path.suffix not in _SHELL_SUFFIXES:
                continue
            if path.is_relative_to(vendored_dir):
                continue
            for bad in _audit_shell_source(path.read_text(encoding="utf-8", errors="replace")):
                offenders.append(f"{path.relative_to(repo_root)}: {bad}")
    assert offenders == []
