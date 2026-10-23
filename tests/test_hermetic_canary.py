"""Canary: the autouse fixture really pins the suite to the repo root."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_cwd_is_repo_root() -> None:
    assert Path.cwd() == REPO_ROOT
