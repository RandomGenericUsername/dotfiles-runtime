from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.ags_apps import AppRefreshOutcome
from runtime.domain.models import DesktopState


class IAgsAppRefresher(ABC):
    """Refresh the configured AGS apps for one applied appearance (AD-27…30)."""

    @abstractmethod
    def refresh(self, state: DesktopState, *, trigger: str = "reconcile") -> AppRefreshOutcome:
        """Apply the appearance to every app that is not already current."""
