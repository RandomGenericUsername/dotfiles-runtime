"""Degraded capture job client — no hub, recorder still runs (5-4, AD-41).

When the session bus or the reactive daemon is absent the capture host must
still record (reduced functionality) and must never crash-loop. This adapter
satisfies :class:`IControllableJobClient` with a synthetic job id and no-op
reporting: no hub registration, no domain events, and no remote ``Control``
(there is no hub to route one). ``set_control_handler`` is accepted and
ignored so the host's lifecycle is identical on both paths.

This is the only job client that does not touch a bus; it is deliberately
tiny so the degraded path cannot itself become a failure mode.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from runtime.ports.jobs import IControllableJobClient

__all__ = ["LOCAL_JOB_ID", "LocalJobClient"]

logger = logging.getLogger(__name__)

#: Synthetic job id handed back while running without a hub.
LOCAL_JOB_ID = "capture-local"


class LocalJobClient(IControllableJobClient):
    """``IControllableJobClient`` with no transport (reduced functionality)."""

    def __init__(self) -> None:
        self._job_id: str | None = None

    def begin(self, kind: str, ttl: float) -> str:
        self._job_id = LOCAL_JOB_ID
        logger.info(
            "capture: running without a hub (kind=%s ttl=%s); "
            "no domain events or remote control this session",
            kind,
            ttl,
        )
        return LOCAL_JOB_ID

    def renew(self, job_id: str) -> None:
        return None

    def report_progress(self, job_id: str, fraction: float) -> None:
        return None

    def end(self, job_id: str, exit_code: int) -> None:
        if self._job_id == job_id:
            self._job_id = None

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        return None

    def set_control_handler(self, handler: Callable[[str, str], None]) -> None:
        return None
