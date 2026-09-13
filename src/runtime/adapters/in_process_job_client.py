"""In-process job client + control channel — a job over a co-hosted hub (5-4).

Binds :class:`IJobClient` to an :class:`IJobRegistry` (hub methods) and an
:class:`IEventPublisher` (validated ``Emit``). This is the hermetic seam the
controller/speed-test tests use, and the composition the daemon uses when it
hosts a lifetime job in-process.

:class:`InProcessControlChannel` is the in-process arm of the hub→job control
channel: the job client binds a handler per ``job_id`` on ``begin`` and drops
it on ``end``; the hub's ``Control`` resolves the same ``job_id`` and calls
the handler. No bound handler is a loud ``UnknownJob`` (N2), exactly like the
D-Bus arm. The D-Bus equivalent for out-of-process tools is
:mod:`runtime.adapters.dbus_job_client`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping

from runtime.domain.models import UnknownJob
from runtime.ports.event_bus import IEventPublisher, IJobRegistry
from runtime.ports.jobs import IControlChannel, IControllableJobClient

__all__ = ["InProcessControlChannel", "InProcessJobClient"]


class InProcessControlChannel(IControlChannel):
    """``IControlChannel`` for a co-hosted job (tests / in-process host).

    Handlers are registered per ``job_id`` by :class:`InProcessJobClient` on
    ``begin`` and removed on ``end``. ``send_control`` for an unknown or ended
    job raises ``UnknownJob`` — never a silent success.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[str, str], None]] = {}
        self._lock = threading.Lock()

    def bind(self, job_id: str, handler: Callable[[str, str], None]) -> None:
        """Register the job's control handler for ``job_id``."""
        with self._lock:
            self._handlers[job_id] = handler

    def unbind(self, job_id: str) -> None:
        """Drop the job's control handler (EndJob)."""
        with self._lock:
            self._handlers.pop(job_id, None)

    def send_control(self, job_id: str, action: str) -> None:
        with self._lock:
            handler = self._handlers.get(job_id)
        if handler is None:
            raise UnknownJob(job_id)
        handler(job_id, action)


class InProcessJobClient(IControllableJobClient):
    """``IJobClient`` over a registry + publisher (no transport).

    When a control channel is injected, a handler set via
    :meth:`set_control_handler` is bound to each allocated ``job_id`` so a
    co-hosted hub can deliver ``Control`` through the same port as the D-Bus
    arm.
    """

    def __init__(
        self,
        registry: IJobRegistry,
        publisher: IEventPublisher,
        *,
        control: InProcessControlChannel | None = None,
    ) -> None:
        self._registry = registry
        self._publisher = publisher
        self._control = control
        self._handler: Callable[[str, str], None] | None = None

    def set_control_handler(self, handler: Callable[[str, str], None]) -> None:
        """Bind the handler ``handler(job_id, action)`` for future jobs."""
        if not callable(handler):
            raise ValueError(f"control handler must be callable, got {type(handler).__name__}")
        self._handler = handler

    def begin(self, kind: str, ttl: float) -> str:
        job_id = self._registry.begin(kind, ttl)
        if self._control is not None and self._handler is not None:
            self._control.bind(job_id, self._handler)
        return job_id

    def renew(self, job_id: str) -> None:
        self._registry.renew(job_id)

    def report_progress(self, job_id: str, fraction: float) -> None:
        self._registry.report_progress(job_id, fraction)

    def end(self, job_id: str, exit_code: int) -> None:
        self._registry.end(job_id, exit_code)
        if self._control is not None:
            self._control.unbind(job_id)

    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        self._publisher.publish(topic, payload)
