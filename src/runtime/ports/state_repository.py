from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.models import DesktopState


class IStateRepository(ABC):
    @abstractmethod
    def load_current(self) -> DesktopState | None:
        """Load current state. Returns None on first run (no current.json)."""

    @abstractmethod
    def save(self, state: DesktopState) -> None:
        """Atomically persist current state (tmp + os.replace)."""
