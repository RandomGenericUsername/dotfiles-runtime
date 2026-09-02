"""Reconcile-desktop-state use case — the swap sequence (steps 1–4).

Implements AD-1 (hexagonal), AD-2 (input changes invalidate), AD-5
(state_root), AD-6 (swap symlinks lead), AD-14 (domain purity), AD-17
(consumer wiring), AD-18 (preserve monitor configs), AD-20 (CLI
rendering).

Scope boundary (Story 2.1):
- Repoints ``current/`` symlinks to converge the desktop with
  ``current.json``, appends ``history.jsonl``, persists refreshed
  ``current.json``.
- Does NOT invoke any desktop reload (Hyprland/AGS/Hyprpaper — Stories
  2.3–2.6); does NOT rewire ``wallpaper set`` (Story 2.7 capstone).

Swap sequence order (shared-data-contract, non-negotiable):
1. Ensure cache entries (wallpaper re-import on miss; palette/icons/effects
   regeneration with hash-mismatch guard; effects/icons graceful).
2. Repoint ONLY ``current/`` symlinks via ``CacheSeeder``.
3. ``current.json`` follows (refreshed ``applied_at``).
4. History: append one ``history.jsonl`` line (trigger ``"reconcile"``).

Derivation (step 1) runs OUTSIDE the lock (staging is race-safe;
tool invocations stay parallel — same split ``ApplyWallpaperUseCase``
uses). The load→repoint→save critical section is serialized by the
blocking seed mutex (double-checked pattern, D1 remediation from Story
1.13).

Architecture:
- Lives in ``application/`` (use-case layer) per AD-1, AD-13
- Injected adapters mirror ``ApplyWallpaperUseCase`` exactly
- No raw ``os``/``json`` FS I/O in the use case (delegated to adapters)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.seeder import CacheSeeder
from runtime.application.derive import DerivationPipeline
from runtime.domain.models import (
    DEFAULT_MONITOR,
    DesktopState,
    EffectsEntry,
    IconsEntry,
    PaletteEntry,
)
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.icon_renderer import IIconRenderer
from runtime.ports.seed_mutex import ISeedMutex
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Outcome of one ``ReconcileDesktopStateUseCase.run`` invocation."""

    repointed: list[Path]
    skipped: list[str]
    state: DesktopState
    cache_regenerated: list[str]


