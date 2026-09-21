"""Lifetime-job ports — the tool side of the hub contract (Phase 5, AD-37).

A lifetime job (capture recorder, speed test) owns the observed process and
reports through the hub's methods; the **hub** is the sole emitter (AD-38).
These ABCs are the transport-agnostic seams the job controllers use:

- :class:`IJobClient` — begin/renew/report/end plus ``publish``. The
  in-process adapter binds it to a registry + publisher; the D-Bus adapter
  binds it to the hub's ``BeginJob``/``RenewJob``/``ReportProgress``/
  ``EndJob``/``Emit`` methods. A job NEVER emits a signal directly.
- :class:`IControlChannel` — the hub-side seam that delivers a validated
  ``Events1.Control`` to the job as ``Job1.Control`` (request/response; the
  hub is the single control path). The D-Bus adapter calls the job's
  bus-attested unique name; the in-process adapter calls a co-hosted job.
- :class:`IRecorderProcess` — the observed recorder child (start/pause/
  resume/stop/is_running).
- :class:`ISpeedTestRunner` — one measurement; the real adapter shells the
  backend, tests inject a fake (no network).

Ports are ABCs only (layering rule); concrete adapters live in
``runtime.adapters``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping

from runtime.domain.jobs import SpeedTestResult

__all__ = [
    "IControlChannel",
    "IControllableJobClient",
    "IJobClient",
    "IRecorderProcess",
    "ISpeedTestRunner",
]


class IJobClient(ABC):
    """The job's method surface onto the hub (report-only; never a signal)."""

    @abstractmethod
    def begin(self, kind: str, ttl: float) -> str:
        """Allocate a live job; return its hub-allocated ``job_id``."""

    @abstractmethod
    def renew(self, job_id: str) -> None:
        """Extend the job's lease by its original ttl (lease keep-alive)."""

    @abstractmethod
    def report_progress(self, job_id: str, fraction: float) -> None:
        """Report progress within 0.0–1.0."""

    @abstractmethod
    def end(self, job_id: str, exit_code: int) -> None:
        """Mark the job ended with its exit code."""

    @abstractmethod
    def publish(self, topic: str, payload: Mapping[str, object]) -> None:
        """Publish a DOMAIN event on ``topic`` through the hub (AD-38)."""


class IControllableJobClient(IJobClient, ABC):
    """A job client that can serve the hub's delegated ``Job1.Control`` (5-4).

    The out-of-process capture host binds its controller's pause/resume/stop
    through this seam; the D-Bus adapter serves ``org.dotfiles.Job1`` on the
    same connection used for ``BeginJob`` and the degraded local adapter
    accepts-and-ignores it (no remote caller). A job that does not accept
    control (e.g. the speed test) is a plain :class:`IJobClient`.
    """

    @abstractmethod
    def set_control_handler(self, handler: Callable[[str, str], None]) -> None:
        """Bind ``handler(job_id, action)`` for inbound ``Job1.Control`` calls."""


class IControlChannel(ABC):
    """Hub-side delivery of a validated control action to the job (5-4, H1).

    The hub validates the action against the domain allowlist BEFORE calling
    this; the channel only delivers the already-validated action and reports
    the outcome. ``job_id`` identifies the job whose endpoint the concrete
    adapter resolved (bus-attested unique name, or an in-process handler).
    """

    @abstractmethod
    def send_control(self, job_id: str, action: str) -> None:
        """Deliver ``action`` to the job bound to ``job_id``.

        Returns normally **only on the job's ack**. Raises ``UnknownJob`` when
        no live endpoint is bound, or the endpoint is unreachable/dead, or the
        call times out; a typed error the job returns crosses through
        unchanged. Never reports success for a delivery that did not happen.
        """


class IRecorderProcess(ABC):
    """The recorder child a capture job owns (AD-37 transitive ownership)."""

    @abstractmethod
    def start(self) -> None:
        """Spawn the recorder; raise loudly if it exits during startup."""

    @abstractmethod
    def pause(self) -> None:
        """Pause the running recorder."""

    @abstractmethod
    def resume(self) -> None:
        """Resume a paused recorder."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the recorder and release the child (idempotent)."""

    @abstractmethod
    def is_running(self) -> bool:
        """True while the observed child is alive."""

    def ensure_alive(self) -> bool:
        """Return True when the recorder is capturing; restart if it can.

        The default (a plain single child) is just :meth:`is_running`. A
        resilient recorder may transparently restart the observed child and
        still return ``True`` — the host's health check treats ``False`` as
        an unexpected death. Overriding this (rather than ``is_running``)
        keeps the pure liveness query side-effect-free.
        """
        return self.is_running()

    def exit_status(self) -> int | None:
        """Return the child's exit status once it has exited, else ``None``.

        Diagnostics only: the host never branches on it, but the CLI surfaces
        it when a recording ends abnormally (a negative value is a signal).
        """
        return None


class ISpeedTestRunner(ABC):
    """One speed-test measurement (no network in tests)."""

    @abstractmethod
    def measure(self) -> SpeedTestResult:
        """Run the measurement and return the contract-typed result."""
