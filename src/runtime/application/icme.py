"""ICME save event — the editor's domain event through the hub (Phase 5, 5-4).

Per the validate-gate ruling C1, ``icme.saved`` is a **BAR/UI domain
event, not a daemon trigger**: the daemon observes ICME through the
watched ``icon-mappings`` file (Epic 5-3), so this module implements only
the emit. It is deliberately not referenced by the daemon or the watch
coordinator — a test pins that.

The emit goes through :class:`IEventPublisher`, i.e. the hub's validated
``Emit`` path; the tool never constructs a D-Bus signal (AD-38).
"""

from __future__ import annotations

from runtime.ports.event_bus import IEventPublisher

__all__ = ["ICME_SAVED_TOPIC", "emit_icme_saved"]

#: The contract topic (mirrors ``contracts/event-contract.json`` topics).
ICME_SAVED_TOPIC = "icme.saved"


def emit_icme_saved(publisher: IEventPublisher, path: str) -> None:
    """Publish ``icme.saved {path}`` after a successful editor save.

    ``path`` is the icon-mappings manifest the editor wrote; a blank path is
    a programming error and fails loud rather than emitting a useless event.
    """
    if not isinstance(path, str) or not path:
        raise ValueError(f"icme.saved path must be a non-empty string, got {path!r}")
    publisher.publish(ICME_SAVED_TOPIC, {"path": path})