class ReconcileDesktopStateUseCase:
    """Repoint ``current/`` symlinks to converge with ``current.json``.

    Steps (one discrete method — the swap step Story 2.2 instruments):
    1. Load ``current.json`` (fail-fast corrupt/absent guard)
    2. Hold the seed mutex (blocking), re-load inside critical section
    3. Ensure cache entries (derivation outside the lock — same split as
       ``ApplyWallpaperUseCase``)
    4. Repoint symlinks via ``CacheSeeder.repoint_current_symlinks``
    5. Re-save ``current.json`` with refreshed ``applied_at``
    6. Append ``history.jsonl`` (trigger ``"reconcile"``)

    Constructor receives ports, the injected ``CacheSeeder`` adapter and
    the state mutex (dependency inversion): the use case wires no concrete
    adapters itself. No reload channel, no backend factory — the reload
    contract (step 5 of the shared-data-contract) is Stories 2.3–2.6.
    """

    def __init__(
        self,
        state_repo: IStateRepository,
        csg: IColorSchemeGenerator,
        weg: IEffectsGenerator,
        itr: IIconRenderer,
        install_spine: Path,
        state_root: Path,
        seeder: CacheSeeder,
        mutex: ISeedMutex,
    ) -> None:
        self._state_repo = state_repo
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex
        self._pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
        )

    def run(self) -> ReconcileResult:
        """Reconcile: entries → symlinks → current.json → history.

        Raises:
            RuntimeError: if ``current.json`` is absent (nothing to
                reconcile), palette (hard dependency) regeneration fails,
                wallpaper entry cannot be rebuilt, or hash-mismatch guard
                fires (AC 6b).
            ValueError: propagated from a corrupt ``current.json`` (fail-
                fast — never swallowed into a reseed).
            OSError: on filesystem failures.
        """
        # Fail-fast corrupt/absent guard (mirrors apply):
        state = self._state_repo.load_current()
        if state is None:
            raise RuntimeError("nothing to reconcile: no current state (never seeded)")
        # ValueError from a corrupt store propagates loudly here — never
        # swallowed into a reseed or fallback.

        # Derivation (step 1) happens OUTSIDE the lock (staging is race-
        # safe; tool invocations stay parallel — same split as apply).
        regenerated: list[str] = []
        palette, effects, icons = self._ensure_entries(state, regenerated)

        # Re-read after derivation: state may have changed concurrently
        # (e.g. a wallpaper set landed while tools ran). Re-load is
        # authoritative; derivation results are validated against it in
        # the hash-mismatch guard.
        with self._mutex.hold(blocking=True):
            state = self._state_repo.load_current()
            if state is None:
                raise RuntimeError("nothing to reconcile: no current state (never seeded)")
            state = DesktopState(
                schema_version=2,
                wallpaper=state.wallpaper,
                monitors=state.monitors,
                palette=palette,
                effects=effects,
                icons=icons,
                applied_at=state.applied_at,
            )

            # Step 2 — repoint ONLY the current/ symlinks
            monitor_names = list(state.monitors) or [DEFAULT_MONITOR]
            wallpaper_target = (
                self._state_root
                / "cache"
                / "wallpapers"
                / state.wallpaper.content_hash
                / "wallpaper.png"
            )
            repointed = self._seeder.repoint_current_symlinks(
                wallpaper_target=wallpaper_target,
                monitor_names=monitor_names,
                palette_entry_hash=state.palette.entry_hash if state.palette else None,
                effects_entry_hash=state.effects.entry_hash if state.effects else None,
                icons_entry_hash=state.icons.entry_hash if state.icons else None,
            )
            skipped = self._derive_skipped(state, monitor_names, repointed)

            # Step 3 — current.json follows (refreshed applied_at)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            saved = DesktopState(
                schema_version=2,
                wallpaper=state.wallpaper,
                monitors=state.monitors,
                palette=palette,
                effects=effects,
                icons=icons,
                applied_at=now,
            )
            self._state_repo.save(saved)

        # Step 4 — history (outside the lock — O_APPEND + fsync is atomic)
        self._seeder.append_history(
            trigger="reconcile",
            wallpaper_hash=saved.wallpaper.content_hash,
            palette_hash=saved.palette.entry_hash if saved.palette else None,
            effects_hash=saved.effects.entry_hash if saved.effects else None,
            icons_hash=saved.icons.entry_hash if saved.icons else None,
            source_path=saved.wallpaper.source_path,
        )

        return ReconcileResult(
            repointed=repointed,
            skipped=skipped,
            state=saved,
            cache_regenerated=regenerated,
        )

    # ------------------------------------------------------------------
    # Step 1 — ensure cache entries (derivation outside the lock)
    # ------------------------------------------------------------------

    def _ensure_entries(
        self,
        state: DesktopState,
        regenerated: list[str],
    ) -> tuple[PaletteEntry | None, EffectsEntry | None, IconsEntry | None]:
        """Ensure every cache entry referenced by the state exists.

        Returns the (possibly-rebuilt) palette/effects/icons entries and
        appends layer names to ``regenerated`` for actual cache misses.
        """
        self._ensure_wallpaper_entry(state, regenerated)
        cached_wallpaper_img = (
            self._state_root
            / "cache"
            / "wallpapers"
            / state.wallpaper.content_hash
            / "wallpaper.png"
        )

        palette = state.palette
        if (
            palette is not None
            and not cache_entry_path(self._state_root, "palettes", palette.entry_hash).exists()
        ):
            try:
                entry, _cache_hit = self._pipeline.ensure_palette(
                    cached_wallpaper_img, state.wallpaper.content_hash
                )
            except Exception as exc:
                raise RuntimeError(f"palette reconcile failed: {exc}") from exc
            self._assert_hash_matches(palette.entry_hash, entry.entry_hash)
            palette = entry
            regenerated.append("palette")

        effects = state.effects
        if (
            effects is not None
            and not cache_entry_path(self._state_root, "effects", effects.entry_hash).exists()
        ):
            try:
                entry_e, _cache_hit = self._pipeline.ensure_effects(
                    cached_wallpaper_img, state.wallpaper.content_hash
                )
            except Exception as exc:
                logger.warning("reconcile: effects regeneration failed; continuing: %s", exc)
                effects = None
            else:
                self._assert_hash_matches(effects.entry_hash, entry_e.entry_hash)
                effects = entry_e
                regenerated.append("effects")

        icons = state.icons
        if (
            icons is not None
            and palette is not None
            and not cache_entry_path(self._state_root, "icons", icons.entry_hash).exists()
        ):
            try:
                entry_i, _cache_hit = self._pipeline.ensure_icons(palette.entry_hash)
            except Exception as exc:
                logger.warning("reconcile: icons regeneration failed; continuing: %s", exc)
                icons = None
            else:
                self._assert_hash_matches(icons.entry_hash, entry_i.entry_hash)
                icons = entry_i
                regenerated.append("icons")

        return palette, effects, icons

    def _ensure_wallpaper_entry(self, state: DesktopState, regenerated: list[str]) -> None:
        """Ensure the wallpaper cache entry exists (re-import from source)."""
        wh = state.wallpaper.content_hash
        cached_png = self._state_root / "cache" / "wallpapers" / wh / "wallpaper.png"
        if cached_png.exists():
            return
        source = state.wallpaper.source_path
        if not source:
            raise RuntimeError(
                f"wallpaper cache entry {wh} is missing and the state has no "
                "source_path; the wallpaper cache entry cannot be rebuilt "
                "without its source"
            )
        src = Path(source)
        if not src.is_file():
            raise RuntimeError(
                f"wallpaper cache entry {wh} is missing and its source no longer exists: {src}"
            )
        self._seeder.import_wallpaper(src, wh, source_mutable=True)
        regenerated.append("wallpaper")

    @staticmethod
    def _assert_hash_matches(recorded: str, regenerated: str) -> None:
        """AC 6b: a regenerated entry must reproduce the recorded hash."""
        if regenerated != recorded:
            raise RuntimeError(
                f"cache entry {recorded} cannot be regenerated from current "
                "spine inputs; re-run wallpaper set"
            )

    # ------------------------------------------------------------------
    # Skipped-symlink derivation
    # ------------------------------------------------------------------

    def _derive_skipped(
        self,
        state: DesktopState,
        monitor_names: list[str],
        repointed: list[Path],
    ) -> list[str]:
        """Compute the skip list: expected symlink names minus created."""
        expected: dict[str, str] = {}
        for name in monitor_names:
            expected[f"wallpaper-{name}.png"] = "wallpaper symlink not created"
        if state.palette is not None:
            for artifact in ("colors.conf", "colors.gtk.css", "colors.yaml"):
                expected[artifact] = "palette artifact missing from cache entry"
        if state.effects is not None:
            expected["effects"] = "effects cache entry missing"
        if state.icons is not None:
            expected["icons"] = "icons cache entry missing"

        created = {p.name for p in repointed}
        skipped: list[str] = []
        for name, reason in expected.items():
            if name not in created:
                skipped.append(f"{name} ({reason})")

        for layer in ("effects", "icons"):
            entry = getattr(state, layer)
            if entry is None:
                skipped.append(f"{layer} (layer is null; nothing to repoint)")
                logger.warning("reconcile: %s layer is null; consumer symlink skipped", layer)

        return skipped
