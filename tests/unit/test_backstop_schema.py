"""AD-44 machine contract for the persisted last-converged backstop record.

The canonical schema is ``contracts/schemas/last-converged.schema.json`` (embedded
byte-identical under ``runtime/adapters/schemas/``). The reader enforces it via
``fastjsonschema`` but keeps the AD-36 "treat as changed" policy: a malformed
record is logged and read as ``None`` (never fatal). The compiled validator, by
contrast, fails loud when called directly on a violation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import fastjsonschema
import pytest

from runtime.adapters.converge_backstop import (
    BACKSTOP_FILENAME,
    BackstopRecord,
    LastConvergedBackstop,
    last_converged_validator,
)

_LOGGER = "runtime.adapters.converge_backstop"


def test_valid_record_round_trips(tmp_path: Path) -> None:
    backstop = LastConvergedBackstop(tmp_path)
    backstop.write("ab" * 32)

    raw = json.loads((tmp_path / BACKSTOP_FILENAME).read_text(encoding="utf-8"))
    last_converged_validator()(raw)

    assert backstop.read_record() == BackstopRecord("ab" * 32, raw["converged_at"])


def test_pre_timestamp_record_is_valid(tmp_path: Path) -> None:
    """Backward compatible: ``converged_at`` is optional on the v1 record."""
    record = {"version": 1, "input_hash": "ee" * 32}
    last_converged_validator()(record)
    (tmp_path / BACKSTOP_FILENAME).write_text(json.dumps(record), encoding="utf-8")
    assert LastConvergedBackstop(tmp_path).read_record() == BackstopRecord("ee" * 32, None)


@pytest.mark.parametrize(
    "record",
    [
        {"version": 2, "input_hash": "aa" * 32},
        {"input_hash": "aa" * 32},
        {"version": 1},
        {"version": 1, "input_hash": ""},
        {"version": 1, "input_hash": 5},
        {"version": 1, "input_hash": "aa" * 32, "converged_at": 5},
        {"version": 1, "input_hash": "aa" * 32, "extra": True},
    ],
)
def test_reader_treats_malformed_record_as_changed_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, record: dict[str, object]
) -> None:
    (tmp_path / BACKSTOP_FILENAME).write_text(json.dumps(record), encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert LastConvergedBackstop(tmp_path).read_record() is None
    assert any("treating as changed" in message for message in caplog.messages)


def test_reader_treats_corrupt_json_as_changed(tmp_path: Path) -> None:
    (tmp_path / BACKSTOP_FILENAME).write_text("{not json", encoding="utf-8")
    assert LastConvergedBackstop(tmp_path).read_record() is None


@pytest.mark.parametrize(
    "record",
    [
        {"version": 2, "input_hash": "aa" * 32},
        {"version": 1},
        {"version": 1, "input_hash": ""},
        {"version": 1, "input_hash": "aa" * 32, "converged_at": 5},
    ],
)
def test_validator_fails_loud_on_violation(record: dict[str, object]) -> None:
    with pytest.raises(fastjsonschema.JsonSchemaValueException):
        last_converged_validator()(record)
