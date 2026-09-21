"""Capture controller — the resident capture lifetime job (Phase 5, AD-37).

The controller owns the recorder child (via :class:`IRecorderProcess`) and
reports through the hub's methods (via :class:`IJobClient`): ``BeginJob``
when recording starts, ``RenewJob`` + ``capture.state`` on every cadence
tick, and ``EndJob`` on stop. The hub — never this module — emits the
``JobStarted``/``JobFinished`` lifecycle signals and the ``DomainEvent``
signal for ``capture.state`` (AD-38).

Elapsed time is **monotonic**: a recorded start (or the accumulated time
of completed segments) plus an injected monotonic clock. It is never read
from a file or a shared wall clock (AD-40 "State vs polling"), so the bar
and the controller cannot disagree through a stale state file.

A finite ``duration`` (seconds, ``0`` = infinite) arms an auto-stop: the
first ``tick`` at or past the deadline stops the recorder, finalizes the
file, and reports completion exactly as a manual stop (same ``stop()``,
same ``idle`` emit, same ``EndJob(0)``). Paused time never counts toward
the deadline — only recorded elapsed does.

``capture.state`` is emitted on every transition AND at least once per
``cadence`` (default one second) while recording. The payload carries the
contract fields ``state``/``elapsed_seconds`` plus an additive optional
``job_id`` (AD-34: additive fields are non-breaking) so a consumer can
route ``Control`` to the right job without a second read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from runtime.ports.jobs import IJobClient, IRecorderProcess

__all__ = ["CaptureController"]

logger = logging.getLogger(__name__)

#: Default lease length (seconds); the serve loop renews every cadence.
DEFAULT_CAPTURE_TTL = 3600.0

#: Default cadence (seconds): the contract requires >= 1 emit per second.
DEFAULT_CAPTURE_CADENCE = 1.0

_STATE_TOPIC = "capture.state"
_JOB_STARTED_STATE = "recording"


class CaptureController:
    """Pure state machine + cadence driver for a resident capture job.

    The clock, the hub client, and the recorder child are all injected, so
    the whole lifecycle is testable without a bus, a timer, or a recorder.
    ``serve`` runs the blocking resident loop; callers may also drive
    ``tick`` directly.
    """

    def __init__(
        self,
        client: IJobClient,
        recorder: IRecorderProcess,
        *,
        clock: Callable[[], float],
        ttl: float = DEFAULT_CAPTURE_TTL,
        cadence: float = DEFAULT_CAPTURE_CADENCE,
        duration: float = 0.0,
    ) -> None:
        if ttl <= 0:
            raise ValueError(f"capture ttl must be > 0, got {ttl!r}")
        if cadence <= 0:
            raise ValueError(f"capture cadence must be > 0, got {cadence!r}")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise ValueError(f"capture duration must be a number, got {duration!r}")
        if duration < 0:
            raise ValueError(f"capture duration must be >= 0, got {duration!r}")
        self._client = client
        self._recorder = recorder
        self._clock = clock
        self._ttl = float(ttl)
        self._cadence = float(cadence)
        self._duration = float(duration)
        self._job_id: str | None = None
        self._state = "idle"
        self._accumulated = 0.0
        self._segment_start: float | None = None
        self._last_emit: float | None = None

    # ── Observability ────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Current contract state: ``idle`` | ``recording`` | ``paused``."""
        return self._state

    @property
    def job_id(self) -> str | None:
        """The hub-allocated job id while a recording is live."""
        return self._job_id

    @property
    def duration(self) -> float:
        """Configured auto-stop deadline in seconds (``0.0`` = infinite)."""
        return self._duration

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> str:
        """Begin the job + recorder and emit the ``recording`` transition."""
        if self._state != "idle":
            raise RuntimeError(f"capture already active in state {self._state!r}")
        job_id = self._client.begin("capture", self._ttl)
        self._job_id = job_id
        try:
            self._recorder.start()
        except BaseException:
            # Never leak the hub lease: the recorder failed after BeginJob, so
            # end the just-allocated job (exit 1) before re-raising. Without
            # this a dead recorder would hold a live job until its TTL while
            # the host's ``stop`` (state still ``idle``) raised a masking
            # error — 5-4 review, lease-leak-on-recorder-failure.
            self._job_id = None
            try:
                self._client.end(job_id, 1)
            except Exception:
                logger.exception("capture: EndJob failed after recorder start error")
            raise
        now = self._clock()
        self._state = _JOB_STARTED_STATE
        self._segment_start = now
        self._accumulated = 0.0
        self._publish_state(now)
        return job_id

    def pause(self) -> None:
        """Pause the recorder and emit the ``paused`` transition."""
        if self._state != "recording":
            raise RuntimeError(f"cannot pause capture in state {self._state!r}")
        now = self._clock()
        self._accumulate(now)
        self._recorder.pause()
        self._state = "paused"
        self._publish_state(now)

    def resume(self) -> None:
        """Resume the recorder and emit the ``recording`` transition."""
        if self._state != "paused":
            raise RuntimeError(f"cannot resume capture in state {self._state!r}")
        self._recorder.resume()
        now = self._clock()
        self._segment_start = now
        self._state = _JOB_STARTED_STATE
        self._publish_state(now)

    def stop(self, exit_code: int = 0) -> None:
        """Stop the recorder, emit ``idle``, and end the hub job.

        ``exit_code`` (default 0) is the hub ``EndJob`` code: a nonzero
        code marks an abnormal end (e.g. the recorder died mid-job) while
        still releasing the recorder and the lease exactly once.
        """
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError(f"capture exit code must be an int, got {exit_code!r}")
        if self._state == "idle":
            raise RuntimeError("cannot stop capture: no active recording")
        now = self._clock()
        self._accumulate(now)
        self._recorder.stop()
        self._state = "idle"
        self._publish_state(now)
        job_id = self._job_id
        self._job_id = None
        if job_id is not None:
            try:
                self._client.end(job_id, exit_code)
            except Exception:
                logger.exception("capture: EndJob failed for %s; continuing", job_id)

    def control(self, job_id: str, action: str) -> None:
        """Deliver a hub ``Control`` action to this job (single control path).

        The hub validates the action against the kind allowlist before this
        runs; the controller only checks identity + transition validity.
        """
        if self._job_id is None or job_id != self._job_id:
            raise RuntimeError(f"control for unknown capture job {job_id!r}")
        if action == "pause":
            self.pause()
        elif action == "resume":
            self.resume()
        elif action == "stop":
            self.stop()
        else:  # pragma: no cover - the hub allowlist rejects these first
            raise ValueError(f"unsupported capture action {action!r}")

    # ── Cadence ──────────────────────────────────────────────────────

    def tick(self, now: float | None = None) -> bool:
        """Emit + renew if a cadence interval elapsed; return whether emitted.

        Only ticks while recording. A finite ``duration`` deadline is checked
        first: on expiry the controller stops exactly like a manual stop
        (recorder stopped, file finalized, ``idle`` emitted, job ended 0).
        A failed publish/renew is contained: the
        at-most-once contract permits a dropped signal and the bar
        interpolates locally, so a transient hub failure never stops the
        recording (AD-41 recoverable flavor).
        """
        current = self._clock() if now is None else now
        if self._state != _JOB_STARTED_STATE:
            return False
        if self._duration > 0 and self._elapsed(current) >= self._duration:
            self.stop()
            return True
        if self._last_emit is not None and current - self._last_emit < self._cadence:
            return False
        self._publish_state(current)
        if self._job_id is not None:
            try:
                self._client.renew(self._job_id)
            except Exception:
                logger.exception("capture: RenewJob failed for %s; continuing", self._job_id)
        return True

    def serve(self, stop_requested: Callable[[], bool], sleep: Callable[[float], None]) -> None:
        """Resident blocking loop: tick + sleep until ``stop_requested``.

        ``sleep`` is injected so the loop is testable without real time;
        production passes ``time.sleep`` and a signal-backed stop predicate.
        """
        while not stop_requested():
            self.tick()
            if stop_requested():
                break
            sleep(self._cadence)

    # ── Internals ────────────────────────────────────────────────────

    def _accumulate(self, now: float) -> None:
        if self._state == _JOB_STARTED_STATE and self._segment_start is not None:
            self._accumulated += max(0.0, now - self._segment_start)
        self._segment_start = None

    def _elapsed(self, now: float) -> int:
        total = self._accumulated
        if self._state == _JOB_STARTED_STATE and self._segment_start is not None:
            total += max(0.0, now - self._segment_start)
        return max(0, int(total))

    def _publish_state(self, now: float) -> None:
        if self._job_id is None:
            return
        payload: dict[str, object] = {
            "state": self._state,
            "elapsed_seconds": self._elapsed(now),
            "job_id": self._job_id,
        }
        try:
            self._client.publish(_STATE_TOPIC, payload)
        except Exception:
            logger.exception("capture: capture.state publish failed; continuing")
        self._last_emit = now
