"""In-process hub — the 2a fake transport over the domain registry (Phase 5).

Implements :class:`IJobRegistry` by delegating every call to a domain
:class:`EventHub`. The ONLY concrete transport in P5-1-2a: no D-Bus, no
threads, no timers — the real D-Bus client stays deferred to P5-1-2b.

Layering (AD-1/13/14/25): adapters bridge ports and domain; the domain
never imports the port (domain→ports is forbidden), so this module owns
the (trivially delegating) seam.
"""

from __future__ import annotations

from runtime.domain.hub import EventHub
from runtime.ports.event_bus import IJobRegistry


class InProcessJobRegistry(IJobRegistry):
    """``IJobRegistry`` backed by a domain ``EventHub`` (test/wiring seam)."""

    def __init__(self, hub: EventHub) -> None:
        self._hub = hub

    @property
    def epoch(self) -> int:
        return self._hub.epoch

    def begin(self, kind: str, ttl: float) -> str:
        return self._hub.begin(kind, ttl)

    def renew(self, job_id: str) -> None:
        self._hub.renew(job_id)

    def adopt(self, job_id: str, pid: int) -> None:
        self._hub.adopt(job_id, pid)

    def report_progress(self, job_id: str, fraction: float) -> None:
        self._hub.report_progress(job_id, fraction)

    def end(self, job_id: str, exit_code: int) -> None:
        self._hub.end(job_id, exit_code)

    def control(self, job_id: str, action: str) -> None:
        self._hub.control(job_id, action)

    def active_jobs(self) -> dict[str, str]:
        return self._hub.active_jobs()
