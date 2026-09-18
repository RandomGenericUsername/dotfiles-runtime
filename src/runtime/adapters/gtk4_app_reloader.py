"""GTK4 app restart reload adapter — restarts GTK4 apps after a swap (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.adw.css`` (through the ``~/.config/gtk-4.0`` spine symlink),
GTK4/libadwaita apps (power-options-gtk, hyprmod) have NO native hot-reload
mechanism for CSS changes. This adapter discovers running instances via
``/proc`` scan and restarts them so they pick up the new color scheme.

Discovery pattern mirrors ``AgsReloader``: scan ``/proc/[pid]/cmdline`` for
target executable names (power-options-gtk, hyprmod). Restart pattern:
kill → relaunch with original argv (detached, ``start_new_session=True``) →
liveness poll (8 polls × 0.25s = 2s grace window).

Skip list provides safety for apps that should never be auto-restarted
(e.g., apps with critical unsaved state). Matches AgsReloader's skip list
pattern (ags-icme, ags-wallpaper-selector are excluded).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from runtime.ports.desktop_reloader import IDesktopReloader

logger = logging.getLogger(__name__)

_LIVENESS_POLLS = 8
_LIVENESS_POLL_INTERVAL = 0.25

#: Target GTK4 app executable names (discovered via /proc scan)
TARGET_APPS: frozenset[str] = frozenset({"power-options-gtk", "hyprmod"})

#: App names never restarted automatically. Currently empty; can be extended
#: if GTK4 apps have critical unsaved state that would be lost on restart.
DEFAULT_SKIP_APPS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Gtk4AppProcess:
    """One running GTK4 app process."""

    pid: int
    argv: tuple[str, ...]
    app_name: str


def _discover_gtk4_apps() -> list[Gtk4AppProcess]:
    """Enumerate running GTK4 app processes from ``/proc``.

    Scans ``/proc/[pid]/cmdline`` for processes whose executable basename
    matches ``TARGET_APPS``. Each discovered process is recorded with its pid,
    full argv, and app name.
    """
    apps: list[Gtk4AppProcess] = []
    proc = Path("/proc")
    try:
        entries = list(proc.iterdir())
    except OSError:
        return apps
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = tuple(part for part in raw.decode("utf-8", "replace").split("\0") if part)
        if not argv:
            continue
        exe_name = Path(argv[0]).name
        if exe_name in TARGET_APPS:
            apps.append(
                Gtk4AppProcess(
                    pid=int(entry.name),
                    argv=argv,
                    app_name=exe_name,
                )
            )
    return apps


class Gtk4AppReloader(IDesktopReloader):
    """Restarts GTK4 apps (power-options-gtk, hyprmod) on a palette swap.

    Args:
        skip_apps: app names never restarted automatically
            (defaults to ``DEFAULT_SKIP_APPS``).
        app_lister: injectable discovery of running GTK4 apps
            (defaults to a ``/proc`` scan) — tests inject a fake.
    """

    def __init__(
        self,
        *,
        skip_apps: frozenset[str] = DEFAULT_SKIP_APPS,
        app_lister: Callable[[], Sequence[Gtk4AppProcess]] = _discover_gtk4_apps,
    ) -> None:
        self._skip_apps = skip_apps
        self._app_lister = app_lister

    def reload(self) -> bool:
        """Restart every running GTK4 app process.

        Returns:
            True when every discovered app restarts successfully; False on
            any restart failure. No target apps running is a vacuous ``True``.
        """
        apps = self._app_lister()
        if not apps:
            logger.debug("gtk4: no target apps running; vacuous success")
            return True

        all_ok = True
        for app in apps:
            if app.app_name in self._skip_apps:
                logger.debug("gtk4: leaving %s running (skip list)", app.app_name)
                continue
            if not self._restart(app):
                all_ok = False
        return all_ok

    def _restart(self, app: Gtk4AppProcess) -> bool:
        """Kill and relaunch one GTK4 app process.

        Returns:
            True if the restart succeeded and the new process is still running
            after the liveness poll; False on any failure.
        """
        # Kill existing process
        try:
            subprocess.run(
                ["kill", str(app.pid)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (
            FileNotFoundError,
            PermissionError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ) as exc:
            logger.debug("gtk4: kill failed for %s (pid %d): %s", app.app_name, app.pid, exc)
            # Continue with relaunch attempt anyway

        # Relaunch with original argv
        try:
            proc = subprocess.Popen(
                app.argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (
            FileNotFoundError,
            PermissionError,
            OSError,
            ValueError,
        ) as exc:
            logger.warning("gtk4: relaunch failed for %s: %s", app.app_name, exc)
            return False

        # Liveness poll
        for poll_index in range(_LIVENESS_POLLS):
            if proc.poll() is not None:
                logger.warning(
                    "gtk4: restart failed for %s: process exited with code %s",
                    app.app_name,
                    proc.returncode,
                )
                return False
            if poll_index < _LIVENESS_POLLS - 1:
                time.sleep(_LIVENESS_POLL_INTERVAL)
        return True