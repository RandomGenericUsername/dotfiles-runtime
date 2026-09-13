"""Lifetime-job domain value types (Phase 5, AD-37).

Pure, zero-I/O value types shared by the job controllers in
``runtime.application`` and the ports in ``runtime.ports.jobs``. Nothing
here touches the bus, a clock, or a process: the capture and speed-test
jobs are *edges*, but their state vocabulary and result carrier are pure
domain data (AD-1/AD-14).
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CAPTURE_STATES", "SpeedTestResult"]

#: The ``capture.state.state`` enum, mirroring
#: ``contracts/event-contract.json`` ``topics['capture.state'].enum``. The
#: controller never invents a fourth state (the hub validator would reject
#: it as ``PayloadTooLarge``); ``starting``/``stopping`` from the legacy GUI
#: scaffolding are deliberately NOT contract states.
CAPTURE_STATES: frozenset[str] = frozenset({"idle", "recording", "paused"})


@dataclass(frozen=True, slots=True)
class SpeedTestResult:
    """One speed-test measurement (wire: ``speedtest.finished`` payload).

    Fields mirror ``contracts/event-contract.json``
    ``topics['speedtest.finished'].payload`` (``down_mbps``/``up_mbps``
    ``d``, ``latency_ms`` ``d``). They are floats by type so the emitted
    ``a{sv}`` variants encode as contract-typed doubles, never as an
    inferred ``x`` int.
    """

    down_mbps: float
    up_mbps: float
    latency_ms: float
