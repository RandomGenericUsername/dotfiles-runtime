"""Deferred session-bus name owner — placeholder until P5-1-2 (Phase 5).

The concrete Python D-Bus client library choice is DELIBERATELY deferred
(P5-1-1 Gate-1 rec; must fit the synchronous core, GLib/sync preferred over
asyncio-first), so this adapter cannot speak D-Bus yet: ``acquire()``
always fails fatal with :class:`BusUnavailableError`, letting the
supervisor retry with backoff until P5-1-2 wires the real client.
``release()`` is a no-op (never-owned) and ``wait_until_terminated()``
parks on ``signal.pause()`` (unreachable in production until acquire can
succeed; SIGTERM then kills the process and the bus frees the name).

Layering (AD-1/13/14/25, AD-34): this adapter is the ONLY module that may
import a D-Bus client library when P5-1-2 arrives; the core (domain, ports,
application) and the CLI composition stay transport-agnostic and depend on
:mod:`runtime.ports.bus_name_owner` only.

Parking is a release-driven ``threading.Event.wait()`` (zero CPU, no
polling, no timers): the composition root's SIGTERM/SIGINT handler calls
:meth:`release`, which unblocks the wait.
"""

from __future__ import annotations

import threading

from runtime.domain.models import BusUnavailableError
from runtime.ports.bus_name_owner import IBusNameOwner


class DeferredDbusNameOwner(IBusNameOwner):
    """Name owner without a D-Bus client (P5-1-1 placeholder)."""

    def __init__(self) -> None:
        self._released = threading.Event()

    def acquire(self) -> None:
        """Always fail fatal: no D-Bus client is wired yet (P5-1-2)."""
        raise BusUnavailableError(
            "session-bus client not wired yet (P5-1-2 wires the D-Bus adapter); "
            "the supervisor will retry with backoff"
        )

    def release(self) -> None:
        """Mark released (idempotent): unblocks :meth:`wait_until_terminated`."""
        self._released.set()

    def wait_until_terminated(self) -> None:
        """Block with zero CPU (no polling, no timers) until released."""
        self._released.wait()
