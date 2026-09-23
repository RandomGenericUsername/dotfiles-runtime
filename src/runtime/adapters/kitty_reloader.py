"""kitty reload adapter — signals running kitty processes (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/colors.kitty`` (which the provisioned ``kitty.conf`` includes),
the adapter enumerates running kitty processes (``pgrep -x kitty``) and
sends each ``SIGUSR1``. kitty reloads its configuration (including the
included runtime palette) in the windows of the signalled process, so every
OPEN kitty window follows the new palette — event-driven, no polling, no
OSC/pty writes.

R5 semantics (mirrors ``TerminalColorApplier``):
- No kitty process is a VACUOUS SUCCESS (``True``) — a terminal may
  legitimately not be open.
- A kitty process exists but signalling it fails (``OSError`` — e.g.
  permission) is a SURFACED FAILURE (``False``); the reconcile use case
  collects ``KittyReloader`` into ``ReconcileResult.reload_failures``.

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

#: Enumerate running kitty PIDs (returns ``[]`` when none / unavailable).
PidSource = Callable[[], list[int]]
#: Signal one PID with one signal number (``os.kill`` in production).
Signaller = Callable[[int, int], None]

_PGREP_TIMEOUT_S = 5


def _default_pid_source() -> list[int]:
    """Enumerate running kitty PIDs via ``pgrep -x kitty``.

    ``pgrep`` exits non-zero when no process matches (the empty result is
    the vacuous case). A missing/blocked ``pgrep``, a timeout, or any spawn
    error degrades to ``[]`` — enumeration is best-effort and must never
    turn "no reachable kitty" into a surfaced failure.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "kitty"],
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
        logger.debug("kitty pid enumeration unavailable: %s", exc)
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


class KittyReloader(IDesktopReloader):
    """Adapter that reloads every running kitty process via ``SIGUSR1``.

    Args:
        pid_source: injectable process enumerator. When ``None``, runs
            ``pgrep -x kitty``.
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
        """Signal every running kitty process so it reloads its config.

        Returns:
            True when no kitty process is running (vacuous), or when every
            enumerated kitty process received ``SIGUSR1``. False when a
            kitty process exists but signalling it failed (surfaced per R5).
        """
        pids = self._pid_source()
        if not pids:
            logger.debug("no kitty processes running; kitty reload is vacuous")
            return True
        failed = False
        for pid in pids:
            try:
                self._signaller(pid, signal.SIGUSR1)
            except OSError as exc:
                logger.warning(
                    "KittyReloader reload failed: cannot signal kitty pid %s: %s", pid, exc
                )
                failed = True
        return not failed
