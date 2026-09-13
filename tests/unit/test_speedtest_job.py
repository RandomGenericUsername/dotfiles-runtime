"""Unit tests for the speed-test lifetime job (Phase 5, 5-4, AD-37).

The runner is a fake (no network); the hub client is a fake that records
the method calls and published payloads.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from runtime.application.speedtest import SpeedTestJob
from runtime.domain.jobs import SpeedTestResult
from runtime.ports.jobs import IJobClient, ISpeedTestRunner


class _FakeClient(IJobClient):
    def __init__(self) -> None:
        self.begins: list[tuple[str, float]] = []
        self.progress: list[tuple[str, float]] = []
        self.ends: list[tuple[str, int]] = []
        self.published: list[tuple[str, dict[str, object]]] = []

    def begin(self, kind: str, ttl: float) -> str:
        self.begins.append((kind, ttl))
        return "job-9"

    def renew(self, job_id: str) -> None:  # pragma: no cover - not used
        raise AssertionError("speed test never renews")

    def report_progress(self, job_id: str, fraction: float) -> None:
        self.progress.append((job_id, fraction))

    def end(self, job_id: str, exit_code: int) -> None:
        self.ends.append((job_id, exit_code))

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        self.published.append((topic, dict(payload)))


class _FakeRunner(ISpeedTestRunner):
    def __init__(
        self, result: SpeedTestResult | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    def measure(self) -> SpeedTestResult:
        self.calls += 1
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


class TestSpeedTestJob:
    def test_run_emits_contract_payload_and_ends_zero(self) -> None:
        client = _FakeClient()
        runner = _FakeRunner(SpeedTestResult(down_mbps=100.0, up_mbps=50.0, latency_ms=12.5))
        job = SpeedTestJob(client, runner, ttl=120.0)
        result = job.run()
        assert result == SpeedTestResult(100.0, 50.0, 12.5)
        assert client.begins == [("speedtest", 120.0)]
        assert client.progress == [("job-9", 0.0)]
        assert client.ends == [("job-9", 0)]
        assert client.published == [
            (
                "speedtest.finished",
                {"down_mbps": 100.0, "up_mbps": 50.0, "latency_ms": 12.5},
            )
        ]

    def test_payload_values_are_floats(self) -> None:
        client = _FakeClient()
        runner = _FakeRunner(SpeedTestResult(down_mbps=1, up_mbps=2, latency_ms=3))  # type: ignore[arg-type]
        SpeedTestJob(client, runner).run()
        payload = client.published[0][1]
        assert all(isinstance(value, float) for value in payload.values())

    def test_measurement_failure_ends_nonzero_and_reraises(self) -> None:
        client = _FakeClient()
        runner = _FakeRunner(error=RuntimeError("no route to host"))
        with pytest.raises(RuntimeError, match="no route"):
            SpeedTestJob(client, runner).run()
        assert client.ends == [("job-9", 1)]
        assert client.published == []

    def test_bad_ttl_rejected(self) -> None:
        with pytest.raises(ValueError, match="ttl"):
            SpeedTestJob(_FakeClient(), _FakeRunner(SpeedTestResult(1.0, 1.0, 1.0)), ttl=0)
