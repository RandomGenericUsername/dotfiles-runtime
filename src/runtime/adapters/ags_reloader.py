"""AGS restart reload adapter — restarts the AGS bar after a swap (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.gtk.css`` (through the ``~/.config/ags`` spine symlink to
``colors.css``, applied at runtime via ``app.apply_css``), the adapter
restarts the AGS process. AGS has NO native hot-reload (verified in the
AGS source, ``cli/cmd/run.go:145`` ``// TODO: watch and restart``
[ARCHITECTURE-SPINE.md:236]), so a restart is the only reload channel.

Restart = three steps: ``ags quit`` (tolerated failure — a fresh instance
is the goal, not the death of the old one), a detached ``ags run``
(``Popen`` + ``start_new_session=True`` — the bar is a long-running
foreground process and must never block ``reconcile``), then a liveness
poll within a ≤ 2s grace window. Any failure after quit is reported as
``False`` with a warning log; the reconcile use case collects the class
name into ``ReconcileResult.reload_failures`` via the port — mirroring
``HyprlandReloader``. Missing ``ags`` in PATH is a surfaced failure, not a
skip (spec-literal R5; same decision as the Hyprland adapter).

Binary resolution reuses ``_resolve_via_which`` from ``hyprland_reloader``
(adapters→adapters import — layering-green) so the separator/which/access
branch logic is not forked a second time.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from runtime.adapters.hyprland_reloader import _resolve_via_which
from runtime.ports.desktop_reloader import IDesktopReloader

logger = logging.getLogger(__name__)

_LIVENESS_POLLS = 8
_LIVENESS_POLL_INTERVAL = 0.25


def _resolve_ags(ags_path: Path | None) -> Path | None:
    """Resolve the ``ags`` binary path.

    When ``ags_path`` is ``None``, search ``PATH`` via ``shutil.which``
    and verify executable access. When a path is explicitly provided,
    mirror ``hyprland_reloader._resolve_hyprctl``'s branch structure: a
    path containing separators is verified as an executable file, while a
    bare name is resolved via ``shutil.which``. Any unresolvable input
    yields ``None`` for fail-later behaviour.
    """
    if ags_path is not None:
        candidate = str(ags_path)
        if (
            os.path.sep in candidate
            or (os.path.altsep and os.path.altsep in candidate)
            or "\\" in candidate
        ):
            if ags_path.is_file():
                try:
                    return ags_path if os.access(candidate, os.X_OK) else None
                except OSError:
                    return None
            return None
        return _resolve_via_which(candidate)
    return _resolve_via_which("ags")


class AgsReloader(IDesktopReloader):
    """Adapter that restarts the AGS process (quit → detached run → liveness).

    Args:
        ags_path: explicit path to the ``ags`` binary. When ``None``,
            resolved via ``shutil.which("ags")``; if not found, the
            adapter stores ``None`` and ``reload()`` returns ``False``
            without spawning a subprocess (surfaced failure, not a skip).
    """

    def __init__(self, ags_path: Path | None = None) -> None:
        self._ags_path: Path | None = _resolve_ags(ags_path)

    def reload(self) -> bool:
        """Restart the AGS bar so it re-reads the palette fragment.

        Returns:
            True when the detached ``ags run`` process is still alive at
            the end of the ≤ 2s liveness window; False on a missing
            binary, a ``run`` spawn/verify failure, or any subprocess
            error after quit. ``ags quit`` failure is tolerated and
            logged, because a fresh instance is the goal.
        """
        if self._ags_path is None:
            logger.warning("ags not found in PATH; AGS reload skipped")
            return False

        # Step A — quit the running instance. Tolerate failure: non-zero
        # exit or an exception only means no live instance (or the quit
        # channel failed); continuing toward a fresh instance is correct.
        try:
            subprocess.run(
                [str(self._ags_path), "quit"],
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
            logger.debug("ags quit failed; continuing with restart: %s", exc)

        # Step B — spawn a DETACHED `ags run` (never a blocking run():
        # `ags run` IS the bar; it would hang reconcile forever).
        # Step C — liveness verification within a ≤ 2s grace window.
        try:
            proc = subprocess.Popen(
                [str(self._ags_path), "run"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(_LIVENESS_POLLS):
                if proc.poll() is not None:
                    logger.warning(
                        "AGS restart failed: process exited with code %s",
                        proc.returncode,
                    )
                    return False
                time.sleep(_LIVENESS_POLL_INTERVAL)
            return True
        except (
            FileNotFoundError,
            PermissionError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ) as exc:
            logger.warning("AGS reload failed: %s", exc)
            return False
