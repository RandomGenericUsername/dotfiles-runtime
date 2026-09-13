"""Unit tests for the shelled Ookla speed-test adapter (Phase 5, 5-4, AD-37).

No network and no live ``speedtest`` binary: the command runner is injected
(fakes raise ``subprocess`` errors or return text), and one test monkeypatches
``subprocess.run`` to pin the default runner's timeout wiring.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from runtime.adapters.command_speedtest_runner import (
    DEFAULT_SPEEDTEST_COMMAND,
    DEFAULT_SPEEDTEST_TIMEOUT_S,
    CommandSpeedTestRunner,
)
from runtime.domain.jobs import SpeedTestResult


def _payload(
    *,
    down: object = 1_000_000,
    up: object = 500_000,
    latency: object = 12.5,
    ping_style: str = "object",
) -> dict[str, object]:
    ping: object = {"latency": latency} if ping_style == "object" else latency
    return {"download": {"bandwidth": down}, "upload": {"bandwidth": up}, "ping": ping}


def _runner(
    payload: object = None,
    *,
    run=None,
    timeout: float = DEFAULT_SPEEDTEST_TIMEOUT_S,
) -> CommandSpeedTestRunner:
    if run is None:
        data = _payload() if payload is None else payload
        run = lambda command: json.dumps(data)  # noqa: E731
    return CommandSpeedTestRunner(DEFAULT_SPEEDTEST_COMMAND, run=run, timeout=timeout)


class TestHappyPath:
    def test_valid_report_is_converted(self) -> None:
        result = _runner().measure()
        # 1_000_000 B/s * 8 / 1e6 = 8 Mbps; 500_000 -> 4 Mbps.
        assert result == SpeedTestResult(down_mbps=8.0, up_mbps=4.0, latency_ms=12.5)

    def test_scalar_ping_is_accepted(self) -> None:
        result = _runner(_payload(ping_style="scalar")).measure()
        assert result.latency_ms == 12.5

    def test_command_is_passed_through(self) -> None:
        seen: list[list[str]] = []

        def run(command):
            seen.append(list(command))
            return json.dumps(_payload())

        CommandSpeedTestRunner(["custom-backend", "--json"], run=run).measure()
        assert seen == [["custom-backend", "--json"]]

    def test_values_are_floats(self) -> None:
        result = _runner(_payload(down=1, up=2, latency=3)).measure()
        assert all(
            isinstance(value, float)
            for value in (result.down_mbps, result.up_mbps, result.latency_ms)
        )


class TestConstruction:
    def test_default_command_constant(self) -> None:
        assert DEFAULT_SPEEDTEST_COMMAND == ("speedtest", "--format=json")

    def test_empty_command_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            CommandSpeedTestRunner([])

    @pytest.mark.parametrize("timeout", [0, -1.0, True, "x"])
    def test_invalid_timeout_rejected(self, timeout: object) -> None:
        with pytest.raises(ValueError, match="timeout"):
            CommandSpeedTestRunner(DEFAULT_SPEEDTEST_COMMAND, timeout=timeout)  # type: ignore[arg-type]


class TestMalformedOutput:
    def test_invalid_json_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="invalid JSON"):
            _runner(run=lambda command: "not json").measure()

    def test_empty_output_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="invalid JSON"):
            _runner(run=lambda command: "").measure()

    def test_json_must_be_an_object(self) -> None:
        with pytest.raises(ValueError, match="must be an object"):
            _runner(run=lambda command: json.dumps([1, 2, 3])).measure()

    def test_missing_download_or_upload_raises(self) -> None:
        with pytest.raises(ValueError, match="download/upload"):
            _runner(run=lambda command: json.dumps({"download": {"bandwidth": 1}})).measure()

    def test_non_numeric_bandwidth_raises(self) -> None:
        with pytest.raises(ValueError, match="bandwidth"):
            _runner(_payload(down="fast")).measure()

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_bandwidth_raises(self, value: float) -> None:
        with pytest.raises(ValueError, match="bandwidth"):
            _runner(run=lambda command: json.dumps({"download": {"bandwidth": value},
                                                    "upload": {"bandwidth": 1},
                                                    "ping": {"latency": 1}})).measure()

    def test_negative_bandwidth_raises(self) -> None:
        with pytest.raises(ValueError, match="bandwidth"):
            _runner(_payload(down=-1)).measure()

    def test_missing_ping_raises(self) -> None:
        with pytest.raises(ValueError, match="latency"):
            _runner({"download": {"bandwidth": 1}, "upload": {"bandwidth": 1}}).measure()

    def test_non_numeric_latency_raises(self) -> None:
        with pytest.raises(ValueError, match="latency"):
            _runner(_payload(latency="fast")).measure()

    def test_negative_latency_raises(self) -> None:
        with pytest.raises(ValueError, match="latency"):
            _runner(_payload(latency=-1)).measure()


class TestProcessFailures:
    def test_non_zero_exit_raises_runtime_error_with_detail(self) -> None:
        def run(command):
            raise subprocess.CalledProcessError(2, command, output="", stderr="no route to host")

        with pytest.raises(RuntimeError, match="exited 2.*no route to host"):
            _runner(run=run).measure()

    def test_non_zero_exit_without_detail_raises(self) -> None:
        def run(command):
            raise subprocess.CalledProcessError(3, command)

        with pytest.raises(RuntimeError, match="exited 3"):
            _runner(run=run).measure()

    def test_timeout_raises_runtime_error(self) -> None:
        def run(command):
            raise subprocess.TimeoutExpired(command, 5.0)

        with pytest.raises(RuntimeError, match="timed out"):
            _runner(run=run, timeout=5.0).measure()

    def test_missing_binary_raises_runtime_error(self) -> None:
        def run(command):
            raise FileNotFoundError("speedtest not found")

        with pytest.raises(RuntimeError, match="could not run"):
            _runner(run=run).measure()

    def test_non_text_output_raises_runtime_error(self) -> None:
        with pytest.raises(RuntimeError, match="non-text output"):
            _runner(run=lambda command: b"{}").measure()  # type: ignore[arg-type,return-value]


class TestDefaultRunnerWiring:
    def test_default_runner_applies_the_configured_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, object] = {}

        def fake_run(command: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["command"] = command
            captured.update(kwargs)
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(_payload()))

        monkeypatch.setattr(
            "runtime.adapters.command_speedtest_runner.subprocess.run", fake_run
        )
        result = CommandSpeedTestRunner(
            ["speedtest", "--format=json"], timeout=7.5
        ).measure()
        assert result.down_mbps == 8.0
        assert captured["timeout"] == 7.5
        assert captured["check"] is True
        assert captured["command"] == ["speedtest", "--format=json"]
