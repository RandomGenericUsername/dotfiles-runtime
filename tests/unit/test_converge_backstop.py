"""Last-converged backstop persistence tests (AD-36)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.converge_backstop import BACKSTOP_FILENAME, LastConvergedBackstop


def test_absent_reads_none(tmp_path: Path) -> None:
    backstop = LastConvergedBackstop(tmp_path)
    assert backstop.read() is None
    assert not (tmp_path / BACKSTOP_FILENAME).exists()


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    backstop = LastConvergedBackstop(tmp_path)
    backstop.write("ab" * 32)
    assert backstop.read() == "ab" * 32
    record = json.loads((tmp_path / BACKSTOP_FILENAME).read_text(encoding="utf-8"))
    assert record["version"] == 1
    assert record["input_hash"] == "ab" * 32
    # Additive timestamp (P5-1-4): the record carries when it converged.
    assert isinstance(record["converged_at"], str) and record["converged_at"].endswith("Z")


def test_read_record_exposes_hash_and_timestamp(tmp_path: Path) -> None:
    from runtime.adapters.converge_backstop import BackstopRecord

    backstop = LastConvergedBackstop(tmp_path)
    backstop.write("cd" * 32)
    rec = backstop.read_record()
    assert isinstance(rec, BackstopRecord)
    assert rec.input_hash == "cd" * 32
    assert rec.converged_at is not None


def test_read_record_tolerates_pre_timestamp_records(tmp_path: Path) -> None:
    """Backward compatible: a v1 record without ``converged_at`` still reads."""
    (tmp_path / BACKSTOP_FILENAME).write_text(
        json.dumps({"version": 1, "input_hash": "ee" * 32}), encoding="utf-8"
    )
    rec = LastConvergedBackstop(tmp_path).read_record()
    assert rec is not None
    assert rec.input_hash == "ee" * 32
    assert rec.converged_at is None


def test_read_record_absent_is_none(tmp_path: Path) -> None:
    assert LastConvergedBackstop(tmp_path).read_record() is None


def test_second_write_replaces(tmp_path: Path) -> None:
    backstop = LastConvergedBackstop(tmp_path)
    backstop.write("aa" * 32)
    backstop.write("bb" * 32)
    assert backstop.read() == "bb" * 32


def test_write_creates_state_root(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "state"
    LastConvergedBackstop(nested).write("cc" * 32)
    assert (nested / BACKSTOP_FILENAME).is_file()


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        "null",
        "[]",
        "123",
        '{"version": 2, "input_hash": "aa"}',
        '{"version": 1}',
        '{"version": 1, "input_hash": ""}',
        '{"version": 1, "input_hash": 5}',
        '{"input_hash": "aa"}',
    ],
)
def test_corrupt_or_wrong_shape_reads_none(tmp_path: Path, payload: str) -> None:
    (tmp_path / BACKSTOP_FILENAME).write_text(payload, encoding="utf-8")
    assert LastConvergedBackstop(tmp_path).read() is None


def test_symlink_is_refused_and_reads_none(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"version": 1, "input_hash": "aa" * 32}), encoding="utf-8")
    (tmp_path / BACKSTOP_FILENAME).symlink_to(real)
    assert LastConvergedBackstop(tmp_path).read() is None


def test_write_rejects_empty_hash(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="input_hash"):
        LastConvergedBackstop(tmp_path).write("")
