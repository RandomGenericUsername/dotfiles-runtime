"""The one v3 convergence function: ``converge(intent) -> generation``.

Every mutation — the daemon's reaction to ``desired.json``, CLI ``reconcile``,
``wallpaper set`` and a skin/mode change — enters here. The function has one
implementation: it serializes through ``converge.lock`` and the injected
reconcile use case owns intent → derive → stage → commit → apply/ledger in one
scope. There is no mode-only or wallpaper-set-only side pipeline.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

from runtime.application.reconcile import ReconcileDesktopStateUseCase, ReconcileResult

logger = logging.getLogger(__name__)

__all__ = ["ConvergeIntent", "ConvergeResult", "ConvergenceEngine", "intent_hash_for"]


@dataclass(frozen=True, slots=True)
class ConvergeIntent:
    """A resolved convergence request (no I/O, no generation yet)."""

    trigger: str
    source: str = "cli"
    job_id: str | None = None
    wallpaper_path: Path | None = None
    skin: str | None = None
    mode: str | None = None
    intent_hash: str | None = None
    #: Opt into the strict unchanged short-circuit (no current.json write,
    #: history line, GC, ledger, apply/refresh or reloaders when the resolved
    #: generation content already equals ``current``). Host config events use
    #: this so an unchanged/non-delegated host change is a true no-op.
    idempotent: bool = False


@dataclass(frozen=True, slots=True)
class ConvergeResult:
    """One committed generation plus the per-app apply outcome."""

    generation_hash: str
    committed: bool
    degraded: bool
    reconcile: ReconcileResult
    regenerated: tuple[str, ...] = ()

    @property
    def state(self) -> object:
        return self.reconcile.state

    @property
    def app_results(self) -> tuple[object, ...]:
        return tuple(self.reconcile.app_refresh_results)


def intent_hash_for(
    *,
    wallpaper: str,
    skin: str,
    mode: str,
    palette: str | None = None,
    effects: str | None = None,
    icons: str | None = None,
) -> str:
    """Canonical hash of a desired-state snapshot (stable across formatting)."""
    material = json.dumps(
        {
            "wallpaper": wallpaper,
            "skin": skin,
            "mode": mode,
            "palette": palette,
            "effects": effects,
            "icons": icons,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class ConvergenceEngine:
    """Run the single reconcile implementation for one intent."""

    def __init__(
        self,
        reconcile_factory: Callable[[str, str | None], ReconcileDesktopStateUseCase],
        *,
        lock: object | None = None,
        regenerate: Callable[[], object] | None = None,
    ) -> None:
        self._reconcile_factory = reconcile_factory
        # The one convergence lock covers an optional RegenerateStale pre-step
        # and the commit/save/history scope, so no CLI or daemon path can
        # interleave a pre-commit snapshot (BUG-013).
        self._lock = lock
        self._regenerate = regenerate

    def converge(self, intent: ConvergeIntent) -> ConvergeResult:
        scope = self._lock.hold() if self._lock is not None else nullcontext()  # type: ignore[attr-defined]
        with scope:
            regenerated: tuple[str, ...] = ()
            if self._regenerate is not None:
                pre_step = self._regenerate()
                regenerated = tuple(
                    sorted(str(layer) for layer in getattr(pre_step, "regenerated", ()) or ())
                )
            use_case = self._reconcile_factory(intent.source, intent.job_id)
            result = use_case.run(
                intent.trigger,
                wallpaper_path=intent.wallpaper_path,
                intent_hash=intent.intent_hash,
                acquire_lock=False,
                idempotent=intent.idempotent,
            )
        if result.generation_hash is None:
            raise RuntimeError("converge did not commit a generation")
        degraded = bool(result.reload_failures) or any(
            record.status != "applied"
            for record in result.app_refresh_results
            if record.status != "skipped"
        )
        logger.info(
            "converge: trigger=%s source=%s generation=%s degraded=%s regenerated=%s",
            intent.trigger,
            intent.source,
            result.generation_hash[:12],
            degraded,
            ",".join(regenerated) or "none",
        )
        return ConvergeResult(
            generation_hash=result.generation_hash,
            committed=True,
            degraded=degraded,
            regenerated=regenerated,
            reconcile=result,
        )
