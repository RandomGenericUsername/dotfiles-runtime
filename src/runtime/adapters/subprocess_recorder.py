"""Subprocess recorder — the capture job's observed child (Phase 5, AD-37).

A thin :class:`IRecorderProcess` over a spawned recorder backend. Backends
disagree on pause/resume/stop signalling, so the adapter resolves a
:class:`RecorderSignals` map from the backend name (inferred from the
command's executable, or passed explicitly):

- ``gpu-screen-recorder`` — ``SIGUSR2`` toggles pause/unpause, ``SIGINT``
  finalizes the file.
- ``wf-recorder`` — has no native pause; ``SIGSTOP``/``SIGCONT`` suspend and
  resume the process, ``SIGINT`` finalizes the file.

The spawn callable, signaler, clock/sleep, and startup grace are injectable
so the adapter is unit-testable without spawning anything.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from runtime.ports.jobs import IRecorderProcess

__all__ = [
    "DEFAULT_BACKEND",
    "DEFAULT_STARTUP_GRACE",
    "RECORDER_BACKENDS",
    "RecorderSignals",
    "SubprocessRecorder",
]

logger = logging.getLogger(__name__)

#: Grace window before checking the child survived startup.
DEFAULT_STARTUP_GRACE = 0.15

#: Backend used when the command's executable is not a recognized recorder.
DEFAULT_BACKEND = "gpu-screen-recorder"


@dataclass(frozen=True, slots=True)
class RecorderSignals:
    """The three control signals a recorder backend understands."""

    stop: int
    pause: int
    resume: int


#: Backend → control signals. gpu-screen-recorder uses one toggle signal for
#: both pause and resume; wf-recorder has no native pause, so process-level
#: ``SIGSTOP``/``SIGCONT`` suspend and resume it. Both finalize on ``SIGINT``.
RECORDER_BACKENDS: Mapping[str, RecorderSignals] = {
    "gpu-screen-recorder": RecorderSignals(
        stop=signal.SIGINT,
        pause=signal.SIGUSR2,
        resume=signal.SIGUSR2,
    ),
    "wf-recorder": RecorderSignals(
        stop=signal.SIGINT,
        pause=signal.SIGSTOP,
        resume=signal.SIGCONT,
    ),
}


def _infer_backend(command: Sequence[str]) -> str:
    """Pick a backend from the command's executable basename, if known."""
    executable = os.path.basename(str(command[0]))
    return executable if executable in RECORDER_BACKENDS else DEFAULT_BACKEND


def _resolve_signals(backend: str | None, command: Sequence[str]) -> RecorderSignals:
    resolved = _infer_backend(command) if backend is None else backend
    try:
        return RECORDER_BACKENDS[resolved]
    except KeyError as exc:
        raise ValueError(
            f"unknown recorder backend {resolved!r}; known: {sorted(RECORDER_BACKENDS)}"
        ) from exc


class SubprocessRecorder(IRecorderProcess):
    """Owns one recorder child process for the duration of a recording."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        backend: str | None = None,
        spawn: Callable[..., Any] = subprocess.Popen,
        signaler: Callable[[int, int], None] = os.kill,
        sleep: Callable[[float], None] = time.sleep,
        startup_grace: float = DEFAULT_STARTUP_GRACE,
        stop_timeout: float = 5.0,
    ) -> None:
        if not command:
            raise ValueError("recorder command must be non-empty")
        self._command = list(command)
        self._signals = _resolve_signals(backend, self._command)
        self._spawn = spawn
        self._signaler = signaler
        self._sleep = sleep
        self._startup_grace = startup_grace
        self._stop_timeout = stop_timeout
        self._proc: Any = None

    @property
    def signals(self) -> RecorderSignals:
        """The resolved backend signal map (observable for wiring/tests)."""
        return self._signals

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            raise RuntimeError("recorder is already running")
        self._proc = self._spawn(self._command, start_new_session=True)
        self._sleep(self._startup_grace)
        if self._proc.poll() is not None:
            raise RuntimeError("recorder backend exited during startup")

    def pause(self) -> None:
        self._signal(self._signals.pause)

    def resume(self) -> None:
        self._signal(self._signals.resume)

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        try:
            self._signaler(proc.pid, self._signals.stop)
        except OSError:
            logger.warning(
                "recorder: stop signal %s failed for pid %s; terminating",
                self._signals.stop,
                proc.pid,
            )
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
