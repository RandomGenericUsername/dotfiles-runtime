"""AD-42/AD-44 drift gate: the history trigger enum has one source.

The pure constant `runtime.domain.history.HISTORY_TRIGGERS` is the source; the
`HistoryTrigger` Literal, the history JSON Schema's `trigger.enum` (both the
canonical file and the embedded copy the runtime enforces), and the consumers
must all agree. Executable, not prose.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path

import runtime.application.inspect as inspect_module
import runtime.application.reconcile as reconcile_module
from runtime.adapters.contract_schemas import load_history_schema
from runtime.domain.history import HISTORY_TRIGGERS, HistoryTrigger


def _schema_enum(schema: dict[str, object]) -> tuple[str, ...]:
    properties = typing.cast("dict[str, object]", schema["properties"])
    trigger = typing.cast("dict[str, object]", properties["trigger"])
    return tuple(typing.cast("list[str]", trigger["enum"]))


def test_literal_matches_constant() -> None:
    assert set(typing.get_args(HistoryTrigger)) == set(HISTORY_TRIGGERS)


def test_canonical_schema_enum_matches_constant(repo_root: Path) -> None:
    path = repo_root / "contracts" / "schemas" / "history.schema.json"
    schema = typing.cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
    assert _schema_enum(schema) == HISTORY_TRIGGERS


def test_embedded_schema_enum_matches_constant() -> None:
    # the runtime enforces THIS copy, not the canonical file
    assert _schema_enum(load_history_schema()) == HISTORY_TRIGGERS


def test_consumers_use_the_shared_symbol() -> None:
    # symbol identity, not value coincidence: a local re-definition fails here
    assert inspect_module.HistoryTrigger is HistoryTrigger
    assert reconcile_module.HISTORY_TRIGGERS is HISTORY_TRIGGERS


def test_constant_has_no_duplicates() -> None:
    assert len(HISTORY_TRIGGERS) == len(set(HISTORY_TRIGGERS))


def test_reserved_and_future_values_present() -> None:
    # force is reserved (never written); reactive is written by the daemon later
    assert "force" in HISTORY_TRIGGERS
    assert "reactive" in HISTORY_TRIGGERS
