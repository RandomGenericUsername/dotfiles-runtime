"""Reconcile-desktop-state use case — the swap sequence (steps 1–5).

Implements AD-1 (hexagonal), AD-2 (input changes invalidate), AD-5
(state_root), AD-6 (swap symlinks lead), AD-14 (domain purity), AD-17
(consumer wiring), AD-18 (preserve monitor configs), AD-20 (CLI
rendering).

Scope boundary (Stories 2.1–2.3):
- Repoints ``current/`` symlinks to converge the desktop with
  ``current.json``, appends ``history.jsonl``, persists refreshed
  ``current.json``, and triggers one reload per injected
  ``IDesktopReloader`` (Hyprland is Story 2.3; AGS is Story 2.4;
  Hyprpaper is Story 2.5; terminal is Story 2.6).
- Does NOT rewire ``wallpaper set`` (Story 2.7 capstone).

Swap sequence order (shared-data-contract, non-negotiable):
1. Ensure cache entries (wallpaper re-import on miss; palette/icons/effects
   regeneration with hash-mismatch guard; effects/icons graceful).
2. Repoint ONLY ``current/`` symlinks via ``CacheSeeder``.
3. ``current.json`` follows (refreshed ``applied_at``).
4. History: append one ``history.jsonl`` line (trigger ``"reconcile"``).
5. Reload desktop consumers (fire-and-report, per injected reloader).

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder, _repoint_symlink
from runtime.application.derive import DerivationPipeline
from runtime.domain.models import (
    DEFAULT_MONITOR,
    DesktopState,
    EffectsEntry,
    IconsEntry,
    PaletteEntry,
)
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.desktop_reloader import IDesktopReloader
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
    reload_failures: list[str] = field(default_factory=list)


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
    7. Reload desktop consumers (fire-and-report, per injected reloader)

    Constructor receives ports, the injected ``CacheSeeder`` adapter, the
    state mutex, and ``IDesktopReloader`` instances (dependency inversion —
    the use case wires no concrete adapters itself). Reload runs once per
    injected reloader after history append (step 5 of the shared-data
    contract), outside the lock.
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
        reloaders: list[IDesktopReloader] | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex
        self._reloaders: list[IDesktopReloader] = list(reloaders) if reloaders is not None else []
        self._pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
        )

    def run(self) -> ReconcileResult:
        """Reconcile: entries → symlinks → current.json → history → reload.

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
            raise RuntimeError("nothing to reconcile")
        # ValueError from a corrupt store propagates loudly here — never
        # swallowed into a reseed or fallback.

        # Recovery: revert stray current/ symlinks to last-good current.json (AC 1, 2, 5).
        # Runs OUTSIDE the lock — same as derivation; concurrent wallpaper set
        # overwriting current.json is caught by the double-checked re-load inside the lock.
        reverted = self._revert_stale_symlinks(state)
        if reverted:
            logger.info("recovery: reverted %d stray symlink(s): %s", len(reverted), reverted)
        else:
            logger.debug("recovery: no stray symlinks detected")

        # Derivation (step 1) happens OUTSIDE the lock (staging is race-
        # safe; tool invocations stay parallel — same split as apply).
        regenerated: list[str] = []
        palette, effects, icons = self._ensure_entries(state, regenerated)
        pre_lock_wallpaper_hash = state.wallpaper.content_hash

        # Re-read after derivation: state may have changed concurrently
        # (e.g. a wallpaper set landed while tools ran). If the wallpaper
        # hash changed, re-derive inside the lock so palette/effects/icons
        # match the authoritative state.
        with self._mutex.hold(blocking=True):
            state = self._state_repo.load_current()
            if state is None:
                raise RuntimeError("nothing to reconcile")

            # Decision D1: re-derive if state changed while we were outside.
            if state.wallpaper.content_hash != pre_lock_wallpaper_hash:
                # Re-derive for the authoritative state inside the lock.
                # This serializes tool invocations only on contention.
                palette, effects, icons = self._ensure_entries(state, regenerated)
            else:
                # Re-validate palette/effects/icons hashes against reloaded
                # state to catch stale derivation even when wallpaper hash
                # matches (e.g. spine change caused hash-mismatch guard).
                # _ensure_entries already validated, but re-check in case
                # state was mutated concurrently for derived layers.
                if palette is not None and state.palette is not None:
                    self._assert_hash_matches(state.palette.entry_hash, palette.entry_hash)
                if effects is not None and state.effects is not None:
                    self._assert_hash_matches(state.effects.entry_hash, effects.entry_hash)
                if icons is not None and state.icons is not None:
                    self._assert_hash_matches(state.icons.entry_hash, icons.entry_hash)

            state = DesktopState(
                schema_version=2,
                wallpaper=state.wallpaper,
                monitors=state.monitors,
                palette=palette,
                effects=effects,
                icons=icons,
                applied_at=state.applied_at,
            )

            # Monitor name validation P1 — fail loud on traversal
            for m in list(state.monitors):
                if "/" in m or "\\" in m or m.strip() != m or ".." in m:
                    raise ValueError(f"monitor name must not contain path separators, got {m!r}")

            # Step 2 — repoint ONLY the current/ symlinks
            monitor_names = list(state.monitors) or [DEFAULT_MONITOR]
            # Use cache_entry_path helper for validation (P7)
            wallpaper_target = (
                cache_entry_path(self._state_root, "wallpapers", state.wallpaper.content_hash)
                / "wallpaper.png"
            )
            # P2 — guard against dangling wallpaper symlink
            if not wallpaper_target.exists() or not wallpaper_target.is_file():
                raise RuntimeError(
                    f"wallpaper cache entry {state.wallpaper.content_hash} "
                    f"missing: {wallpaper_target}"
                )
            repointed = self._seeder.repoint_current_symlinks(
                wallpaper_target=wallpaper_target,
                monitor_names=monitor_names,
                palette_entry_hash=state.palette.entry_hash if state.palette else None,
                effects_entry_hash=state.effects.entry_hash if state.effects else None,
                icons_entry_hash=state.icons.entry_hash if state.icons else None,
            )
            # D2 — cleanup stale symlinks
            stale_removed = self._cleanup_stale_symlinks(state, monitor_names, repointed)
            # P8 — derive skipped outside logging inside lock: collect without logging
            skipped_raw = self._derive_skipped(state, monitor_names, repointed, log=False)
            # Account for stale removals as skipped context (they were stale)
            skipped = skipped_raw

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

        # Logging for skipped null layers outside lock (P8)
        for layer in ("effects", "icons"):
            entry = getattr(saved, layer)
            if entry is None and f"{layer} (layer is null" in " ".join(skipped):
                logger.warning("reconcile: %s layer is null; consumer symlink skipped", layer)
        if stale_removed:
            logger.info(
                "reconcile: removed %d stale symlink(s): %s", len(stale_removed), stale_removed
            )

        # Step 4 — history (outside the lock — O_APPEND + fsync is atomic)
        self._seeder.append_history(
            trigger="reconcile",
            wallpaper_hash=saved.wallpaper.content_hash,
            palette_hash=saved.palette.entry_hash if saved.palette else None,
            effects_hash=saved.effects.entry_hash if saved.effects else None,
            icons_hash=saved.icons.entry_hash if saved.icons else None,
            source_path=saved.wallpaper.source_path,
        )

        # Step 5 — reload desktop consumers (outside lock, fire-and-report)
        reload_failures: list[str] = []
        for reloader in self._reloaders:
            try:
                ok = reloader.reload()
            except Exception as exc:
                logger.warning("desktop reload failed for %s: %s", type(reloader).__name__, exc)
                ok = False
            if not ok:
                reload_failures.append(type(reloader).__name__)

        return ReconcileResult(
            repointed=repointed,
            skipped=skipped,
            state=saved,
            cache_regenerated=regenerated,
            reload_failures=reload_failures,
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
            cache_entry_path(self._state_root, "wallpapers", state.wallpaper.content_hash)
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
        # P6 — if palette is None but icons is not, icons cannot exist; degrade
        if icons is not None and palette is None:
            logger.warning("reconcile: icons present but palette is null; icons degraded to null")
            icons = None
        elif (
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
        cached_png = cache_entry_path(self._state_root, "wallpapers", wh) / "wallpaper.png"
        if cached_png.is_file():
            # P4 — verify content hash for existing file
            try:
                if hash_file(cached_png) != wh:
                    raise RuntimeError(
                        f"wallpaper cache entry {wh} content does not match "
                        f"its hash address: {cached_png}"
                    )
            except OSError as exc:
                raise RuntimeError(f"wallpaper cache entry {wh} cannot be verified: {exc}") from exc
            return
        if cached_png.exists():
            # Exists but not a regular file (dir/symlink) — treat as missing
            raise RuntimeError(
                f"wallpaper cache entry {wh} exists but is not a regular file: {cached_png}"
            )
        source = state.wallpaper.source_path
        # P5 — whitespace/relative guard
        if not source or not source.strip():
            raise RuntimeError(
                f"wallpaper cache entry {wh} is missing and the state has no "
                "source_path; the wallpaper cache entry cannot be rebuilt "
                "without its source"
            )
        src = Path(source.strip())
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
        log: bool = True,
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
                if log:
                    logger.warning("reconcile: %s layer is null; consumer symlink skipped", layer)

        return skipped

    def _cleanup_stale_symlinks(
        self,
        state: DesktopState,
        monitor_names: list[str],
        repointed: list[Path],
    ) -> list[str]:
        """Remove stale current/ symlinks not in expected set (D2)."""
        current_dir = self._state_root / "current"
        if not current_dir.is_dir():
            return []
        expected_names = {f"wallpaper-{n}.png" for n in monitor_names}
        if state.palette is not None:
            expected_names.update({"colors.conf", "colors.gtk.css", "colors.yaml"})
        if state.effects is not None:
            expected_names.add("effects")
        if state.icons is not None:
            expected_names.add("icons")

        removed: list[str] = []
        for item in current_dir.iterdir():
            # Only consider wallpaper symlinks and known consumer names
            if item.name.startswith("wallpaper-") and item.name.endswith(".png"):
                if item.name not in expected_names:
                    try:
                        # Remove stale monitor symlink (file or symlink)
                        if item.is_symlink() or item.is_file():
                            item.unlink()
                            removed.append(item.name)
                    except OSError:
                        pass
            elif item.name in ("effects", "icons"):
                if item.name not in expected_names:
                    try:
                        if item.is_symlink() or item.exists():
                            # For directory symlinks, unlink removes symlink only
                            item.unlink()
                            removed.append(item.name)
                    except OSError:
                        pass
        return removed

    def _build_expected_targets(self, state: DesktopState) -> dict[str, Path]:
        """Map symlink names to correct cache entry targets from current.json."""
        targets: dict[str, Path] = {}
        for monitor_name in state.monitors:
            if (
                "/" in monitor_name
                or "\\" in monitor_name
                or monitor_name.strip() != monitor_name
                or ".." in monitor_name
            ):
                raise ValueError(
                    f"monitor name must not contain path separators, got {monitor_name!r}",
                )
            targets[f"wallpaper-{monitor_name}.png"] = (
                cache_entry_path(
                    self._state_root,
                    "wallpapers",
                    state.wallpaper.content_hash,
                )
                / "wallpaper.png"
            )
        if state.palette is not None:
            pal_dir = cache_entry_path(self._state_root, "palettes", state.palette.entry_hash)
            for artifact in ("colors.conf", "colors.gtk.css", "colors.yaml"):
                targets[artifact] = pal_dir / artifact
        if state.effects is not None:
            targets["effects"] = cache_entry_path(
                self._state_root,
                "effects",
                state.effects.entry_hash,
            )
        if state.icons is not None:
            targets["icons"] = cache_entry_path(
                self._state_root,
                "icons",
                state.icons.entry_hash,
            )
        return targets

    def _revert_stale_symlinks(self, state: DesktopState) -> list[Path]:
        """Revert current/ symlinks whose targets don't match current.json."""
        current_dir = self._state_root / "current"
        if not current_dir.is_dir():
            return []
        expected = self._build_expected_targets(state)
        reverted: list[Path] = []
        for name, expected_target in expected.items():
            link = current_dir / name
            if not link.is_symlink():
                continue
            # Use resolved paths for comparison; also handle dangling symlinks.
            try:
                actual_resolved = link.resolve()
            except OSError:
                actual_resolved = link
            try:
                expected_resolved = expected_target.resolve()
            except OSError:
                expected_resolved = expected_target
            # Check both resolved equality and hash segment containment for robustness.
            if actual_resolved != expected_resolved:
                _repoint_symlink(link, expected_target)
                reverted.append(link)
            else:
                # Resolved equal but still verify hash segment is present to catch edge cases
                # where symlink target string is semantically equal but via different traversal.
                # No-op if already matching.
                pass
        return reverted
