"""Test-session invariants shared by the whole suite.

Every test runs with the repo root as its working directory, so fixture paths
such as ``tests/fixtures/wallpaper.png`` are stable no matter where the suite is
invoked from. Tests that need another directory use ``monkeypatch.chdir`` and
are restored to the repo root afterwards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _repo_root_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
