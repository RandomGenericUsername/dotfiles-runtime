"""Speed-test runner — a shelled measurement backend (Phase 5, AD-37).

A concrete :class:`ISpeedTestRunner` over a command emitting the Ookla
``speedtest --format=json`` shape. The command runner is injectable so the
adapter is unit-testable without network access; the job's own tests use a
fake runner instead (no network, per the story).

Ookla reports bandwidth in bytes/second and latency in milliseconds; the
conversion to the contract's ``d`` megabit fields is done here.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence

from runtime.domain.jobs import SpeedTestResult
from runtime.ports.jobs import ISpeedTestRunner

__all__ = ["CommandSpeedTestRunner"]

DEFAULT_SPEEDTEST_COMMAND: tuple[str, ...] = ("speedtest", "--format=json")
_BITS_PER_MEGABIT = 1_000_000.0
_BYTES_PER_SECOND = 8.0


def _run_command(command: Sequence[str]) -> str:
    """Run the backend and return stdout (raise loudly on failure)."""
    completed = subprocess.run(command, capture_output=True, text=True, check=True)
    return completed.stdout


class CommandSpeedTestRunner(ISpeedTestRunner):
    """Runs the backend command and parses its JSON report."""

    def __init__(
        self,
        command: Sequence[str] = DEFAULT_SPEEDTEST_COMMAND,
        *,
        run: Callable[[Sequence[str]], str] = _run_command,
    ) -> None:
        if not command:
            raise ValueError("speed-test command must be non-empty")
        self._command = list(command)
        self._run = run

    def measure(self) -> SpeedTestResult:
        raw = self._run(self._command)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"speed-test backend emitted invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("speed-test backend JSON must be an object")
        download = data.get("download")
        upload = data.get("upload")
        if not isinstance(download, dict) or not isinstance(upload, dict):
            raise ValueError("speed-test backend JSON missing download/upload objects")
        down = _bytes_per_second_to_mbps(download.get("bandwidth"))
        up = _bytes_per_second_to_mbps(upload.get("bandwidth"))
        latency = _latency_ms(data.get("ping"))
        return SpeedTestResult(down_mbps=down, up_mbps=up, latency_ms=latency)


def _bytes_per_second_to_mbps(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"speed-test bandwidth must be a number, got {value!r}")
    return float(value) * _BYTES_PER_SECOND / _BITS_PER_MEGABIT


def _latency_ms(ping: object) -> float:
    latency = ping.get("latency") if isinstance(ping, dict) else ping
    if isinstance(latency, bool) or not isinstance(latency, (int, float)):
        raise ValueError(f"speed-test latency must be a number, got {latency!r}")
    return float(latency)
