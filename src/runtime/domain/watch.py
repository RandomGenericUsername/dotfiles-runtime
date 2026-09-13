"""Watch-event vocabulary (pure) — the boundary type watch adapters/use cases share.

AD-39/AD-40: the daemon reacts only to an explicit enumerated watch-root
set, with the inotify transport living in ``adapters/`` and the trigger
decision logic in ``application/``. This module is the pure intersection:
an event kind enum plus a path-free record (the path is carried as a
string for logging only — nothing in domain or application stats it).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WatchEventKind(StrEnum):
    """Raw inotify-backed event kinds the coordinator understands."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    MOVED_FROM = "moved_from"
    MOVED_TO = "moved_to"
    OVERFLOW = "overflow"
    IGNORED = "ignored"
    MOVE_SELF = "move_self"
    DELETE_SELF = "delete_self"


#: Kinds that denote an actual input-content change (an input file or a
#: directory entry in a watched tree).
CONTENT_CHANGE: frozenset[WatchEventKind] = frozenset(
    {
        WatchEventKind.CREATED,
        WatchEventKind.MODIFIED,
        WatchEventKind.DELETED,
        WatchEventKind.MOVED_FROM,
        WatchEventKind.MOVED_TO,
    }
)

#: Kinds that mean a watch registration was lost: the watcher must
#: re-establish its watches and force a full re-scan (AD-40).
REGISTRATION_LOSS: frozenset[WatchEventKind] = frozenset(
    {
        WatchEventKind.IGNORED,
        WatchEventKind.MOVE_SELF,
        WatchEventKind.DELETE_SELF,
    }
)


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """One raw watch event. ``path`` is diagnostic only (never stat-ed)."""

    kind: WatchEventKind
    path: str = ""
    is_directory: bool = False


@dataclass(frozen=True, slots=True)
class WatchStatus:
    """Health of the installed watch set (AD-40 registration exhaustion).

    ``registered`` is how many watches are currently installed; ``failed``
    names the roots that could **not** be registered (e.g. ``max_user_watches``
    exhaustion — ``ENOSPC``/``EMFILE``). A non-empty ``failed`` is a degraded
    watch set: the daemon is temporarily blind to those roots and must retry
    at the next safe opportunity (never poll). ``last_error`` is diagnostic
    only and never parsed.
    """

    registered: int = 0
    failed: tuple[str, ...] = ()
    last_error: str | None = None

    @property
    def degraded(self) -> bool:
        """True when any root is unwatched."""
        return bool(self.failed)
