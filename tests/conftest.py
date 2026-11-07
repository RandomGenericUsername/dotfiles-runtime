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


@pytest.fixture(autouse=True)
def _isolated_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never submit a test converge to a developer's live session daemon.

    Tests exercise the CLI fallback under ``converge.lock``; the daemon queue
    has its own in-process suite with a fake registry.
    """
    monkeypatch.setenv("DOTFILES_RUNTIME_DISABLE_DAEMON", "1")


@pytest.fixture(autouse=True)
def _isolated_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep a live host's ``desired.json`` out of the suite.

    Several CLI tests exercise composition roots that read the relocated
    intent document. Without this, a developer machine with a real
    ``$XDG_CONFIG_HOME/dotfiles/desired.json`` makes those tests converge the
    live intent instead of their fixture state. Tests that need a specific
    config home still set it themselves (their monkeypatch runs after this).
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
