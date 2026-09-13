"""Speed-test job — one lifetime job reporting through the hub (Phase 5, AD-37).

The job allocates a hub job, reports progress, runs the injected runner
(the real adapter shells the backend; tests inject a fake — no network),
publishes ``speedtest.finished`` through the hub (AD-38), and ends the job.
A failing measurement ends the job with a non-zero exit code and re-raises
so the caller can surface it; it never leaves the job dangling.
"""

from __future__ import annotations

import logging

from runtime.domain.jobs import SpeedTestResult
from runtime.ports.jobs import IJobClient, ISpeedTestRunner

__all__ = ["SpeedTestJob"]

logger = logging.getLogger(__name__)

DEFAULT_SPEEDTEST_TTL = 120.0

_RESULT_TOPIC = "speedtest.finished"


class SpeedTestJob:
    """Runs one measurement and reports it through the hub."""

    def __init__(
        self,
        client: IJobClient,
        runner: ISpeedTestRunner,
        *,
        ttl: float = DEFAULT_SPEEDTEST_TTL,
    ) -> None:
        if ttl <= 0:
            raise ValueError(f"speed-test ttl must be > 0, got {ttl!r}")
        self._client = client
        self._runner = runner
        self._ttl = float(ttl)

    def run(self) -> SpeedTestResult:
        """Execute the job; return the measured result.

        Emits exactly one ``speedtest.finished`` domain event with the
        contract-typed ``down_mbps``/``up_mbps``/``latency_ms`` doubles.
        """
        job_id = self._client.begin("speedtest", self._ttl)
        try:
            self._client.report_progress(job_id, 0.0)
            result = self._runner.measure()
            self._client.publish(
                _RESULT_TOPIC,
                {
                    "down_mbps": float(result.down_mbps),
                    "up_mbps": float(result.up_mbps),
                    "latency_ms": float(result.latency_ms),
                },
            )
            self._client.end(job_id, 0)
        except Exception:
            try:
                self._client.end(job_id, 1)
            except Exception:
                logger.exception("speedtest: EndJob(1) failed after a measurement error")
            raise
        return result
