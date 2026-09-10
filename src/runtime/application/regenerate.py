"""Minimal selective regeneration + reconverge (Phase 3, Story 1.4).

``RegenerateStaleUseCase`` closes the Epic 1 loop (check → regen →
reconverge): it re-derives ONLY stale layers (+ cascade) through the Phase 2
``DerivationPipeline``, persists the new pointers, and delegates convergence
(swap → save → history → reload) to ``ReconcileDesktopStateUseCase``.

Why this is not a duplicate of reconcile: reconcile converges to RECORDED
state (stale entries are cache hits, so it changes nothing); regen converges
to CURRENT-INPUT state. The bridge, not a duplicate.

Fresh layers are untouched BY CONSTRUCTION: the pipeline short-circuits on
cache hit (hash walk only, no tool invocation), and this use case never calls
``ensure_*`` for fresh layers at all.

Concurrency contract (mirrors the Phase-2 apply→reconcile split):
  - Derivation runs WITHOUT the lock (staging-dir is race-safe; tool
    invocations must not serialize concurrent CLIs for minutes).
  - The save is a compare-and-swap under ``mutex.hold(blocking=True)``:
    re-load ``current.json`` inside the lock and abort loud on wallpaper-hash
    drift (a concurrent ``wallpaper set`` won the race; the operator re-runs —
    check is cheap). Never clobber newer state.
  - The lock is RELEASED before ``reconcile.run()`` (which locks internally;
    nesting flock acquisitions deadlocks).
  - If ``reconcile.run()`` raises after the guarded save, ``current.json``
    is newer than the ``current/`` symlinks with no history line; the next
    plain ``reconcile`` (recovery + repoint) repairs this — re-run it, do
    not hand-edit state.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from runtime.application.check_inputs import CheckInputsUseCase
from runtime.application.derive import DerivationPipeline
from runtime.application.reconcile import ReconcileDesktopStateUseCase
from runtime.domain.models import DesktopState
from runtime.ports.seed_mutex import ISeedMutex
from runtime.ports.state_repository import IStateRepository


@dataclass(frozen=True, slots=True)
class RegenerateResult:
    """Outcome of one selective-regeneration run."""

    regenerated: frozenset[str]
    state: DesktopState
    repointed: tuple[Path, ...] = ()
    reload_failures: tuple[str, ...] = ()


class RegenerateStaleUseCase:
    """Rebuild stale layers (+ cascade) and reconverge the desktop.

    Collaborators are same-layer units and ports only; the composition root
    wires concrete adapters. Import-clean for Story 2.2 reuse: no CLI types,
    no ``typer``, no ``cli_output`` here.
    """

    def __init__(
        self,
        check: CheckInputsUseCase,
        pipeline: DerivationPipeline,
        state_repo: IStateRepository,
        reconcile: ReconcileDesktopStateUseCase,
        mutex: ISeedMutex,
        state_root: Path,
    ) -> None:
        self._check = check
        self._pipeline = pipeline
        self._state_repo = state_repo
        self._reconcile = reconcile
        self._mutex = mutex
        self._state_root = state_root

    def run(self) -> RegenerateResult:
        """Regenerate stale layers and reconverge. Idempotent and re-runnable."""
        stale = self._check.run().stale
        state = self._state_repo.load_current()
        if state is None:
            raise ValueError(
                "no runtime state recorded yet (missing current.json); "
                "run `dotfiles-runtime wallpaper set <img>` first"
            )
        if not stale:
            self._wallpaper_bytes(state)  # fail loud on missing bytes even when fresh
            return RegenerateResult(regenerated=frozenset(), state=state)
        unknown = set(stale) - {"palettes", "effects", "icons"}
        if unknown:
            raise ValueError(f"unknown stale layers: {sorted(unknown)}")

        wallpaper_path = self._wallpaper_bytes(state)
        wallpaper_hash = state.wallpaper.content_hash
        new_palette = state.palette
        new_effects = state.effects
        new_icons = state.icons
        rebuilt: set[str] = set()

        if "palettes" in stale:
            # No absent-entry guard: ensure needs only wallpaper bytes, so a
            # degraded (entry-less) layer heals by derivation here.
            new_palette, _ = self._pipeline.ensure_palette(wallpaper_path, wallpaper_hash)
            rebuilt.add("palettes")
        if "effects" in stale:
            new_effects, _ = self._pipeline.ensure_effects(wallpaper_path, wallpaper_hash)
            rebuilt.add("effects")
        if "icons" in stale or "palettes" in rebuilt:
            # THE CASCADE (structural): icons key on palette_hash, so a new
            # palette is an automatic miss. Pass the POST-step palette hash —
            # never the stale recorded one.
            if new_palette is None:
                raise RuntimeError("icons regen needs a palette hash but none is recorded")
            new_icons, _ = self._pipeline.ensure_icons(new_palette.entry_hash)
            rebuilt.add("icons")

        updated = dataclasses.replace(
            state, palette=new_palette, effects=new_effects, icons=new_icons
        )
        self._save_guarded(state, updated)
        reconciled = self._reconcile.run(trigger="regenerate")
        return RegenerateResult(
            regenerated=frozenset(rebuilt),
            state=reconciled.state,
            repointed=tuple(reconciled.repointed),
            reload_failures=tuple(reconciled.reload_failures),
        )

    def _wallpaper_bytes(self, state: DesktopState) -> Path:
        """Resolve active wallpaper bytes from the cache (truth per AD-16).

        Never falls back to ``WallpaperEntry.source_path`` (may be deleted;
        the cache hardlink survives by design). Missing cached bytes fail
        loud — repopulation belongs to doctor Story 2.2, not this story.
        """
        candidate = (
            self._state_root
            / "cache"
            / "wallpapers"
            / state.wallpaper.content_hash
            / "wallpaper.png"
        )
        if not candidate.is_file():
            raise ValueError(
                f"cached wallpaper bytes missing: {candidate}; "
                "run `dotfiles-runtime doctor --repair` to repopulate"
            )
        return candidate

    def _save_guarded(self, observed: DesktopState, updated: DesktopState) -> None:
        """Compare-and-swap the refreshed state under the mutex."""
        with self._mutex.hold(blocking=True):
            current = self._state_repo.load_current()
            if current is None or _projection(current) != _projection(observed):
                raise RuntimeError(
                    "concurrent modification detected during regeneration "
                    "(another writer won the race); re-run the command"
                )
            self._state_repo.save(updated)


def _projection(state: DesktopState) -> tuple[str | None, str | None, str | None, str | None]:
    """Derivation-relevant identity: exactly what ``save()`` overwrites.

    Timestamp/metadata churn (``applied_at``, monitors) never aborts;
    semantic drift always does.
    """
    return (
        state.wallpaper.content_hash,
        state.palette.entry_hash if state.palette else None,
        state.effects.entry_hash if state.effects else None,
        state.icons.entry_hash if state.icons else None,
    )
