from __future__ import annotations

from abc import ABC, abstractmethod


class IDesktopConfigWriter(ABC):
    @abstractmethod
    def write_consumer_symlink(self, source_path: str, target_path: str) -> None:
        """Atomically repoint a consumer symlink (tmp + os.replace)."""
