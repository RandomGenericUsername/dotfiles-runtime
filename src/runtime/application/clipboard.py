"""Clipboard controller — the resident clipboard lifetime job (Phase 5, D1).

Mirrors :mod:`runtime.application.capture`: the controller owns the injected
:class:`IClipboardSource` and :class:`IClipboardStore` and reports through the
injected :class:`IJobClient` (``BeginJob`` / ``RenewJob`` / ``EndJob`` /
``Emit``). The hub — never this module — emits the lifecycle signals and the
``DomainEvent`` carrying ``clipboard.update`` / ``clipboard.state`` (AD-38).

State is ``idle`` / ``running`` / ``paused``. Incognito is the ``paused``
state: while paused a captured reading is discarded and nothing is stored or
published, so a password copied during incognito never reaches history.

The controller is transport-agnostic and fully testable: clock, source, store,
config, and client are injected, so there is no bus, no compositor, and no
real time in the tests.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from runtime.adapters.hashing import hash_file_bytes
from runtime.domain.clipboard import (
    ClipboardItem,
    ClipboardReading,
    RetentionLimits,
    classify,
)
from runtime.ports.clipboard import IClipboardConfigReader, IClipboardSource, IClipboardStore
from runtime.ports.jobs import IJobClient

__all__ = ["ClipboardController", "DEFAULT_CLIPBOARD_TTL", "DEFAULT_RENEW_INTERVAL"]

logger = logging.getLogger(__name__)

#: Default lease length (seconds); renewed every ``renew_interval``.
DEFAULT_CLIPBOARD_TTL = 3600.0

#: Default lease-renewal interval (seconds).
DEFAULT_RENEW_INTERVAL = 60.0

_STATE_TOPIC = "clipboard.state"
_UPDATE_TOPIC = "clipboard.update"
_JOB_KIND = "clipboard"


class ClipboardController:
    """Pure state machine + drain loop for the resident clipboard job."""

    def __init__(
        self,
        client: IJobClient,
        source: IClipboardSource,
        store: IClipboardStore,
        config: IClipboardConfigReader,
        *,
        clock: Callable[[], float],
        ttl: float = DEFAULT_CLIPBOARD_TTL,
        renew_interval: float = DEFAULT_RENEW_INTERVAL,
    ) -> None:
        if ttl <= 0:
            raise ValueError(f"clipboard ttl must be > 0, got {ttl!r}")
        if renew_interval <= 0:
            raise ValueError(f"clipboard renew interval must be > 0, got {renew_interval!r}")
        self._client = client
        self._source = source
        self._store = store
        self._config = config
        self._clock = clock
        self._ttl = float(ttl)
        self._renew_interval = float(renew_interval)
        self._job_id: str | None = None
        self._state = "idle"
        self._limits: RetentionLimits = RetentionLimits()
        self._last_renew: float | None = None

    # ── Observability ────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Current contract state: ``idle`` | ``running`` | ``paused``."""
        return self._state

    @property
    def job_id(self) -> str | None:
        """The hub-allocated job id while the watcher is live."""
        return self._job_id

    @property
    def source_mode(self) -> str:
        """The source's declared mode (``protocol`` or the degraded ``polling``)."""
        return self._source.mode

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> str:
        """Read limits, begin the job, start the source, announce running."""
        if self._state != "idle":
            raise RuntimeError(f"clipboard already active in state {self._state!r}")
        self._limits = self._config.read()
        job_id = self._client.begin(_JOB_KIND, self._ttl)
        self._job_id = job_id
        try:
            self._source.start()
        except BaseException:
            # Never leak the hub lease if the source failed after BeginJob.
            self._job_id = None
            try:
                self._client.end(job_id, 1)
            except Exception:
                logger.exception("clipboard: EndJob failed after source start error")
            raise
        self._state = "running"
        self._last_renew = self._clock()
        self._publish_state()
        return job_id

    def pause(self) -> None:
        """Enter incognito: stop storing/publishing captured content."""
        if self._state != "running":
            raise RuntimeError(f"cannot pause clipboard in state {self._state!r}")
        self._state = "paused"
        self._publish_state()

    def resume(self) -> None:
        """Leave incognito and resume capture."""
        if self._state != "paused":
            raise RuntimeError(f"cannot resume clipboard in state {self._state!r}")
        self._state = "running"
        self._publish_state()

    def stop(self) -> None:
        """Stop the source, announce idle, and end the hub job (exit 0)."""
        if self._state == "idle":
            raise RuntimeError("cannot stop clipboard: not active")
        self._state = "idle"
        self._publish_state()
        try:
            self._source.stop()
        except Exception:
            logger.exception("clipboard: source stop failed; continuing")
        job_id = self._job_id
        self._job_id = None
        if job_id is not None:
            try:
                self._client.end(job_id, 0)
            except Exception:
                logger.exception("clipboard: EndJob failed for %s; continuing", job_id)

    def control(self, job_id: str, action: str) -> None:
        """Deliver a hub ``Control`` action (the single control path)."""
        if self._job_id is None or job_id != self._job_id:
            raise RuntimeError(f"control for unknown clipboard job {job_id!r}")
        if action == "pause":
            self.pause()
        elif action == "resume":
            self.resume()
        elif action == "stop":
            self.stop()
        else:  # pragma: no cover - the hub allowlist rejects these first
            raise ValueError(f"unsupported clipboard action {action!r}")

    # ── Draining ─────────────────────────────────────────────────────

    def tick(self, now: float | None = None) -> bool:
        """Drain one reading and renew the lease on cadence; return handled.

        A failed store/publish/renew is contained: capture continues and the
        next change is still observed (AD-41 recoverable flavor).
        """
        current = self._clock() if now is None else now
        handled = False
        if self._state != "idle":
            try:
                reading = self._source.next_change(0.0)
            except Exception:
                logger.exception("clipboard: source read failed; continuing")
                reading = None
            if reading is not None and self._state == "running":
                handled = self.handle_reading(reading, current)
        if self._state != "idle" and self._should_renew(current):
            if self._job_id is not None:
                try:
                    self._client.renew(self._job_id)
                except Exception:
                    logger.exception("clipboard: RenewJob failed for %s; continuing", self._job_id)
            self._last_renew = current
        return handled

    def handle_reading(self, reading: ClipboardReading, now: float | None = None) -> bool:
        """Classify, store, evict, and publish one captured reading."""
        timestamp = self._clock() if now is None else now
        digest = hash_file_bytes(reading.canonical_bytes())
        kind = classify(reading.mimetypes, reading.text)
        item = ClipboardItem(
            hash=digest,
            kind=kind,
            timestamp=timestamp,
            text=reading.text if kind != "image" else None,
            path=reading.image_path if kind == "image" else None,
        )
        try:
            stored = self._store.add(item)
        except Exception:
            logger.exception("clipboard: store add failed; dropping this reading")
            return False
        try:
            self._store.evict(self._limits)
        except Exception:
            logger.exception("clipboard: eviction failed; continuing")
        try:
            self._client.publish(_UPDATE_TOPIC, stored.to_payload())
        except Exception:
            logger.exception("clipboard: clipboard.update publish failed; continuing")
        return True

    def _should_renew(self, now: float) -> bool:
        if self._last_renew is None:
            return True
        return now - self._last_renew >= self._renew_interval

    def _publish_state(self) -> None:
        if self._job_id is None:
            return
        payload = {"state": self._state, "job_id": self._job_id}
        try:
            self._client.publish(_STATE_TOPIC, payload)
        except Exception:
            logger.exception("clipboard: clipboard.state publish failed; continuing")
