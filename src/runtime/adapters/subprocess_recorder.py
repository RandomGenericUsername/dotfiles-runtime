"""Subprocess recorder — the capture job's observed child (Phase 5, AD-37).

A thin :class:`IRecorderProcess` over a spawned recorder backend
(``gpu-screen-recorder`` / ``wf-recorder``). Pause/resume use
``SIGUSR2`` (gpu-screen-recorder's toggle) and stop uses ``SIGINT`` so the
backend finalizes the file. The spawn callable, signaler, and clock are
injectable so the adapter is unit-testable without spawning anything.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

from runtime.ports.jobs import IRecorderProcess

__all__ = ["SubprocessRecorder"]

logger = logging.getLogger(__name__)

#: Grace window before checking the child survived startup.
DEFAULT_STARTUP_GRACE = 0.15


class SubprocessRecorder(IRecorderProcess):
    """Owns one recorder child process for the duration of a recording."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        spawn: Callable[..., Any] = subprocess.Popen,
        signaler: Callable[[int, int], None] = os.kill,
        sleep: Callable[[float], None] = time.sleep,
        startup_grace: float = DEFAULT_STARTUP_GRACE,
        stop_timeout: float = 5.0,
    ) -> None:
        if not command:
            raise ValueError("recorder command must be non-empty")
        self._command = list(command)
        self._spawn = spawn
        self._signaler = signaler
        self._sleep = sleep
        self._startup_grace = startup_grace
        self._stop_timeout = stop_timeout
        self._proc: Any = None

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            raise RuntimeError("recorder is already running")
        self._proc = self._spawn(self._command, start_new_session=True)
        self._sleep(self._startup_grace)
        if self._proc.poll() is not None:
            raise RuntimeError("recorder backend exited during startup")

    def pause(self) -> None:
        self._signal(signal.SIGUSR2)

    def resume(self) -> None:
        self._signal(signal.SIGUSR2)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            self._signaler(proc.pid, signal.SIGINT)
        except OSError:
            logger.warning("recorder: SIGINT failed for pid %s; terminating", proc.pid)
        try:
            proc.wait(timeout=self._stop_timeout)
        except Exception:
            logger.warning("recorder: graceful stop timed out; terminating pid %s", proc.pid)
            try:
                proc.terminate()
            except Exception:
                logger.exception("recorder: terminate failed; child may linger")

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _signal(self, signum: int) -> None:
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("recorder is not running")
        try:
            self._signaler(self._proc.pid, signum)
        except OSError:
            logger.warning("recorder: signal %s failed for pid %s", signum, self._proc.pid)
