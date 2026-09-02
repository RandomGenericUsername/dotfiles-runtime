"""Hyprland monitor source adapter — enumerates active outputs via ``hyprctl monitors -j``.

Implements ``IMonitorSource``: discovers the compositor's real output
names so wallpaper targeting stops depending on the Phase-2 ``DP-1``
stub. Replaces the previous hardcoded-default monitor for seeding and
apply (AD-17 consumer wiring; the ``DEFAULT_MONITOR`` constant remains
only as a fallback when detection is unavailable).

Detection contract:
- Runs ``hyprctl monitors -j`` (verified v0.56.x contract: JSON array of
  output objects, each carrying ``name``, ``disabled``, ``mirrorOf``).
- Keeps outputs that are enabled and not mirrors of another output; a
  disabled or mirrored output has no independently wallpapered image.
- Returns names sorted for determinism (house rule).
- Any failure — missing binary, non-zero exit, timeout, invalid JSON —
  returns ``[]`` (best-effort): a headless/CI machine or a transient
  Hyprland absence must degrade to the caller's default monitor, never
  abort seeding or ``wallpaper set``.

Binary resolution reuses ``_resolve_hyprctl`` from ``hyprland_reloader``
(adapters→adapters import — layering-green, same precedent as
``hyprpaper_reloader``). Subprocess handling mirrors the reloader
families: ``text=True``, ``capture_output=True``, ``timeout=10``, and the
exception set ``FileNotFoundError, PermissionError, TimeoutExpired,
OSError`` plus ``ValueError`` so text-mode decode errors still return
``[]``.

References: [Hyprctl monitors], [ARCHITECTURE-SPINE.md:235 — the
previously-deferred "real monitor detection" stub, closed by this
connector].
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from runtime.adapters.hyprland_reloader import _resolve_hyprctl
from runtime.ports.monitor_source import IMonitorSource

logger = logging.getLogger(__name__)

_SUBPROCESS_ERRORS: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    PermissionError,
    subprocess.TimeoutExpired,
    OSError,
    ValueError,
)


class HyprlandMonitorSource(IMonitorSource):
    """Detect active Hyprland output names via ``hyprctl monitors -j``.

    Args:
        hyprctl_path: explicit path to the ``hyprctl`` binary. When
            ``None``, resolved via ``shutil.which("hyprctl")``; if not
            found, ``detect_monitors()`` returns ``[]`` without spawning
            a subprocess.
    """

    def __init__(self, hyprctl_path: Path | None = None) -> None:
        self._hyprctl_path: Path | None = _resolve_hyprctl(hyprctl_path)

    def detect_monitors(self) -> list[str]:
        """Return the ordered active output names, or ``[]`` when undetectable."""
        if self._hyprctl_path is None:
            logger.warning("hyprctl not found in PATH; monitor detection unavailable")
            return []
        try:
            result = subprocess.run(
                [str(self._hyprctl_path), "monitors", "-j"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except _SUBPROCESS_ERRORS as exc:
            logger.warning("monitor detection failed: %s", exc)
            return []
        if result.returncode != 0:
            logger.warning(
                "monitor detection failed (exit %d): %s", result.returncode, result.stderr
            )
            return []
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            logger.warning("monitor detection returned invalid JSON: %s", exc)
            return []
        if not isinstance(payload, list):
            logger.warning(
                "monitor detection returned unexpected shape: %s", type(payload).__name__
            )
            return []
        names: list[str] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            if entry.get("disabled"):
                continue
            if entry.get("mirrorOf") not in (None, "", "none"):
                continue
            names.append(name)
        names.sort()
        return names
