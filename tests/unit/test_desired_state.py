"""Unit tests for the desired-state reader (Phase 4, Story 4.2)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from runtime.adapters.desired_state_reader import read_desired_state
from runtime.domain.models import DesiredState

VALID_HASH = "a" * 64
VALID_DOC = {"version": 1, "wallpaper": "/pics/wall.jpg", "keep": 5, "pinned": [VALID_HASH]}


def _write(state_root: Path, payload: object) -> Path:
    desired = state_root / "desired.json"
    if isinstance(payload, str):
        desired.write_text(payload, encoding="utf-8")
    else:
        desired.write_text(json.dumps(payload), encoding="utf-8")
    return desired


def test_valid_file_round_trips(tmp_path: Path) -> None:
    _write(tmp_path, VALID_DOC)
    assert read_desired_state(tmp_path) == DesiredState(
        wallpaper="/pics/wall.jpg", keep=5, pinned=(VALID_HASH,)
    )


def test_valid_file_empty_pinned(tmp_path: Path) -> None:
    _write(tmp_path, {**VALID_DOC, "pinned": []})
    assert read_desired_state(tmp_path) == DesiredState(
        wallpaper="/pics/wall.jpg", keep=5, pinned=()
    )


def test_absent_file_returns_none_and_creates_nothing(tmp_path: Path) -> None:
    assert read_desired_state(tmp_path) is None
    assert not (tmp_path / "desired.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        "{not json",
        {k: v for k, v in VALID_DOC.items() if k != "wallpaper"},
        {k: v for k, v in VALID_DOC.items() if k != "keep"},
        {k: v for k, v in VALID_DOC.items() if k != "pinned"},
        "null",
        "123",
        '""',
        "{}",
        "   ",
        ["not", "an", "object"],
        {**VALID_DOC, "version": 2},
        {**VALID_DOC, "version": True},
        {**VALID_DOC, "version": 1.0},
        {**VALID_DOC, "version": "1"},
        {**VALID_DOC, "version": None},
        {k: v for k, v in VALID_DOC.items() if k != "version"},
        {**VALID_DOC, "wallpaper": ""},
        {**VALID_DOC, "wallpaper": 123},
        {**VALID_DOC, "keep": -1},
        {**VALID_DOC, "keep": True},
        {**VALID_DOC, "keep": "5"},
        {**VALID_DOC, "pinned": "not-a-list"},
        {**VALID_DOC, "pinned": ["short"]},
        {**VALID_DOC, "pinned": ["A" * 64]},
        {**VALID_DOC, "pinned": [123]},
        {**VALID_DOC, "typoo": 1},
    ],
)
def test_malformations_raise_value_error(tmp_path: Path, payload: object) -> None:
    _write(tmp_path, payload)
    with pytest.raises(ValueError, match=r"desired\.json|desired state"):
        read_desired_state(tmp_path)


def test_symlinked_file_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text(json.dumps(VALID_DOC), encoding="utf-8")
    (tmp_path / "desired.json").symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        read_desired_state(tmp_path)


def test_dangling_symlink_refused_not_absent(tmp_path: Path) -> None:
    (tmp_path / "desired.json").symlink_to(tmp_path / "nonexistent.json")
    with pytest.raises(ValueError, match="symlink"):
        read_desired_state(tmp_path)


def test_model_is_frozen() -> None:
    state = DesiredState(wallpaper="/pics/w.jpg", keep=0, pinned=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.keep = 9  # type: ignore[misc]
