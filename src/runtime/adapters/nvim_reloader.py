"""nvim reload adapter — signals running Neovim processes (SIGUSR1).

Implements ``IDesktopReloader``: after the swap repoints the runtime palette
artifact (``colors.nvim.lua``), the adapter enumerates running Neovim
processes (``pgrep -x nvim``) and sends each ``SIGUSR1`` so a running editor
can re-read the palette without a restart.

This mirrors ``KittyReloader`` (same enumeration, same signal, same R5
semantics) so the reload chain stays uniform and testable.

R5 semantics (mirrors ``KittyReloader``/``BtopReloader``):
- No nvim process is a VACUOUS SUCCESS (``True``) — an editor may
  legitimately not be open.
- An nvim process exists but signalling it fails (``OSError`` — e.g.
  permission) is a SURFACED FAILURE (``False``); the reconcile use case
  collects ``NvimReloader`` into ``ReconcileResult.reload_failures``.

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

#: Enumerate running nvim PIDs (returns ``[]`` when none / unavailable).
PidSource = Callable[[], list[int]]
#: Signal one PID with one signal number (``os.kill`` in production).
Signaller = Callable[[int, int], None]

_PGREP_TIMEOUT_S = 5


def _default_pid_source() -> list[int]:
    """Enumerate running nvim PIDs via ``pgrep -x nvim``.

    ``pgrep`` exits non-zero when no process matches (the empty result is
    the vacuous case). A missing/blocked ``pgrep``, a timeout, or any spawn
    error degrades to ``[]`` — enumeration is best-effort and must never
    turn "no reachable nvim" into a surfaced failure.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-x", "nvim"],
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
        logger.debug("nvim pid enumeration unavailable: %s", exc)
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


class NvimReloader(IDesktopReloader):
    """Adapter that reloads every running Neovim process via ``SIGUSR1``.

    Args:
        pid_source: injectable process enumerator. When ``None``, runs
            ``pgrep -x nvim``.
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
        """Signal every running nvim process so it reloads the palette.

        Returns:
            True when no nvim process is running (vacuous), or when every
            enumerated nvim process received ``SIGUSR1``. False when an nvim
            process exists but signalling it failed (surfaced per R5).
        """
        pids = self._pid_source()
        if not pids:
            logger.debug("no nvim processes running; nvim reload is vacuous")
            return True
        failed = False
        for pid in pids:
            try:
                self._signaller(pid, signal.SIGUSR1)
            except OSError as exc:
                logger.warning(
                    "NvimReloader reload failed: cannot signal nvim pid %s: %s", pid, exc
                )
                failed = True
        return not failed
