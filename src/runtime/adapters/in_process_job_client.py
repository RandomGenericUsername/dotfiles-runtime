"""In-process job client — a job reporting over a co-hosted hub (Phase 5).

Binds :class:`IJobClient` to an :class:`IJobRegistry` (hub methods) and an
:class:`IEventPublisher` (validated ``Emit``). This is the hermetic seam
the controller/speed-test tests use, and the composition the daemon uses
when it hosts a lifetime job in-process (control delivery then rides the
hub's control sink). The D-Bus equivalent for out-of-process tools is
:mod:`runtime.adapters.dbus_job_client`.
"""

from __future__ import annotations

from collections.abc import Mapping

from runtime.ports.event_bus import IEventPublisher, IJobRegistry
from runtime.ports.jobs import IJobClient

__all__ = ["InProcessJobClient"]


class InProcessJobClient(IJobClient):
    """``IJobClient`` over a registry + publisher (no transport)."""

    def __init__(self, registry: IJobRegistry, publisher: IEventPublisher) -> None:
        self._registry = registry
        self._publisher = publisher

    def begin(self, kind: str, ttl: float) -> str:
        return self._registry.begin(kind, ttl)

    def renew(self, job_id: str) -> None:
        self._registry.renew(job_id)

    def report_progress(self, job_id: str, fraction: float) -> None:
        self._registry.report_progress(job_id, fraction)

    def end(self, job_id: str, exit_code: int) -> None:
        self._registry.end(job_id, exit_code)

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        self._publisher.publish(topic, payload)
