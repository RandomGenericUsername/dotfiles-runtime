"""Production capture job host (Phase 5, 5-4 follow-up N1, AD-37/AD-38/AD-40).

Composes the already-tested capture pieces into one resident foreground
host:

- :class:`~runtime.application.capture.CaptureController` — the state
  machine, cadence, lease renewal, and ``capture.state`` publishing;
- an injected :class:`~runtime.ports.jobs.IControllableJobClient` — the
  D-Bus arm in production (serves ``org.dotfiles.Job1.Control`` on the same
  connection it used for ``BeginJob``) or the degraded local arm when no
  hub is present;
- an injected :class:`~runtime.ports.jobs.IRecorderProcess` —
  ``SubprocessRecorder`` in production.

The host is transport-agnostic: it never imports jeepney or touches a bus
(AD-34) and never constructs a signal (AD-38). It owns the single stop
event: a validated ``Control("stop")`` (or a supervisor signal) requests
stop, the serving loop exits, and :meth:`stop` finalizes the recorder and
ends the hub job. ``tick`` is driven by the client's receive loop in
production (no polling); the degraded path simply blocks on the stop event.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from runtime.application.capture import (
    DEFAULT_CAPTURE_CADENCE,
    DEFAULT_CAPTURE_TTL,
    CaptureController,
)
from runtime.ports.jobs import IControllableJobClient, IRecorderProcess

__all__ = ["CaptureHost"]

logger = logging.getLogger(__name__)


class CaptureHost:
    """Owns the resident capture job + its stop lifecycle.

    ``clock`` (monotonic seconds), the client, the recorder, and the ttl /
    cadence are all injected so the lifecycle is fully testable without a
    bus, a recorder, or real time.
    """

    def __init__(
        self,
        client: IControllableJobClient,
        recorder: IRecorderProcess,
        *,
        clock: Callable[[], float],
        ttl: float = DEFAULT_CAPTURE_TTL,
        cadence: float = DEFAULT_CAPTURE_CADENCE,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._client = client
        self._controller = CaptureController(
            client, recorder, clock=clock, ttl=ttl, cadence=cadence
        )
        self._stop_event = stop_event if stop_event is not None else threading.Event()

    # ── Observability ────────────────────────────────────────────────

    @property
    def controller(self) -> CaptureController:
        """The underlying controller (for tests/inspection)."""
        return self._controller

    @property
    def job_id(self) -> str | None:
        """The hub-allocated job id while a recording is live."""
        return self._controller.job_id

    @property
    def state(self) -> str:
        """Current contract state: ``idle`` | ``recording`` | ``paused``."""
        return self._controller.state

    @property
    def stop_event(self) -> threading.Event:
        """The single stop event a signal handler / serve loop shares."""
        return self._stop_event

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> str:
        """Bind the control handler, begin the job, and start the recorder."""
        self._client.set_control_handler(self._control)
        return self._controller.start()

    def tick(self) -> bool:
        """Emit ``capture.state`` + renew the lease on a cadence boundary."""
        return self._controller.tick()

    def stop(self) -> None:
        """Stop the recorder and end the hub job (idempotent, never raises)."""
        if self._controller.job_id is None:
            return
        self._controller.stop()

    def request_stop(self) -> None:
        """Ask the serving loop to exit (signal handler / supervisor)."""
        self._stop_event.set()

    def stop_requested(self) -> bool:
        """True once a stop was requested (stop signal or ``Control("stop")``)."""
        return self._stop_event.is_set()

    def wait(self) -> None:
        """Block with no polling until a stop is requested (degraded path)."""
        self._stop_event.wait()

    # ── Internals ────────────────────────────────────────────────────

    def _control(self, job_id: str, action: str) -> None:
        """Route a hub ``Control`` action; ``stop`` also exits the host.

        The hub validates the action against the kind allowlist before this
        runs (single control path, AD-38). A stop ends the recording, so the
        resident serving loop is asked to exit too; the controller's own
        transition errors (unknown job / invalid action) propagate to the
        caller as the job's typed D-Bus error.
        """
        self._controller.control(job_id, action)
        if action == "stop":
            self._stop_event.set()
