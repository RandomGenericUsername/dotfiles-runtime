"""Session-bus name ownership port — the daemon's well-known name seam (Phase 5).

The runtime daemon is the sole owner of ``org.dotfiles.Events`` (AD-38);
this port owns the acquire/hold/release lifecycle so the core never imports
D-Bus (AD-34) and the concrete Python D-Bus client choice stays deferred
(P5-1-1 leaves the real adapter unimplemented; P5-1-2 wires it).

Domain purity (AD-1, AD-14):
- This module is a port: ABC only, no I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.models import BusNameContentionError, BusNameError, BusUnavailableError

__all__ = [
    "IBusNameOwner",
    "BusNameError",
    "BusNameContentionError",
    "BusUnavailableError",
]

#: The versionless well-known bus name (AD-34/C4); the interface stays
#: ``org.dotfiles.Events1`` (version in the interface name only).
BUS_NAME = "org.dotfiles.Events"


class IBusNameOwner(ABC):
    """Acquire, hold, and release the daemon's well-known session-bus name."""

    @abstractmethod
    def acquire(self) -> None:
        """Request the name with DO_NOT_QUEUE semantics (fail-fast, never queue).

        Returns once the name is owned. Readiness (``Type=dbus``) follows
        from this return.

        Raises:
            BusNameContentionError: another process owns the name.
            BusUnavailableError: no session bus (or no D-Bus client) to ask.
        """

    @abstractmethod
    def release(self) -> None:
        """Release the name (best-effort, idempotent).

        Called on SIGTERM and in every shutdown path — including paths
        where ``acquire()`` never succeeded (so it must tolerate the
        never-owned state). Never raises for the never-owned case.
        """

    @abstractmethod
    def wait_until_terminated(self) -> None:
        """Block (no polling, no timers) until shutdown is requested.

        Returns after :meth:`release` is invoked (e.g. from a SIGTERM
        handler installed by the composition root). The process dying
        without release still frees the name (bus disconnect), but the
        orderly path always releases first.
        """
