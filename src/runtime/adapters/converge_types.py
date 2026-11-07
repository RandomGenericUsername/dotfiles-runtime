"""Shared result carrier for the daemon converge queue (no I/O)."""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["ConvergeQueueResult"]


@dataclass(frozen=True, slots=True)
class ConvergeQueueResult:
    """What the worker reports for one coalesced converge execution."""

    generation_hash: str | None
    error: str | None = None
    exit_code: int = 0
    degraded: bool = False
    superseded_by: str | None = None
    app_results: tuple[dict[str, object], ...] = field(default_factory=tuple)
