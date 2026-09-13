"""Reactive converge use case — the daemon's composite (AD-35/AD-36/AD-42).

This is composition, not a new pipeline: it runs the existing synchronous
use cases in the pinned order

    CheckInputs → RegenerateStale → Reconcile → declarative converge

then a prune pass, and appends **exactly one** history line
``trigger="reactive"``. Inner history writes are suppressed by the
composition root (the injected use cases are wired with a history-suppressing
``CacheSeeder``); this use case owns the single reactive audit line.

Loop safety (AD-36): a persisted last-converged input-hash backstop short
circuits an unchanged input set. An unseeded runtime (no ``current.json``)
is a benign no-op — the daemon never seeds (AD-11) and never errors on it.
Observe-only mode (the shipped default, AD-35/AD-41) detects the change and
returns without running any use case and without appending history.

The use case performs no I/O itself: hashing, persistence, use-case
invocation, and history appending are all injected callables (composition
root owns the wiring), so it is unit-testable with fakes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReactiveConvergeResult:
    """Outcome of one reactive converge decision."""

    ran: bool
    reason: str
    input_hash: str


class ReactiveConvergeUseCase:
    """Decide and (when active) run the reactive convergence composite."""

    def __init__(
        self,
        *,
        input_hash: Callable[[], str],
        read_backstop: Callable[[], str | None],
        write_backstop: Callable[[str], None],
        has_state: Callable[[], bool],
        check_inputs: Callable[[], object],
        regenerate_stale: Callable[[], object],
        reconcile: Callable[[], object],
        declarative: Callable[[], object],
        append_history: Callable[[], None],
        prune: Callable[[], object] | None = None,
        observe_only: bool = True,
    ) -> None:
        self._input_hash = input_hash
        self._read_backstop = read_backstop
        self._write_backstop = write_backstop
        self._has_state = has_state
        self._check_inputs = check_inputs
        self._regenerate_stale = regenerate_stale
        self._reconcile = reconcile
        self._declarative = declarative
        self._append_history = append_history
        self._prune = prune
        self._observe_only = observe_only

    def run(self) -> ReactiveConvergeResult:
        """Converge if the watched inputs changed; idempotent and re-runnable."""
        current = self._input_hash()
        if self._read_backstop() == current:
            logger.debug("reactive: inputs unchanged; no-op")
            return ReactiveConvergeResult(ran=False, reason="unchanged", input_hash=current)
        if not self._has_state():
            logger.info("reactive: unseeded (no current.json); benign no-op")
            return ReactiveConvergeResult(ran=False, reason="unseeded", input_hash=current)
        if self._observe_only:
            logger.info("reactive: watched inputs changed; observe-only (no converge)")
            return ReactiveConvergeResult(ran=False, reason="observe-only", input_hash=current)

        # Composite (AD-36), history suppressed inside every step.
        self._check_inputs()
        self._regenerate_stale()
        self._reconcile()
        self._declarative()
        if self._prune is not None:
            self._prune()
        # Exactly one audit line, after the converge settled and before the
        # backstop is persisted: a failure here leaves the backstop unwritten,
        # so the next event retries instead of silently losing the change.
        self._append_history()
        self._write_backstop(current)
        logger.info("reactive: converged (input hash %s)", current[:12])
        return ReactiveConvergeResult(ran=True, reason="converged", input_hash=current)
