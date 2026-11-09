"""Port: read the host's effective defaults for authored skin tokens."""

from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.host_defaults import HostSnapshot


class IHostDefaults(ABC):
    """Read the live host values that a skin may inherit.

    Implementations are read-only and best-effort: a missing compositor,
    ambiguous instance discovery or an invalid option returns ``None`` (host
    unavailable) rather than aborting convergence. No polling is allowed.
    """

    @abstractmethod
    def read(self) -> HostSnapshot | None:
        """Return one effective-value snapshot, or ``None`` when unavailable."""
        raise NotImplementedError
