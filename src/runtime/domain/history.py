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
#: ``reactive`` is written by the daemon reactive composite. ``daemon`` is a
#: generation-manifest *source kind*, never a history trigger; daemon-queue
#: jobs are normalized to the originating intent (``set``/``reconcile``) or
#: to ``reactive`` when they carry no valid request (see
#: :func:`normalize_history_trigger`).
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


def normalize_history_trigger(value: object, *, fallback: str = "reactive") -> str:
    """Return a writable history trigger for a job/request value.

    The daemon queue carries the trigger chosen by whoever submitted the
    request (``set`` for ``wallpaper set``, ``reconcile`` for a standalone
    reconcile) and has no trigger of its own. A missing or unknown value is
    *not* allowed to reach ``history.jsonl``: it is mapped to ``fallback``
    (``reactive`` for a daemon-originated job). The fallback must itself be a
    canonical trigger so this helper can never smuggle in a new value.
    """
    if fallback not in HISTORY_TRIGGERS:
        raise ValueError(f"invalid fallback history trigger: {fallback!r}")
    if isinstance(value, str) and value in HISTORY_TRIGGERS:
        return value
    return fallback
