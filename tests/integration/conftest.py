"""Integration-test isolation: never spawn the real desktop.

Same guard as ``tests/unit/conftest.py`` — integration suites exercise
reloaders with fake binaries patched per-test; without this guard an
un-patched reload path could reach the developer's live session
(incident 2026-09-07: a test-spawned ``ags run`` survived pytest and
replaced the live bar with a poisoned ``XDG_STATE_HOME``).
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
    """Block system-installed desktop binaries; allow test shims.

    Shim tests place fake binaries in a pytest tmp ``bin_dir`` prepended to
    ``PATH`` and rely on real ``shutil.which`` resolving them. The guard
    therefore only blocks resolutions that land in the real system/user bin
    locations — a test shim never lives there.
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
