"""Clipboard job host — resident lifecycle + control for the watcher (D1).

Composes the tested clipboard pieces into one foreground host, mirroring
:mod:`runtime.application.capture_host`:

- :class:`~runtime.application.clipboard.ClipboardController` — the state
  machine, drain loop, lease renewal, and ``clipboard.update`` /
  ``clipboard.state`` publishing;
- an injected :class:`~runtime.ports.jobs.IControllableJobClient` — the D-Bus
  arm (serves ``org.dotfiles.Job1.Control``) or the degraded local arm;
- injected :class:`IClipboardSource` / :class:`IClipboardStore` /
  :class:`IClipboardConfigReader`.

The host is transport-agnostic (never imports jeepney, never constructs a
signal). It owns the single stop event: a validated ``Control("stop")`` or a
supervisor signal exits the loop, and :meth:`stop` finalizes the source and
ends the hub job.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from runtime.application.clipboard import (
    DEFAULT_CLIPBOARD_TTL,
    DEFAULT_RENEW_INTERVAL,
    ClipboardController,
)
from runtime.ports.clipboard import IClipboardConfigReader, IClipboardSource, IClipboardStore
from runtime.ports.jobs import IControllableJobClient

__all__ = ["ClipboardHost"]

logger = logging.getLogger(__name__)

#: Degraded-path loop cadence (seconds) for draining the source.
DEFAULT_SERVE_CADENCE = 0.2


class ClipboardHost:
    """Owns the resident clipboard job + its stop lifecycle."""

    def __init__(
        self,
        client: IControllableJobClient,
        source: IClipboardSource,
        store: IClipboardStore,
        config: IClipboardConfigReader,
        *,
        clock: Callable[[], float],
        ttl: float = DEFAULT_CLIPBOARD_TTL,
        renew_interval: float = DEFAULT_RENEW_INTERVAL,
        stop_event: threading.Event | None = None,
    ) -> None:
        self._client = client
        self._controller = ClipboardController(
            client,
            source,
            store,
            config,
            clock=clock,
            ttl=ttl,
            renew_interval=renew_interval,
        )
        self._stop_event = stop_event if stop_event is not None else threading.Event()

    # ── Observability ────────────────────────────────────────────────

    @property
    def controller(self) -> ClipboardController:
        """The underlying controller (for tests/inspection)."""
        return self._controller

    @property
    def job_id(self) -> str | None:
        """The hub-allocated job id while the watcher is live."""
        return self._controller.job_id

    @property
    def state(self) -> str:
        """Current contract state: ``idle`` | ``running`` | ``paused``."""
        return self._controller.state

    @property
    def stop_event(self) -> threading.Event:
        """The single stop event shared by a signal handler / serve loop."""
        return self._stop_event

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> str:
        """Bind the control handler, begin the job, and start the source."""
        self._client.set_control_handler(self._control)
        return self._controller.start()

    def tick(self) -> bool:
        """Drain one reading + renew the lease on cadence."""
        return self._controller.tick()

    def stop(self) -> None:
        """Stop the watcher and end the hub job (idempotent, never raises)."""
        if self._controller.job_id is None:
            return
        self._controller.stop()

    def request_stop(self) -> None:
        """Ask the serving loop to exit (signal handler / ``Control("stop")``)."""
        self._stop_event.set()

    def stop_requested(self) -> bool:
        """True once a stop was requested."""
        return self._stop_event.is_set()

    def serve(
        self,
        *,
        cadence: float = DEFAULT_SERVE_CADENCE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Degraded-path loop: drain + tick until stop (no bus to pump)."""
        while not self.stop_requested():
            self.tick()
            if self.stop_requested():
                break
            sleep(cadence)

    # ── Internals ────────────────────────────────────────────────────

    def _control(self, job_id: str, action: str) -> None:
        """Route a hub ``Control`` action; ``stop`` also exits the host."""
        self._controller.control(job_id, action)
        if action == "stop":
            self._stop_event.set()
