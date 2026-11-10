"""Host-compositor event vocabulary (pure).

Hyprland's socket2 is an event stream, not a state file: a ``configreloaded``
line says "re-read effective values now". The daemon consumes that event and
lets the normal convergence path decide whether anything actually changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HostEventKind(StrEnum):
    """Event kinds the runtime consumes from a host event source."""

    CONFIG_RELOADED = "configreloaded"


@dataclass(frozen=True, slots=True)
class HostEvent:
    """One host-compositor event; ``raw`` is diagnostic only."""

    kind: HostEventKind
    raw: str = ""
