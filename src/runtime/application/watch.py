"""Watch coalescing + recovery coordinator (AD-39/AD-40).

The transport (inotify) is injected as an :class:`IWatchSource`; this module
owns the *decision* logic:

- **Coalesce/debounce**: a burst of events is collected and fires **one**
  trigger when the source reports a quiet window (``read_event`` returns
  ``None``). There is no fixed sleep and no state polling — the source is
  the only thing consulted.
- **Overflow** (``IN_Q_OVERFLOW``): force a full re-scan trigger.
- **Registration loss** (``IN_IGNORED`` / ``IN_MOVE_SELF`` /
  ``IN_DELETE_SELF``): re-establish the watches via ``source.rebuild()``
  and force a full re-scan trigger.
- Triggering is recoverable: a raising converge callback is logged and the
  watch loop continues (AD-41 fatal-vs-recoverable).

The coordinator makes no filesystem calls itself — a "no polling" invariant
test can assert it never stats or rescans a root outside the two forced
conditions above.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from runtime.domain.watch import (
    CONTENT_CHANGE,
    REGISTRATION_LOSS,
    WatchEvent,
    WatchEventKind,
)
from runtime.ports.watch_source import IWatchSource

logger = logging.getLogger(__name__)

#: Default quiet window: how long the source must report no event before a
#: collected burst is fired as one trigger. Not a poll interval.
DEFAULT_DEBOUNCE_SECONDS = 0.25


@dataclass(frozen=True, slots=True)
class WatchTrigger:
    """One coalesced trigger handed to the converge callback."""

    reason: str
    full_rescan: bool


class WatchAccumulator:
    """Pure burst accumulator: ingest events, take at most one trigger."""

    __slots__ = ("_pending", "_full_rescan", "_reason")

    def __init__(self) -> None:
        self._pending = False
        self._full_rescan = False
        self._reason = "change"

    def ingest(self, event: WatchEvent) -> bool:
        """Record one event; return True when watches must be rebuilt.

        A registration loss raises the rebuild flag and is *once-per-burst*:
        the coordinator calls ``source.rebuild()`` immediately, and the
        resulting full re-scan is carried on the single trigger.
        """
        if event.kind is WatchEventKind.OVERFLOW:
            self._pending = True
            self._full_rescan = True
            self._reason = "overflow"
            return False
        if event.kind in REGISTRATION_LOSS:
            self._pending = True
            self._full_rescan = True
            self._reason = "registration-loss"
            return True
        if event.kind in CONTENT_CHANGE:
            self._pending = True
        return False

    def take(self) -> WatchTrigger | None:
        """Return the coalesced trigger (if any) and reset the accumulator."""
        if not self._pending:
            return None
        trigger = WatchTrigger(reason=self._reason, full_rescan=self._full_rescan)
        self._pending = False
        self._full_rescan = False
        self._reason = "change"
        return trigger


class WatchCoordinator:
    """Bridge an :class:`IWatchSource` to a coalesced converge callback."""

    def __init__(
        self,
        source: IWatchSource,
        on_trigger: Callable[[WatchTrigger], None],
        *,
        debounce: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        if debounce <= 0:
            raise ValueError(f"debounce must be positive, got {debounce!r}")
        self._source = source
        self._on_trigger = on_trigger
        self._debounce = debounce

    def serve_events(self, stop: threading.Event) -> None:
        """Blocking loop: coalesce events until ``stop`` is set.

        Called on a dedicated thread by the daemon. Each iteration blocks in
        ``source.read_event`` (never a timed state poll); a ``None`` result is
        the quiet-window signal that releases one accumulated trigger.
        """
        accumulator = WatchAccumulator()
        while not stop.is_set():
            event = self._source.read_event(self._debounce)
            if event is None:
                trigger = accumulator.take()
                if trigger is not None:
                    self._fire(trigger)
                continue
            if accumulator.ingest(event):
                try:
                    self._source.rebuild()
                except Exception:
                    logger.exception("watch: rebuilding watches failed; continuing")
        # Flush a burst that was still open when stop arrived? Intentionally
        # no: shutdown must not start a long converge the supervisor is about
        # to kill. The change is caught by converge-on-start next time.

    def _fire(self, trigger: WatchTrigger) -> None:
        """Invoke the converge callback; failures are recoverable (AD-41)."""
        try:
            self._on_trigger(trigger)
        except Exception:
            logger.exception(
                "watch: converge trigger (%s, full_rescan=%s) failed; continuing",
                trigger.reason,
                trigger.full_rescan,
            )
