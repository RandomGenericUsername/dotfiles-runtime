"""Host-event coalescing + watch-health aggregation (H-3.4).

The host event transport (Hyprland socket2) is injected as an
:class:`IHostEventSource`; this module owns the decision logic:

- **Debounce**: a burst of ``configreloaded`` events is collected and fires
  **one** trigger when the source reports a quiet window (``read_event``
  returns ``None``). There is no fixed sleep and no state poll — the source's
  blocking read is the only clock.
- **Shutdown**: a burst still open at stop is intentionally not flushed; the
  daemon is stopping, and the next start converges from the current state.

:class:`WatchStatusAggregator` merges the inotify watch-set health and the
host event-stream health into the one persisted ``watch-health.json`` record,
so an unavailable socket is visible without being desktop drift.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from runtime.application.watch import WatchTrigger
from runtime.domain.watch import WatchStatus
from runtime.ports.host_event_source import IHostEventSource

logger = logging.getLogger(__name__)

#: Default quiet window: how long the host socket must report no event before
#: a collected burst is fired as one trigger. Not a poll interval.
DEFAULT_HOST_DEBOUNCE_SECONDS = 0.25


class HostEventCoordinator:
    """Bridge an :class:`IHostEventSource` to a coalesced converge callback."""

    def __init__(
        self,
        source: IHostEventSource,
        on_trigger: Callable[[WatchTrigger], None],
        *,
        debounce: float = DEFAULT_HOST_DEBOUNCE_SECONDS,
        on_status: Callable[[WatchStatus], None] | None = None,
    ) -> None:
        if debounce <= 0:
            raise ValueError(f"debounce must be positive, got {debounce!r}")
        self._source = source
        self._on_trigger = on_trigger
        self._debounce = debounce
        self._on_status = on_status
        self._last_status: WatchStatus | None = None

    def serve_events(self, stop: threading.Event) -> None:
        """Blocking loop: coalesce host events until ``stop`` is set."""
        self._report_status()
        pending = False
        while not stop.is_set():
            event = self._source.read_event(self._debounce)
            if event is None:
                if pending:
                    pending = False
                    self._fire()
                continue
            pending = True
            self._report_status()

    def _report_status(self) -> None:
        if self._on_status is None:
            return
        status = self._source.status()
        if status == self._last_status:
            return
        self._last_status = status
        try:
            self._on_status(status)
        except Exception:
            logger.exception("host-watch: watch-status callback failed; continuing")

    def _fire(self) -> None:
        trigger = WatchTrigger(reason="configreloaded", full_rescan=False)
        try:
            self._on_trigger(trigger)
        except Exception:
            logger.exception("host-watch: configreloaded converge failed; continuing")


class WatchStatusAggregator:
    """Merge per-source health into one persisted watch-status record.

    Each source reports only its own registrations/failures; the daemon owns
    the one ``watch-health.json`` file. A failure in any source degrades the
    merged record; optional roots are informational.
    """

    def __init__(self, write: Callable[[WatchStatus], None]) -> None:
        self._write = write
        self._lock = threading.Lock()
        self._statuses: dict[str, WatchStatus] = {}

    def update(self, source: str, status: WatchStatus) -> None:
        with self._lock:
            self._statuses[source] = status
            merged = self._merge()
        try:
            self._write(merged)
        except Exception:
            logger.exception("host-watch: persisting merged watch health failed")

    def _merge(self) -> WatchStatus:
        statuses = list(self._statuses.values())
        failed = tuple(sorted({root for status in statuses for root in status.failed}))
        optional_absent = tuple(
            sorted({root for status in statuses for root in status.optional_absent})
        )
        last_error = next(
            (status.last_error for status in statuses if status.last_error),
            None,
        )
        return WatchStatus(
            registered=sum(status.registered for status in statuses),
            failed=failed,
            last_error=last_error,
            optional_absent=optional_absent,
        )

    @property
    def status(self) -> WatchStatus:
        with self._lock:
            return self._merge()
