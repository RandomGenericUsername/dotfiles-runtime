"""Unit-test isolation: never spawn the real desktop.

Incident (2026-09-07): ``test_cli_crash_recovery`` invoked the CLI composition
root with only two of four reloaders stubbed; on a provisioned host the real
``AgsReloader`` fired ``ags quit && ags run`` inside a monkeypatched
``XDG_STATE_HOME`` (a pytest tmp dir). The spawned bar survived the test
session, replaced the developer's live bar, and rendered broken icons from the
deleted tmp dir. This autouse guard closes that class of leak: the real
desktop binaries resolve to "not found" for every unit test unless the test
itself patches a fake binary path (test-level monkeypatch wins by ordering).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

_REAL_DESKTOP_BINARIES = (
    "ags",
    "hyprctl",
    "hyprpaper",
    "swaybg",
    "swww",
    "mpvpaper",
)


@pytest.fixture(autouse=True)
def _no_real_desktop_binaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block system-installed desktop binaries; allow test fakes.

    ``shutil.which`` is the single binary-resolution seam used by
    ``HyprlandReloader``, ``AgsReloader``, and ``HyprpaperReloader``. The
    guard blocks only resolutions landing in the real system/user bin
    locations; tests that patch ``shutil.which`` with a fake path override
    it by ordering (test-level monkeypatch runs after the autouse fixture).
    """
    real_which = shutil.which
    system_dirs = (
        "/usr/bin",
        "/usr/local/bin",
        "/bin",
        "/sbin",
        "/usr/sbin",
        str(Path.home() / ".local" / "bin"),
    )

    def _guarded_which(name: str | None, *args: object, **kwargs: object) -> str | None:
        resolved = real_which(name, *args, **kwargs)  # type: ignore[arg-type]
        if name in _REAL_DESKTOP_BINARIES and resolved is not None:
            resolved_str = str(resolved)
            if any(
                resolved_str == prefix or resolved_str.startswith(prefix + "/")
                for prefix in system_dirs
            ):
                return None
        return resolved

    monkeypatch.setattr(shutil, "which", _guarded_which)


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Locate the repo root by walking up for the canonical schemas dir.

    Avoids a brittle hardcoded `parents[N]` depth so tests survive a move or an
    installed-only layout.
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "contracts" / "schemas" / "history.schema.json").is_file():
            return parent
    raise RuntimeError("could not locate repo root (contracts/schemas/... missing)")
