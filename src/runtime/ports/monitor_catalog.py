"""Monitor catalog port — read-only descriptors for connected outputs (REQ-009).

Sibling to :class:`runtime.ports.monitor_source.IMonitorSource`: the
wallpaper path keeps consuming ``detect_monitors()`` (active names), while
the ``monitors list`` command and future per-monitor features consume the
richer descriptors. Implementations may satisfy both ports; consumers pick
only the surface they need.

The canonical contract is the capture EPIC-002 ``design.md`` §2.2. The
runtime owns the JSON schema at
``contracts/schemas/monitors-list.schema.json`` (D9: no ``contracts`` bump).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MonitorGeometry:
    """Compositor logical rectangle (display/join data, not capture math)."""

    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    """One compositor output, including disabled/mirrored diagnostics.

    ``disabled`` is carried so the pure application projection can filter
    without re-parsing; it is never emitted in the catalog JSON. ``label``
    is ``description`` when non-empty, else ``connector`` (contract §2.2).
    """

    connector: str
    label: str
    description: str
    make: str
    model: str
    serial: str
    geometry: MonitorGeometry
    scale: float
    transform: int
    focused: bool
    mirror_of: str | None
    disabled: bool = False


class IMonitorCatalog(ABC):
    """Read-only monitor descriptor source (language-agnostic CLI surface)."""

    @abstractmethod
    def list_monitors(self) -> list[MonitorInfo]:
        """Return every output the compositor reported, in compositor order.

        Disabled and mirrored outputs are included (with their diagnostic
        fields) so the pure projection owns filtering and ordering. An empty
        list means detection is unavailable; callers degrade to the
        ``unavailable`` catalog shape instead of failing.
        """
