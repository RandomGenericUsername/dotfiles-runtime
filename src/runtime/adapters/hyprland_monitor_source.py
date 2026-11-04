"""Hyprland monitor source adapter — enumerates outputs via ``hyprctl monitors -j``.

Implements both monitor ports:

- ``IMonitorSource`` (``detect_monitors()``): the wallpaper path's active
  output names, sorted, disabled/mirrors filtered. This behavior is
  unchanged by REQ-009.
- ``IMonitorCatalog`` (``list_monitors()``): the full descriptor payload
  consumed by ``dotfiles-runtime monitors list`` and future per-monitor
  features. It parses ``name``/``description``/``make``/``model``/``serial``
  /``x``/``y``/``width``/``height``/``scale``/``transform``/``focused``/
  ``disabled``/``mirrorOf``. Disabled/mirrored entries are retained so the
  pure application projection owns filtering and ordering.

Detection contract (both methods):
- Runs ``hyprctl monitors -j`` (verified v0.56.x contract: JSON array of
  output objects).
- Any failure — missing binary, non-zero exit, timeout, invalid JSON —
  returns ``[]`` (best-effort): a headless/CI machine or a transient
  Hyprland absence must degrade to the caller's documented fallback, never
  abort seeding, ``wallpaper set``, or the read-only catalog command.

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
from runtime.ports.monitor_catalog import IMonitorCatalog, MonitorGeometry, MonitorInfo
from runtime.ports.monitor_source import IMonitorSource

logger = logging.getLogger(__name__)

_SUBPROCESS_ERRORS: tuple[type[BaseException], ...] = (
    FileNotFoundError,
    PermissionError,
    subprocess.TimeoutExpired,
    OSError,
    ValueError,
)


def _string(value: object) -> str:
    """Coerce an optional compositor string field; absent/non-string -> ``""``."""
    return value if isinstance(value, str) else ""


def _integer(value: object, default: int = 0) -> int:
    """Coerce a JSON number/bool to ``int``; malformed values use ``default``."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _number(value: object, default: float = 1.0) -> float:
    """Coerce a JSON number to ``float``; malformed values use ``default``."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _mirror_of(value: object) -> str | None:
    """Normalize Hyprland's ``mirrorOf`` sentinel values to ``None``."""
    if value in (None, "", "none"):
        return None
    return value if isinstance(value, str) else str(value)


class HyprlandMonitorSource(IMonitorSource, IMonitorCatalog):
    """Enumerate Hyprland outputs via ``hyprctl monitors -j``.

    Args:
        hyprctl_path: explicit path to the ``hyprctl`` binary. When
            ``None``, resolved via ``shutil.which("hyprctl")``; if not
            found, both methods return ``[]`` without spawning a
            subprocess.
    """

    def __init__(self, hyprctl_path: Path | None = None) -> None:
        self._hyprctl_path: Path | None = _resolve_hyprctl(hyprctl_path)

    def _read_entries(self) -> list[dict[str, object]] | None:
        """Return the compositor's raw output objects, or ``None`` on failure.

        ``None`` is the availability signal; an empty list means the
        compositor answered with no outputs. All failure logging lives here
        so ``detect_monitors()`` and ``list_monitors()`` share one path.
        """
        if self._hyprctl_path is None:
            logger.warning("hyprctl not found in PATH; monitor detection unavailable")
            return None
        try:
            result = subprocess.run(
                [str(self._hyprctl_path), "monitors", "-j"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except _SUBPROCESS_ERRORS as exc:
            logger.warning("monitor detection failed: %s", exc)
            return None
        if result.returncode != 0:
            logger.warning(
                "monitor detection failed (exit %d): %s", result.returncode, result.stderr
            )
            return None
        try:
            payload = json.loads(result.stdout)
        except ValueError as exc:
            logger.warning("monitor detection returned invalid JSON: %s", exc)
            return None
        if not isinstance(payload, list):
            logger.warning(
                "monitor detection returned unexpected shape: %s", type(payload).__name__
            )
            return None
        return [entry for entry in payload if isinstance(entry, dict)]

    def list_monitors(self) -> list[MonitorInfo]:
        """Return every reported output as a descriptor (compositor order).

        Disabled and mirrored outputs are retained for the pure projection.
        An empty list means detection was unavailable or the compositor
        reported no outputs.
        """
        entries = self._read_entries()
        if entries is None:
            return []
        monitors: list[MonitorInfo] = []
        for entry in entries:
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                continue
            description = _string(entry.get("description"))
            monitors.append(
                MonitorInfo(
                    connector=name,
                    label=description or name,
                    description=description,
                    make=_string(entry.get("make")),
                    model=_string(entry.get("model")),
                    serial=_string(entry.get("serial")),
                    geometry=MonitorGeometry(
                        x=_integer(entry.get("x")),
                        y=_integer(entry.get("y")),
                        width=_integer(entry.get("width")),
                        height=_integer(entry.get("height")),
                    ),
                    scale=_number(entry.get("scale")),
                    transform=_integer(entry.get("transform")),
                    focused=bool(entry.get("focused")),
                    mirror_of=_mirror_of(entry.get("mirrorOf")),
                    disabled=bool(entry.get("disabled")),
                )
            )
        return monitors

    def detect_monitors(self) -> list[str]:
        """Return the ordered active output names, or ``[]`` when undetectable."""
        names = [
            monitor.connector
            for monitor in self.list_monitors()
            if not monitor.disabled and monitor.mirror_of is None
        ]
        names.sort()
        return names
