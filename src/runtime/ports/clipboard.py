"""Clipboard ports — transport-agnostic seams for the clipboard job.

The clipboard host talks to three injected seams and never to a subprocess,
a file, or a bus directly (layering rule):

- :class:`IClipboardSource` — the change stream. The production adapter is
  ``wl-paste --watch`` (compositor ``wlr-data-control`` events) with a
  declared polling fallback; tests inject a scripted fake.
- :class:`IClipboardStore` — history persistence (load/add/dedupe/evict/
  favorite/delete). The production adapter is the JSON document store.
- :class:`IClipboardConfigReader` — per-kind retention settings.

Ports are ABCs only; concrete adapters live in ``runtime.adapters``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from runtime.domain.clipboard import ClipboardItem, ClipboardReading, RetentionLimits

__all__ = ["IClipboardConfigReader", "IClipboardSource", "IClipboardStore"]

#: Declared source modes; ``polling`` is the explicit degraded mode.
SOURCE_MODE_PROTOCOL = "protocol"
SOURCE_MODE_POLLING = "polling"


class IClipboardSource(ABC):
    """A blocking clipboard-change stream with a declared mode."""

    @abstractmethod
    def start(self) -> None:
        """Begin observing (spawn the watch process / arm the poller)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop observing and release any child process (idempotent)."""

    @abstractmethod
    def next_change(self, timeout: float) -> ClipboardReading | None:
        """Return the next reading, or ``None`` if none arrived in time.

        In protocol mode this blocks up to ``timeout`` on the compositor
        event stream; in the degraded polling mode it samples at most once
        per call and returns a reading only when the content actually
        changed. It MUST NOT raise for an empty clipboard.
        """

    @property
    @abstractmethod
    def mode(self) -> str:
        """``protocol`` when the data-control path is live, else ``polling``."""


class IClipboardStore(ABC):
    """History persistence with hash dedupe and retention."""

    @abstractmethod
    def load(self) -> list[ClipboardItem]:
        """All stored items, newest first."""

    @abstractmethod
    def add(self, item: ClipboardItem) -> ClipboardItem:
        """Insert or, when the hash exists, bump recency; return the stored item.

        An existing item's ``favorite`` flag is preserved (a re-copy must not
        clear a favorite).
        """

    @abstractmethod
    def delete(self, item_hash: str) -> bool:
        """Remove the item; return whether it existed."""

    @abstractmethod
    def set_favorite(self, item_hash: str, favorite: bool) -> bool:
        """Set the favorite flag; return whether the item existed."""

    @abstractmethod
    def evict(self, limits: RetentionLimits) -> list[ClipboardItem]:
        """Drop items over the per-kind limits; return the evicted items."""


class IClipboardConfigReader(ABC):
    """Reads the per-kind retention policy from the settings file."""

    @abstractmethod
    def read(self) -> RetentionLimits:
        """Return the configured limits, falling back to defaults on error."""
