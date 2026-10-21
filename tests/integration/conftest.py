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
#   1. the PROVISIONED spine (``$DOTFILES_INSTALL_SPINE`` or
#      ``$XDG_DATA_HOME/dotfiles``) — the production source (AD-13), or
#   2. an explicit env override (an assets checkout), or
#   3. a real corpus — the pinned CLI's own bundled templates — and
#   4. otherwise ``None`` (the caller must ``pytest.skip`` with a clear reason).
#
# Never silently pass. Regression guard: ``tests/architecture/
# test_no_retired_sources.py`` fails if any ``src/runtime`` source reintroduces a
# retired-tree literal.
#
# Env names:
#   DOTFILES_CSG_TEMPLATES_DIR  — CSG palette templates dir (production resolves
#                                 it from the spine: ``find_templates_dir``).
#   DOTFILES_ICON_TEMPLATES_DIR — ITR icon templates dir, an assets checkout
#                                 override (production resolves it from the
#                                 spine: ``find_icon_templates``).
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


def _resolve_install_spine() -> Path | None:
    """Resolve the provisioned install spine exactly as the runtime CLI does.

    ``$DOTFILES_INSTALL_SPINE`` (explicit override) else
    ``$XDG_DATA_HOME/dotfiles`` (default ``~/.local/share/dotfiles``). ``None``
    when the directory does not exist on this machine.
    """
    explicit = os.environ.get("DOTFILES_INSTALL_SPINE")
    if explicit:
        candidate = Path(explicit).expanduser()
    else:
        xdg_data = Path(
            os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
        )
        candidate = xdg_data / "dotfiles"
    return candidate.resolve() if candidate.is_dir() else None


def resolve_icon_templates_dir() -> Path | None:
    """Resolve the ITR icon-templates dir — spine first, then an assets checkout.

    ITR templates are assets: since S1 they ship in the pinned ``dotfiles-assets``
    repo and deploy to ``<install>/icon-templates``. Production reads the spine
    (``find_icon_templates``); a test may point at an assets checkout with
    ``DOTFILES_ICON_TEMPLATES_DIR``. ``None`` means "not provided" — the caller
    must skip loudly, never silently fall back to a retired repo tree.
    """
    spine = _resolve_install_spine()
    if spine is not None and (spine / "icon-templates").is_dir():
        return spine / "icon-templates"
    if (env_dir := _env_dir(ICON_TEMPLATES_DIR_ENV)) is not None:
        return env_dir
    return None


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
