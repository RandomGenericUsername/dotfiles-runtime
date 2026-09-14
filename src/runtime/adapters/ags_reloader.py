"""AGS restart reload adapter — restarts AGS consumers after a swap (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.gtk.css`` (through the ``~/.config/ags`` spine symlink to
``colors.css``, applied at runtime via ``app.apply_css``), AGS has NO native
hot-reload (verified in the AGS source, ``cli/cmd/run.go:145`` ``// TODO: watch
and restart`` [ARCHITECTURE-SPINE.md:236]), so a restart is the only reload
channel. This includes the standalone AGS tools (``hypr-pano``, ``capture``,
…) that also read the generated palette: each running instance is restarted
with the argv it was launched with, so the palette change propagates exactly
like it does for the bar.

The bar (default instance ``ags``, launched as a bare ``ags run``) is restarted
unconditionally, mirroring the original Story 2.4 behavior. The standalone tool
instances are discovered from ``/proc`` (their ``ags run -d <dir>`` argv), and
only running ones are restarted; an instance whose config dir is in
``skip_config_dirs`` is left alone (the editor ``ags-icme`` may hold unsaved
state).

Restart per instance = quit (``ags quit -i <instance>``; tolerated failure —
a non-zero exit deterministically means no live instance) + a detached
``ags run`` (``Popen`` + ``start_new_session=True`` — never a blocking
``run()``) + a liveness poll within a ≤ 2s grace window.

Missing ``ags`` in PATH is a surfaced failure, not a skip (spec-literal R5;
same decision as the Hyprland adapter).

Binary resolution imports ``_resolve_via_which`` from ``hyprland_reloader``
(adapters→adapters import — layering-green) for the bare-name ``PATH`` lookup;
the separator/executable-file branch structure mirrors
``hyprland_reloader._resolve_hyprctl``.
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

from runtime.adapters.hyprland_reloader import _resolve_via_which
from runtime.ports.desktop_reloader import IDesktopReloader

logger = logging.getLogger(__name__)

_LIVENESS_POLLS = 8
_LIVENESS_POLL_INTERVAL = 0.25

#: Config-dir names never restarted automatically. The icon color mapping
#: editor can hold unsaved edits; a wallpaper change must not discard them.
#: The wallpaper selector is on-demand with a self-hiding window: restarting
#: it after a set would pop the dialog back open uninvited (it starts
#: visible, ICME pattern). It refreshes its palette live from the
#: ``wallpaper.state`` domain event instead (no restart needed).
DEFAULT_SKIP_CONFIG_DIRS: frozenset[str] = frozenset({"ags-icme", "ags-wallpaper-selector"})

_INSTANCE_NAME_RE = re.compile(r"""instanceName\s*:\s*["']([^"']+)["']""")


def _resolve_ags(ags_path: Path | None) -> Path | None:
    """Resolve the ``ags`` binary path (mirrors ``hyprland_reloader``)."""
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


@dataclass(frozen=True, slots=True)
class AgsAppProcess:
    """One running standalone AGS app (``ags run -d <dir>``)."""

    pid: int
    argv: tuple[str, ...]
    config_dir: Path
    instance: str


def _instance_name_for_dir(config_dir: Path) -> str | None:
    """Read ``instanceName`` from the app's ``app.tsx`` (None if absent)."""
    try:
        text = (config_dir / "app.tsx").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _INSTANCE_NAME_RE.search(text)
    return match.group(1) if match is not None else None


def _discover_ags_apps() -> list[AgsAppProcess]:
    """Enumerate running standalone AGS apps from ``/proc`` (bar excluded).

    The bar runs as a bare ``ags run`` with no config dir; it is restarted
    separately and never appears here.
    """
    apps: list[AgsAppProcess] = []
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
        if len(argv) < 2 or Path(argv[0]).name != "ags" or argv[1] != "run":
            continue
        config_dir: Path | None = None
        for index, arg in enumerate(argv):
            if arg in ("-d", "--directory") and index + 1 < len(argv):
                config_dir = Path(argv[index + 1])
                break
        if config_dir is None:
            continue
        instance = _instance_name_for_dir(config_dir) or config_dir.name
        apps.append(
            AgsAppProcess(
                pid=int(entry.name),
                argv=argv,
                config_dir=config_dir,
                instance=instance,
            )
        )
    return apps


class AgsReloader(IDesktopReloader):
    """Restarts the bar and the standalone AGS consumers on a palette swap.

    Args:
        ags_path: explicit path to the ``ags`` binary. When ``None``,
            resolved via ``shutil.which("ags")``; if not found, the
            adapter stores ``None`` and ``reload()`` returns ``False``
            without spawning a subprocess (surfaced failure, not a skip).
        app_lister: injectable discovery of running standalone apps
            (defaults to a ``/proc`` scan) — tests inject a fake.
        skip_config_dirs: config-dir names never restarted automatically.
    """

    def __init__(
        self,
        ags_path: Path | None = None,
        *,
        app_lister: Callable[[], Sequence[AgsAppProcess]] = _discover_ags_apps,
        skip_config_dirs: frozenset[str] = DEFAULT_SKIP_CONFIG_DIRS,
    ) -> None:
        self._ags_path: Path | None = _resolve_ags(ags_path)
        self._app_lister = app_lister
        self._skip_config_dirs = skip_config_dirs

    def reload(self) -> bool:
        """Restart the bar and every running standalone AGS consumer.

        Returns:
            True when the bar restarted (its detached process is still alive
            at the end of the liveness window) AND every reloaded tool did
            too; False on a missing binary or any restart failure.
        """
        if self._ags_path is None:
            logger.warning("ags not found in PATH; AGS reload skipped")
            return False

        bar_ok = self._restart(self._ags_path, [], instance=None)

        tools_ok = True
        for app in self._app_lister():
            if app.config_dir.name in self._skip_config_dirs:
                logger.debug("ags: leaving %s running (skip list)", app.instance)
                continue
            if not self._restart(self._ags_path, list(app.argv), instance=app.instance):
                tools_ok = False
        return bar_ok and tools_ok

    def _restart(
        self,
        ags_bin: Path,
        argv: list[str],
        *,
        instance: str | None,
    ) -> bool:
        """Quit ``instance`` (bar when ``None``) then relaunch ``argv`` detached."""
        quit_argv = [str(ags_bin), "quit"]
        if instance is not None:
            quit_argv += ["-i", instance]
        try:
            quit_result = subprocess.run(
                quit_argv,
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
        else:
            if quit_result.returncode != 0:
                logger.debug("ags quit reports no live instance; continuing")
            else:
                logger.debug("ags quit delivered; teardown is asynchronous")

        run_argv = [str(ags_bin), "run"] if not argv else argv
        try:
            proc = subprocess.Popen(
                run_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for poll_index in range(_LIVENESS_POLLS):
                if proc.poll() is not None:
                    logger.warning(
                        "AGS restart failed for %s: process exited with code %s",
                        instance or "bar",
                        proc.returncode,
                    )
                    return False
                if poll_index < _LIVENESS_POLLS - 1:
                    time.sleep(_LIVENESS_POLL_INTERVAL)
            return True
        except (
            FileNotFoundError,
            PermissionError,
            subprocess.TimeoutExpired,
            OSError,
            ValueError,
        ) as exc:
            logger.warning("AGS reload failed for %s: %s", instance or "bar", exc)
            return False
