"""Speed-test runner — a shelled measurement backend (Phase 5, AD-37).

A concrete :class:`ISpeedTestRunner` over a command emitting the Ookla
``speedtest --format=json`` shape. The command runner is injectable so the
adapter is unit-testable without network access; the job's own tests use a
fake runner instead (no network, per the story).

Failure modes are surfaced as typed exceptions rather than leaking
``subprocess`` internals: a non-zero exit, a timeout, or a missing binary
raises :class:`RuntimeError` (process-level), while malformed / non-numeric
/ non-finite output raises :class:`ValueError` (data-level). Ookla reports
bandwidth in bytes/second and latency in milliseconds; the conversion to the
contract's ``d`` megabit fields is done here.
"""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Callable, Sequence
from functools import partial

from runtime.domain.jobs import SpeedTestResult
from runtime.ports.jobs import ISpeedTestRunner

__all__ = ["DEFAULT_SPEEDTEST_COMMAND", "DEFAULT_SPEEDTEST_TIMEOUT_S", "CommandSpeedTestRunner"]

DEFAULT_SPEEDTEST_COMMAND: tuple[str, ...] = ("speedtest", "--format=json")

#: Wall-clock ceiling for one measurement (seconds).
DEFAULT_SPEEDTEST_TIMEOUT_S = 60.0

_BITS_PER_MEGABIT = 1_000_000.0
_BYTES_PER_SECOND = 8.0


def _run_command(command: Sequence[str], *, timeout: float) -> str:
    """Run the backend and return stdout (raise loudly on failure)."""
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return completed.stdout


class CommandSpeedTestRunner(ISpeedTestRunner):
    """Runs the backend command and parses its JSON report."""

    def __init__(
        self,
        command: Sequence[str] = DEFAULT_SPEEDTEST_COMMAND,
        *,
        run: Callable[[Sequence[str]], str] | None = None,
        timeout: float = DEFAULT_SPEEDTEST_TIMEOUT_S,
    ) -> None:
        if not command:
            raise ValueError("speed-test command must be non-empty")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"speed-test timeout must be > 0 seconds, got {timeout!r}")
        self._command = list(command)
        self._timeout = float(timeout)
        self._run = run if run is not None else partial(_run_command, timeout=self._timeout)

    def measure(self) -> SpeedTestResult:
        raw = self._invoke()
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

    def _invoke(self) -> str:
        """Run the injection seam, translating process failures to ``RuntimeError``."""
        try:
            raw = self._run(self._command)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"speed-test backend timed out after {self._timeout:g}s"
            ) from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeError(
                f"speed-test backend exited {exc.returncode}{suffix}"
            ) from exc
        except OSError as exc:
            raise RuntimeError(f"speed-test backend could not run: {exc}") from exc
        if not isinstance(raw, str):
            raise RuntimeError(
                f"speed-test backend returned non-text output: {type(raw).__name__}"
            )
        return raw


def _finite_number(value: object, field: str) -> float:
    """Coerce a JSON number to a finite, non-negative float or fail loud."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"speed-test {field} must be a number, got {value!r}")
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:  # e.g. an int too large for float
        raise ValueError(f"speed-test {field} is out of range, got {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"speed-test {field} must be finite and non-negative, got {value!r}")
    return number


def _bytes_per_second_to_mbps(value: object) -> float:
    result = _finite_number(value, "bandwidth") * _BYTES_PER_SECOND / _BITS_PER_MEGABIT
    if not math.isfinite(result):  # a huge finite input can overflow on scaling
        raise ValueError(f"speed-test bandwidth is out of range, got {value!r}")
    return result


def _latency_ms(ping: object) -> float:
    latency = ping.get("latency") if isinstance(ping, dict) else ping
    return _finite_number(latency, "latency")
