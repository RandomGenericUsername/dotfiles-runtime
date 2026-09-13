"""Recording indicator — the bar's ``capture.state`` consumer (Phase 5, 5-4).

A Python-side harness for the AGS recording widget (there is no JS test
runner in this repo, so the GJS binding is exercised here). It binds a
``capture.state`` subscription through the shared :class:`IEventSubscriber`
port and renders the **pushed** ``state``/``elapsed_seconds``; it never
reads a status file and never polls (AD-40). Between pushes it may
interpolate locally from a monotonic clock plus the recorded push time,
which is exactly what the contract notes permit for continuous display.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from runtime.ports.event_bus import IEventSubscriber

__all__ = ["IndicatorView", "RecordingIndicator"]

_TOPIC = "capture.state"
_VALID_STATES = frozenset({"idle", "recording", "paused"})


@dataclass(frozen=True, slots=True)
class IndicatorView:
    """Rendered recording-indicator state (pure value)."""

    state: str
    elapsed_seconds: int
    visible: bool


class RecordingIndicator:
    """Renders the pushed ``capture.state`` without touching the filesystem.

    The subscriber is the transport-agnostic ``IEventSubscriber`` the hub
    already implements; the clock is injected so interpolation is testable
    and monotonic.
    """

    def __init__(self, subscriber: IEventSubscriber, *, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._state = "idle"
        self._base_elapsed = 0
        self._synced_at = clock()
        subscriber.subscribe(_TOPIC, self._on_event)

    def _on_event(self, topic: str, payload: Mapping[str, object]) -> None:
        if topic != _TOPIC:
            return
        state = payload.get("state")
        if not isinstance(state, str) or state not in _VALID_STATES:
            return
        elapsed = payload.get("elapsed_seconds")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
            elapsed = 0
        self._base_elapsed = max(0, int(elapsed))
        self._state = state
        self._synced_at = self._clock()

    def snapshot(self) -> IndicatorView:
        """Current view; interpolates only while recording (never a file read)."""
        elapsed = self._base_elapsed
        if self._state == "recording":
            elapsed += max(0, int(self._clock() - self._synced_at))
        return IndicatorView(
            state=self._state,
            elapsed_seconds=elapsed,
            visible=self._state != "idle",
        )
