"""Hyprland reload adapter — invokes ``hyprctl reload`` (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.conf`` (through the ``~/.config/hypr`` spine symlink),
the adapter triggers ``hyprctl reload`` so Hyprland re-reads the new
palette. Failures are reported as ``False`` with warning logs; the
reconcile use case collects them into ``ReconcileResult.reload_failures``.

Binary resolution mirrors ``csg_adapter.py:135-163`` (path-separator
check → ``shutil.which`` → ``os.access`` executable verification).
Subprocess handling mirrors ``csg_adapter.py:297-319`` (``text=True``,
``capture_output=True``, ``timeout=10``, exception set
``FileNotFoundError, PermissionError, TimeoutExpired, OSError`` plus
``ValueError`` so text-mode decode/argument errors still yield ``False``).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from runtime.ports.desktop_reloader import IDesktopReloader

logger = logging.getLogger(__name__)


def _resolve_via_which(name: str) -> Path | None:
    """Resolve a command ``name`` on ``PATH`` and verify it is executable."""
    resolved = shutil.which(name)
    if resolved is None:
        return None
    try:
        return Path(resolved) if os.access(resolved, os.X_OK) else None
    except OSError:
        return None


def _resolve_hyprctl(hyprctl_path: Path | None) -> Path | None:
    """Resolve ``hyprctl`` binary path.

    When ``hyprctl_path`` is ``None``, search ``PATH`` via
    ``shutil.which`` and verify executable access. When a path is
    explicitly provided, mirror ``csg_adapter.py:135-163``: a path
    containing separators is verified as an executable file, while a
    bare name is resolved via ``shutil.which``. Any unresolvable input
    yields ``None`` for fail-later behaviour.
    """
    if hyprctl_path is not None:
        candidate = str(hyprctl_path)
        if (
            os.path.sep in candidate
            or (os.path.altsep and os.path.altsep in candidate)
            or "\\" in candidate
        ):
            if hyprctl_path.is_file():
                try:
                    return hyprctl_path if os.access(candidate, os.X_OK) else None
                except OSError:
                    return None
            return None
        return _resolve_via_which(candidate)
    return _resolve_via_which("hyprctl")


class HyprlandReloader(IDesktopReloader):
    """Adapter that reloads Hyprland via ``hyprctl reload``.

    Args:
        hyprctl_path: explicit path to ``hyprctl`` binary. When ``None``,
            resolved via ``shutil.which("hyprctl")``; if not found, the
            adapter stores ``None`` and ``reload()`` returns ``False``
            without spawning a subprocess.
    """

    def __init__(self, hyprctl_path: Path | None = None) -> None:
        self._hyprctl_path: Path | None = _resolve_hyprctl(hyprctl_path)

    def reload(self) -> bool:
        """Reload Hyprland configuration.

        Returns:
            True on ``hyprctl reload`` exit 0; False on non-zero exit,
            missing binary, timeout, or OS errors. Warnings are logged
            with stderr details where available.
        """
        if self._hyprctl_path is None:
            logger.warning("hyprctl not found in PATH; Hyprland reload skipped")
            return False
        try:
            result = subprocess.run(
                [str(self._hyprctl_path), "reload"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (
            FileNotFoundError,
            PermissionError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ) as exc:
            logger.warning("Hyprland reload failed: %s", exc)
            return False
        if result.returncode == 0:
            return True
        logger.warning("Hyprland reload failed (exit %d): %s", result.returncode, result.stderr)
        return False
