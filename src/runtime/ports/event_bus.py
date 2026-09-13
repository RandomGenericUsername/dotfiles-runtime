"""Event-bus ports — the runtime side of the shared event contract (Phase 5).

``IEventPublisher`` / ``IEventSubscriber`` are the transport-agnostic seam
mandated by the spine structural seed: the core publishes and subscribes
through these ABCs, and P5-1-2b binds them to the session-D-Bus adapter
behind ``org.dotfiles.Events1`` (methods, signals, and payload shapes pinned
by ``contracts/event-contract.*``). ``IJobRegistry`` is the hub port the
daemon consumes: job lifecycle + leases + control + hydration.

Domain purity (AD-1, AD-14):
- This module is a port: ABCs only, no I/O. The domain registry
  (:mod:`runtime.domain.hub`) stays independent — it never imports this
  module (domain→ports is forbidden); adapters bridge the two.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping

__all__ = ["IEventPublisher", "IEventSubscriber", "IJobRegistry"]


class IEventPublisher(ABC):
    """Publish a domain event on a contract topic (2b: hub ``Emit``)."""

    @abstractmethod
    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        """Publish ``payload`` on ``topic``.

        Raises:
            UnknownTopic: the topic is not in the contract topics table.
            PayloadTooLarge: the payload exceeds the structural caps.
            RateLimited: the producer exceeded its publish rate.
        """


class IEventSubscriber(ABC):
    """Subscribe to contract topics with subscribe-before-read hydration."""

    @abstractmethod
    def subscribe(self, topic: str, handler: Callable[[str, Mapping[str, object]], None]) -> None:
        """Register ``handler(topic, payload)`` for future publications."""


class IJobRegistry(ABC):
    """Hub job lifecycle, leases, control, and hydration (2b: wire methods).

    Wire-name mapping (``contracts/event-contract.json`` `methods`):
    ``begin``→``BeginJob(kind, ttl)``, ``renew``→``RenewJob(job_id)``,
    ``adopt``→``AdoptJob(job_id, pid)``, ``report_progress``→
    ``ReportProgress(job_id, fraction)``, ``end``→``EndJob(job_id,
    exit_code)``, ``control``→``Control(job_id, action)``,
    ``active_jobs``→``GetActiveJobs() → {job_id: kind}``.

    Widths are a 2b concern: the domain takes ``float`` ttls and unbounded
    ints, and 2b validates them against the wire types (``ttl:u``,
    ``pid:u``, ``exit_code:i``) at dispatch.
    """

    @property
    @abstractmethod
    def epoch(self) -> int:
        """This start's epoch (bumped per start, never reused)."""

    @abstractmethod
    def begin(self, kind: str, ttl: float) -> str:
        """Allocate a live job; return its opaque unique ``job_id``."""

    @abstractmethod
    def renew(self, job_id: str) -> None:
        """Extend a live job's lease by its original ttl.

        Raises:
            UnknownJob: never seen (or registry reset).
            JobEnded: ended or lease-expired.
        """

    @abstractmethod
    def adopt(self, job_id: str, pid: int) -> None:
        """Record the observed child PID (AD-37 transitive ownership)."""

    @abstractmethod
    def report_progress(self, job_id: str, fraction: float) -> None:
        """Record progress within 0.0–1.0 (out of range: ``ValueError``)."""

    @abstractmethod
    def end(self, job_id: str, exit_code: int) -> None:
        """Mark a job ended (second end: ``JobEnded``)."""

    @abstractmethod
    def control(self, job_id: str, action: str) -> None:
        """Check an action against the kind allowlist (``NotControllable``)."""

    @abstractmethod
    def active_jobs(self) -> dict[str, str]:
        """``{job_id: kind}`` for live jobs only (hydration path)."""
