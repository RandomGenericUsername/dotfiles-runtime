"""GTK4 app restart reload adapter — restarts GTK4 apps after a swap (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.adw.css`` (through the ``~/.config/gtk-4.0`` spine symlink),
GTK4/libadwaita apps (power-options-gtk, hyprmod) have NO native hot-reload
mechanism for CSS changes. This adapter discovers running instances via
``/proc`` scan and restarts them so they pick up the new color scheme.

Restart-safety contract
-----------------------

- **Allowlist only.** Only ``TARGET_APPS`` (``power-options-gtk``, ``hyprmod``)
  are ever discovered or restarted; arbitrary GTK4 apps are never wildcard-swept
  in, so an app with genuine unsaved state is never at risk.
- **Per-target state rationale.** ``power-options-gtk`` is a frontend for the
  power-options daemon: changes apply to the daemon immediately, there is no
  pending buffer, and reopening re-reads state. ``hyprmod`` persists to the
  Hyprland config and applies via ``hyprctl``: state is on disk and reopening
  re-reads it. The only thing a restart can lose is a half-typed field in an
  open dialog — identical to the user closing the window.
- **Graceful first, then escalate.** SIGTERM is the same shutdown path as the
  user closing the window. The adapter waits (bounded poll of
  ``_LIVENESS_POLLS`` × ``_LIVENESS_POLL_INTERVAL`` ≈ 2 s) for the old pid to
  exit, escalating to SIGKILL only if the grace window elapses.
- **Race-free handoff.** Relaunch (``Popen(..., start_new_session=True)``)
  happens only after the old pid is gone. Both targets are single-instance
  ``GApplication``s: starting the replacement while the dying old instance still
  owns the session-bus name would forward activation to it and exit, silently
  failing the restart.
- **Opt-out and vacuous success.** ``skip_apps`` is the per-app opt-out; no
  target running is a vacuous success.

Discovery pattern mirrors ``AgsReloader``: scan ``/proc/[pid]/cmdline`` for
target app names. Both direct binaries (``power-options-gtk``) and
interpreter-wrapped scripts (``hyprmod`` is a Python script, so its cmdline is
``python /usr/bin/hyprmod``) are resolved from the argv shape.
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


#: Basenames that identify a Python interpreter (``python``, ``python3``,
#: ``python3.14``, ``python3.14.1``, ...).
_PYTHON_INTERPRETER = re.compile(r"(python|python3|python\d+(\.\d+)*)")


def _resolve_python_target(tokens: Sequence[str]) -> str | None:
    """Resolve a TARGET_APPS name from the tokens after a Python interpreter.

    Handles ``python -m hyprmod`` (module name) and
    ``python /usr/bin/hyprmod`` (script basename). Only the interpreter's
    immediate module/script position is inspected — never arbitrary argv — so
    ``bash -c 'hyprmod'`` / ``rg hyprmod`` cannot false-positive.
    """
    if not tokens:
        return None
    first = tokens[0]
    if first == "-m":
        if len(tokens) < 2:
            return None
        candidate = tokens[1].split(".")[0]
    else:
        candidate = Path(first).name
    return candidate if candidate in TARGET_APPS else None


def _resolve_app_name(argv: tuple[str, ...]) -> str | None:
    """Resolve an allowlisted app name from a process ``argv``.

    Resolution (precision over recall — no arbitrary-position scanning):

    - direct binary: ``argv[0]`` basename is a target (``power-options-gtk``);
    - interpreter-wrapped: ``argv[0]`` basename is a Python interpreter
      (``python``/``python3``/``python3.14``/...), so the target is resolved
      from the next token (``python /usr/bin/hyprmod`` or
      ``python -m hyprmod``);
    - ``/usr/bin/env python ...``: the Python interpreter is located after
      ``env`` and the same rule applied.

    Returns the matching ``TARGET_APPS`` name, else ``None``. The caller keeps
    the original full ``argv`` for relaunch.
    """
    if not argv:
        return None
    head = Path(argv[0]).name
    if head in TARGET_APPS:
        return head
    if _PYTHON_INTERPRETER.fullmatch(head):
        return _resolve_python_target(argv[1:])
    if head == "env":
        for index in range(1, len(argv)):
            if _PYTHON_INTERPRETER.fullmatch(Path(argv[index]).name):
                return _resolve_python_target(argv[index + 1 :])
    return None


def _discover_gtk4_apps(proc_root: Path = Path("/proc")) -> list[Gtk4AppProcess]:
    """Enumerate running GTK4 app processes from ``/proc``.

    Scans ``/proc/[pid]/cmdline`` and resolves each process to an allowlisted
    app via :func:`_resolve_app_name` — direct binaries (``power-options-gtk``)
    and interpreter-wrapped scripts (``hyprmod`` as ``python /usr/bin/hyprmod``)
    alike. Each discovered process is recorded with its pid, full original
    argv, and app name.
    """
    apps: list[Gtk4AppProcess] = []
    try:
        entries = list(proc_root.iterdir())
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
        app_name = _resolve_app_name(argv)
        if app_name is not None:
            apps.append(
                Gtk4AppProcess(
                    pid=int(entry.name),
                    argv=argv,
                    app_name=app_name,
                )
            )
    return apps


def _wait_for_exit(
    pid: int,
    *,
    polls: int = _LIVENESS_POLLS,
    interval: float = _LIVENESS_POLL_INTERVAL,
) -> bool:
    """Poll until ``pid`` is gone or the bounded budget elapses.

    Uses ``os.kill(pid, 0)`` as a liveness probe: it returns normally while the
    process exists and raises ``ProcessLookupError`` once it has exited.

    Returns:
        True when the process is gone; False when it is still alive after the
        poll budget.
    """
    for poll_index in range(polls):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if poll_index < polls - 1:
            time.sleep(interval)
    return False


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
            logger.info("gtk4: restarting %s (pid %d)", app.app_name, app.pid)
            if self._restart(app):
                logger.info("gtk4: restarted %s", app.app_name)
            else:
                logger.info("gtk4: restart failed for %s (pid %d)", app.app_name, app.pid)
                all_ok = False
        return all_ok

    def _restart(self, app: Gtk4AppProcess) -> bool:
        """Kill and relaunch one GTK4 app process.

        Returns:
            True if the restart succeeded and the new process is still running
            after the liveness poll; False on any failure.
        """
        # Graceful shutdown (SIGTERM) — equivalent to the user closing the window
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
            # Continue with the exit wait / relaunch attempt anyway

        if not _wait_for_exit(app.pid):
            logger.debug(
                "gtk4: %s (pid %d) still alive after grace window; escalating to SIGKILL",
                app.app_name,
                app.pid,
            )
            try:
                subprocess.run(
                    ["kill", "-9", str(app.pid)],
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
                logger.debug("gtk4: SIGKILL failed for %s (pid %d): %s", app.app_name, app.pid, exc)
            if not _wait_for_exit(app.pid):
                logger.warning(
                    "gtk4: %s (pid %d) still alive after SIGKILL; aborting restart",
                    app.app_name,
                    app.pid,
                )
                return False

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
