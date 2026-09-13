"""Unit tests for the desired-state reader (Phase 4, Story 4.2; Phase 5 relocation)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from runtime.adapters.desired_state_reader import read_desired_state, resolve_desired_path
from runtime.domain.models import DesiredState

VALID_HASH = "a" * 64
VALID_DOC = {"version": 1, "wallpaper": "/pics/wall.jpg", "keep": 5, "pinned": [VALID_HASH]}


def _intent_path(tmp_path: Path) -> Path:
    return resolve_desired_path(tmp_path / "config")


def _write(tmp_path: Path, payload: object) -> Path:
    desired = _intent_path(tmp_path)
    desired.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        desired.write_text(payload, encoding="utf-8")
    else:
        desired.write_text(json.dumps(payload), encoding="utf-8")
    return desired


def test_resolve_desired_path_is_under_config_home_dotfiles() -> None:
    assert resolve_desired_path(Path("/cfg")) == Path("/cfg/dotfiles/desired.json")


def test_valid_file_round_trips(tmp_path: Path) -> None:
    _write(tmp_path, VALID_DOC)
    assert read_desired_state(_intent_path(tmp_path)) == DesiredState(
        wallpaper="/pics/wall.jpg", keep=5, pinned=(VALID_HASH,)
    )


def test_valid_file_empty_pinned(tmp_path: Path) -> None:
    _write(tmp_path, {**VALID_DOC, "pinned": []})
    assert read_desired_state(_intent_path(tmp_path)) == DesiredState(
        wallpaper="/pics/wall.jpg", keep=5, pinned=()
    )


def test_absent_file_returns_none_and_creates_nothing(tmp_path: Path) -> None:
    assert read_desired_state(_intent_path(tmp_path)) is None
    assert not _intent_path(tmp_path).exists()


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
        read_desired_state(_intent_path(tmp_path))


def test_symlinked_file_refused(tmp_path: Path) -> None:
    intent = _intent_path(tmp_path)
    intent.parent.mkdir(parents=True, exist_ok=True)
    real = intent.parent / "real.json"
    real.write_text(json.dumps(VALID_DOC), encoding="utf-8")
    intent.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        read_desired_state(intent)


def test_dangling_symlink_refused_not_absent(tmp_path: Path) -> None:
    intent = _intent_path(tmp_path)
    intent.parent.mkdir(parents=True, exist_ok=True)
    intent.symlink_to(intent.parent / "nonexistent.json")
    with pytest.raises(ValueError, match="symlink"):
        read_desired_state(intent)


def test_model_is_frozen() -> None:
    state = DesiredState(wallpaper="/pics/w.jpg", keep=0, pinned=())
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.keep = 9  # type: ignore[misc]
