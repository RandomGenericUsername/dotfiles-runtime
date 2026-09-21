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
        duration: float = 0.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._client = client
        self._recorder = recorder
        self._controller = CaptureController(
            client, recorder, clock=clock, ttl=ttl, cadence=cadence, duration=duration
        )
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._failed: str | None = None

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
        """Emit ``capture.state`` + renew the lease on a cadence boundary.

        A duration auto-stop ends the job inside the controller; the host
        then asks the serving loop to exit so the process finalizes and
        terminates exactly as after a manual stop.
        """
        job_id = self._controller.job_id
        emitted = self._controller.tick()
        if job_id is not None and self._controller.job_id is None:
            # The tick ended the job (duration auto-stop; a manual Control
            # stop already sets the event itself — this is idempotent).
            self._stop_event.set()
        return emitted

    def stop(self, exit_code: int = 0) -> None:
        """Stop the recorder and end the hub job (idempotent, never raises).

        A nonzero ``exit_code`` marks an abnormal end (recorder died
        mid-job); the recorder release is still idempotent (a dead child
        is a no-op) so the partial file is kept on disk. A recorder whose
        release raises (e.g. a failed segment join) is contained — the
        reason is recorded for the caller's finalize report.
        """
        if self._controller.job_id is None:
            return
        try:
            self._controller.stop(exit_code=exit_code)
        except Exception as exc:
            logger.exception("capture: recorder stop failed")
            self.mark_failed(f"recorder finalize failed: {exc}")

    @property
    def failed(self) -> str | None:
        """Failure reason when the recorder died unexpectedly, else None."""
        return self._failed

    def mark_failed(self, reason: str) -> None:
        """Record an unexpected recorder death (idempotent, first wins)."""
        if self._failed is None:
            self._failed = reason

    def check_health(self) -> bool:
        """True while the owned recorder is capturing for a live job.

        Call on every cadence tick before ``tick()``. A resilient recorder
        (``SegmentingRecorder``) restarts its child transparently here and
        still returns ``True``; a plain recorder whose child is gone is an
        unexpected death — mark it failed and ask the serving loop to exit
        so the host finalizes with a nonzero ``EndJob`` instead of
        publishing ever-growing ``recording`` elapsed for a dead file.
        """
        if self._controller.job_id is None or self._stop_event.is_set():
            return True
        try:
            alive = bool(self._recorder.ensure_alive())
        except Exception:
            logger.exception("capture: recorder health probe failed")
            alive = False
        if alive:
            return True
        status: int | None = None
        try:
            status = self._recorder.exit_status()
        except Exception:
            logger.exception("capture: recorder exit-status probe failed")
        reason = "recorder exited unexpectedly"
        if status is not None:
            reason = f"{reason} (status {status})"
        self.mark_failed(reason)
        self._stop_event.set()
        return False

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
