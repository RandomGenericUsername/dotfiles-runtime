"""Integration-test isolation: never spawn the real desktop.

Same guard as ``tests/unit/conftest.py`` — integration suites exercise
reloaders with fake binaries patched per-test; without this guard an
un-patched reload path could reach the developer's live session
(incident 2026-09-07: a test-spawned ``ags run`` survived pytest and
replaced the live bar with a poisoned ``XDG_STATE_HOME``).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# ── Derivation-input resolution for integration tests ──────────────────────
#
# The retired in-repo package trees (the ``cli-tools`` / ``shared`` /
# ``gui-tools`` roots under ``src``) were deleted by the cli-tools-repos split;
# the CLIs deploy from pinned repos. Integration tests used to *walk file
# ancestors* for the stale checkout under ``<root>/cli-tools/<tool>/...``, find
# nothing on a post-split host, and silently degrade (skip / tmp fallback)
# instead of failing — so the "discovery" was dead code that hid a missing
# input. These helpers resolve a real input explicitly and let the caller skip
# *loudly*:
#
#   1. an explicit env override, or
#   2. a real corpus — the pinned CLI's own bundled templates, or the live
#      checked-in dotfiles asset — and
#   3. otherwise ``None`` (the caller must ``pytest.skip`` with a clear reason).
#
# Never silently pass. Regression guard: ``tests/architecture/
# test_no_retired_sources.py`` fails if any ``src/runtime`` source reintroduces a
# retired-tree literal.
#
# Env names:
#   DOTFILES_CSG_TEMPLATES_DIR  — CSG palette templates dir (production resolves
#                                 it from the spine: ``find_templates_dir``).
#   DOTFILES_ICON_TEMPLATES_DIR — ITR icon templates dir (production resolves it
#                                 from the spine: ``find_icon_templates``).
CSG_TEMPLATES_DIR_ENV = "DOTFILES_CSG_TEMPLATES_DIR"
ICON_TEMPLATES_DIR_ENV = "DOTFILES_ICON_TEMPLATES_DIR"

#: Worktree root: ``<root>/src/runtime/tests/integration/conftest.py``.
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _env_dir(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_dir() else None


def resolve_csg_templates_dir(csg_bin: str, dest: Path) -> Path | None:
    """Resolve a CSG templates dir for a test — never via a retired repo tree.

    Production resolves the dir from the provisioning spine
    (``find_templates_dir(install_spine)``) and passes it to ``CsgAdapter``.
    A test can point at one explicitly with ``DOTFILES_CSG_TEMPLATES_DIR``;
    otherwise dump the on-PATH binary's own bundled templates (the corpus it
    actually generates from) into ``dest``. ``None`` means "not provided" —
    the caller must skip loudly.
    """
    if (env_dir := _env_dir(CSG_TEMPLATES_DIR_ENV)) is not None:
        return env_dir
    out = dest / "templates-dump"
    try:
        result = subprocess.run(
            [csg_bin, "dump-templates", "-o", str(out)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    dumped = out / "templates"
    return dumped if dumped.is_dir() else None


def resolve_icon_templates_dir() -> Path | None:
    """Resolve the ITR icon-templates dir — never via a retired repo tree.

    ITR templates are dotfiles-authored assets, not a CLI bundle. Production
    resolves them from the install spine (``find_icon_templates``); the live
    source of truth in this repo is the checked-in ``dotfiles/assets/
    icon-templates`` tree (``ARCHITECTURE.md`` marks ``dotfiles/assets/`` live),
    which is also the repo candidate the runtime consults when
    ``DOTFILES_DEV_INPUTS_ROOT`` points at a checkout (``derive.py``
    ``icon_templates``). A test can override with
    ``DOTFILES_ICON_TEMPLATES_DIR``; otherwise the checked-in asset is used.
    ``None`` means "not provided" — the caller must skip loudly.
    """
    if (env_dir := _env_dir(ICON_TEMPLATES_DIR_ENV)) is not None:
        return env_dir
    live = _REPO_ROOT / "dotfiles" / "assets" / "icon-templates"
    return live if live.is_dir() else None


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

    # KittyReloader's default pid source would `pgrep -x kitty` and SIGUSR1
    # the developer's live kitty processes. Neutralise it by default; tests
    # that exercise kitty signalling inject a pid_source/signaller.
    from runtime.adapters import kitty_reloader

    monkeypatch.setattr(kitty_reloader, "_default_pid_source", lambda: [])
