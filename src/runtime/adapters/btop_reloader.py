"""btop reload adapter — signals running btop processes (SIGUSR2).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.btop.theme`` (which the btop_config role exposes as a
consumer pointer under ``<install>/config/btop/themes/``), the adapter
enumerates running btop processes and sends each ``SIGUSR2``.

btop has no theme-file watcher, but it does honour ``SIGUSR2`` as a full
config + theme hot reload. ``src/btop.cpp:1053``::

    case SIGUSR2:
        Global::reload_conf = true;
        Input::interrupt();

and the main loop honours it at ``src/btop.cpp:1121-1130``::

    else if (Global::reload_conf) {
        init_config(cli.low_color, cli.filter);
        Theme::updateThemes();   // RE-SCANS the themes directory
        Theme::setTheme();       // re-reads the file from disk
        Global::resized = true;  // forces a full redraw
    }

Because ``updateThemes()`` re-scans rather than re-reads a cached path, a
theme file REPLACED on disk is picked up live, without restarting btop.
Verified empirically on btop 1.4.7: with the theme file swapped and no
signal, the rendered background stays on the old value; after ``SIGUSR2``
the new value appears and a second ``Loading theme file`` line is logged.

``SIGUSR1`` is deliberately NOT used: btop registers it as a bare
"Input::poll interrupt" no-op (``btop.cpp:314-315``). ``SIGWINCH`` only
forces a resize redraw. ``SIGUSR2`` is the reload signal.

This is the exact analogue of ``KittyReloader``, which sends ``SIGUSR1``
so kitty re-reads its config. Note that ``SIGUSR2`` re-reads btop's whole
config, not just the theme, so menu-driven config changes are re-applied
too. That is btop's own semantics for the signal and is intended.

R5 semantics (mirrors ``KittyReloader``):
- No btop process is a VACUOUS SUCCESS (``True``) — btop may legitimately
  not be open.
- A btop process exists but signalling it fails (``OSError``) is a
  SURFACED FAILURE (``False``); the reconcile use case collects
  ``BtopReloader`` into ``ReconcileResult.reload_failures``.

The pid source and the signaller are constructor-injectable so unit tests
need no real processes or signals.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from collections.abc import Callable

from runtime.ports.desktop_reloader import IDesktopReloader

logger = logging.getLogger(__name__)

#: Enumerate running btop PIDs (returns ``[]`` when none / unavailable).
PidSource = Callable[[], list[int]]
#: Signal one PID with one signal number (``os.kill`` in production).
Signaller = Callable[[int, int], None]

_PGREP_TIMEOUT_S = 5


def _default_pid_source() -> list[int]:
    """Enumerate running btop PIDs via ``pgrep -x btop``.

    ``pgrep`` exits non-zero when no process matches (the empty result is
    the vacuous case). A missing/blocked ``pgrep``, a timeout, or any spawn
    error degrades to ``[]`` — enumeration is best-effort and must never
    turn "no reachable btop" into a surfaced failure.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "btop"],
            capture_output=True,
            text=True,
            timeout=_PGREP_TIMEOUT_S,
        )
    except (
        FileNotFoundError,
        PermissionError,
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
    ) as exc:
        logger.debug("btop pid enumeration unavailable: %s", exc)
        return []
    pids: list[int] = []
    for token in (result.stdout or "").split():
        try:
            pid = int(token)
        except ValueError:
            continue
        if pid > 0:
            pids.append(pid)
    return pids


class BtopReloader(IDesktopReloader):
    """Adapter that reloads every running btop process via ``SIGUSR2``.

    Args:
        pid_source: injectable process enumerator. When ``None``, runs
            ``pgrep -x btop``.
        signaller: injectable ``(pid, signum)`` signaller. When ``None``,
            uses ``os.kill``.
    """

    def __init__(
        self,
        pid_source: PidSource | None = None,
        signaller: Signaller | None = None,
    ) -> None:
        self._pid_source: PidSource = pid_source if pid_source is not None else _default_pid_source
        self._signaller: Signaller = signaller if signaller is not None else os.kill

    def reload(self) -> bool:
        """Signal every running btop process so it reloads config + theme.

        Returns:
            True when no btop process is running (vacuous), or when every
            enumerated btop process received ``SIGUSR2``. False when a btop
            process exists but signalling it failed (surfaced per R5).
        """
        pids = self._pid_source()
        if not pids:
            logger.debug("no btop processes running; btop reload is vacuous")
            return True
        failed = False
        for pid in pids:
            try:
                self._signaller(pid, signal.SIGUSR2)
            except OSError as exc:
                logger.warning("BtopReloader reload failed: cannot signal btop pid %s: %s", pid, exc)
                failed = True
        return not failed