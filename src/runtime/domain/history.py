"""History trigger enum — the single source of truth (AD-42, AD-44).

Every path that accepts a ``history.jsonl`` trigger references this module
(the reconcile validation and the inspect reader), and the history JSON
Schema's ``trigger.enum`` is pinned to it by an executable drift test. Pure
domain: a value set and a type alias, no I/O.
"""

from __future__ import annotations

from typing import Literal

#: Accepted ``history.jsonl`` trigger values, in canonical order.
#: ``force`` is **reserved** — historical, accepted on read, never written.
#: ``reactive`` is intended for the Phase 5 daemon; no caller writes it yet,
#: but accepting it ahead of the writer is deliberate at this boundary.
HISTORY_TRIGGERS: tuple[str, ...] = (
    "seed",
    "set",
    "reconcile",
    "force",
    "regenerate",
    "doctor",
    "prune",
    "reactive",
)

HistoryTrigger = Literal[
    "seed",
    "set",
    "reconcile",
    "force",
    "regenerate",
    "doctor",
    "prune",
    "reactive",
]
