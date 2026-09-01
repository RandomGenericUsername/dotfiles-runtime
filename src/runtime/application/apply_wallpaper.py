"""Apply-wallpaper use case — derive, cache, persist the desktop state.

Implements AD-1 (hexagonal), AD-2 (input changes invalidate), AD-5
(state_root), AD-6 (swap symlinks lead), AD-11 (read-only spine reads),
AD-14 (domain purity), AD-15 (cross-package boundary), AD-18 (preserve
monitor configs), AD-19/AD-20 (CLI rendering).

Scope boundary (Story 1.13 — the Epic-1-to-Epic-2 seam):
- Derives the three layers for a user-chosen wallpaper, ensures cache
  entries (cache hit = entry-dir existence; zero tool invocations on
  hit), and persists ``current.json`` (schema_version 2, atomic).
- Does NOT repoint ``current/`` symlinks, does NOT append
  ``history.jsonl``, and does NOT invoke any desktop reload — the swap
  sequence is owned by ``ReconcileDesktopStateUseCase`` (Epic 2).
  ``current.json`` is the only persisted artifact of this story.

Architecture:
- Lives in ``application/`` (use-case layer) per AD-1, AD-13
- Orchestrates injected ports and the injected ``CacheSeeder`` adapter;
  constructs no concrete adapters itself (composition root wires them)
- No raw ``os``/``json`` FS I/O in the use case (delegated to adapters)
- Failure policy mirrors ``SeedCacheUseCase``: palette is a hard
  dependency (``palette apply failed:``), effects/icons degrade
  gracefully with a visible warning; corrupt state propagates loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.application.derive import DerivationPipeline
from runtime.domain.models import (
    DEFAULT_MONITOR,
    BackendType,
    DesktopState,
    EffectsEntry,
    FitMode,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteEntry,
    WallpaperEntry,
)
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.icon_renderer import IIconRenderer
from runtime.ports.seed_mutex import ISeedMutex
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplyWallpaperResult:
    """Outcome of one ``ApplyWallpaperUseCase.run`` invocation."""

    wallpaper_hash: str
    palette: PaletteEntry
    effects: EffectsEntry | None
    icons: IconsEntry | None
    cache_hit_palette: bool
    cache_hit_effects: bool
    cache_hit_icons: bool
    state: DesktopState


class ApplyWallpaperUseCase:
    """Derive, cache, and persist the state for a user-set wallpaper.

    Steps:
    1. Resolve + validate the input (absolute, existing regular file)
    2. Load current state (fail-fast corrupt-state guard)
    3. Hash the wallpaper (sha256 of file bytes) and import it into the
       cache via the seeder (copy policy — the cache owns its bytes for
       user-supplied files; content-verified; meta.json write-once)
    4. Ensure palette (hard), effects and icons (graceful) cache entries
       via the shared ``DerivationPipeline`` — outside the mutex (staging
       is race-safe; tool invocations stay parallel)
    5. Hold the state mutex (blocking) around the read-modify-write of
       ``current.json`` — a concurrent first-run seed can never be
       overtaken by an applier that observed absent state; monitors
       preserved with ``source_hash`` updated (AD-18), defaulted when no
       state or empty monitors
    6. Save ``current.json`` via the injected state repository (atomic)

    Constructor receives ports, the injected ``CacheSeeder`` adapter and
    the state mutex (dependency inversion): the use case wires no concrete
    adapters itself.
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
        self._install_spine = install_spine
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

    def run(self, image_path: Path) -> ApplyWallpaperResult:
        """Apply a wallpaper: derive → cache → persist ``current.json``.

        Raises:
            ValueError: if the input path is missing, empty, or not a
                regular file; or propagated from a corrupt current.json
            RuntimeError: if palette (hard dependency) derivation fails,
                or the cache holds content that contradicts its hash
            OSError: on filesystem failures (unreadable input, etc.)
        """
        img = self._validate_input(image_path)

        # Corrupt-state guard (fail fast, mirrors seed's fast path): a
        # ValueError/RuntimeError from a corrupt store propagates loudly —
        # do NOT swallow into a fresh state. Re-read inside the critical
        # section below (double-checked pattern).
        self._state_repo.load_current()

        wallpaper_hash = hash_file(img)

        # Wallpaper layer: import into the cache (copy policy — cache owns
        # its bytes; idempotent, content-verified, meta.json write-once).
        self._seeder.import_wallpaper(img, wallpaper_hash, source_mutable=True)

        # Palette (hard dependency): failure aborts the apply — save()
        # happens only after all layers succeed/degrade, so current.json
        # is left unchanged.
        try:
            palette, cache_hit_palette = self._pipeline.ensure_palette(img, wallpaper_hash)
        except Exception as exc:
            raise RuntimeError(f"palette apply failed: {exc}") from exc

        # Effects + icons (graceful degradation): entry None on failure,
        # current.json field null (history schema allows nulls).
        effects: EffectsEntry | None = None
        cache_hit_effects = False
        try:
            effects, cache_hit_effects = self._pipeline.ensure_effects(img, wallpaper_hash)
        except Exception as exc:
            logger.warning("apply: effects generation failed; continuing: %s", exc)

        icons: IconsEntry | None = None
        cache_hit_icons = False
        try:
            icons, cache_hit_icons = self._pipeline.ensure_icons(palette.entry_hash)
        except Exception as exc:
            logger.warning("apply: icon rendering failed; continuing: %s", exc)

        # Persist ONLY current.json — no symlink repoint, no history
        # append, no reload (AC 6; ReconcileDesktopStateUseCase owns the
        # swap sequence in Epic 2). The read-modify-write of the state is
        # serialized against a concurrent first-run seed (and other
        # applies) via the blocking state mutex: the authoritative
        # load_current() happens inside the critical section so a seed
        # landing between derivation and save can never be overtaken.
        with self._mutex.hold(blocking=True):
            existing = self._state_repo.load_current()
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            wallpaper_entry = WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=wallpaper_hash,
                source_path=str(img),
                imported_at=now,
            )
            state = DesktopState(
                schema_version=2,
                wallpaper=wallpaper_entry,
                monitors=self._build_monitors(existing, wallpaper_hash),
                palette=palette,
                effects=effects,
                icons=icons,
                applied_at=now,
            )
            self._state_repo.save(state)

        return ApplyWallpaperResult(
            wallpaper_hash=wallpaper_hash,
            palette=palette,
            effects=effects,
            icons=icons,
            cache_hit_palette=cache_hit_palette,
            cache_hit_effects=cache_hit_effects,
            cache_hit_icons=cache_hit_icons,
            state=state,
        )

    @staticmethod
    def _validate_input(image_path: Path) -> Path:
        """Resolve the input to an absolute path and validate it.

        Rejects missing files, directories, and anything that is not a
        regular file with ``ValueError`` (the CLI maps this to a
        non-zero exit). Empty paths resolve to the CWD (a directory) and
        are rejected by the same guard.
        """
        img = image_path.expanduser().resolve()
        if not img.exists():
            raise ValueError(f"wallpaper image not found: {img}")
        if not img.is_file():
            raise ValueError(f"wallpaper image must be a regular file, got: {img}")
        return img

    @staticmethod
    def _build_monitors(
        existing: DesktopState | None, wallpaper_hash: str
    ) -> dict[str, MonitorWallpaperConfig]:
        """Preserve existing monitor configs, updating only source_hash.

        When ``existing`` is None (seed skipped — no provisioning spine)
        or its ``monitors`` dict is empty, default to the seeder's
        convention: single ``DEFAULT_MONITOR`` monitor, ``hyprpaper``
        backend, ``cover`` fit (AD-18: a wallpaper change must never
        silently reset a user's per-monitor backend).
        """
        if existing is not None and existing.monitors:
            return {
                name: MonitorWallpaperConfig(
                    backend=cfg.backend,
                    source_hash=wallpaper_hash,
                    fit_mode=cfg.fit_mode,
                    mpv_options=cfg.mpv_options,
                    ipc_socket=cfg.ipc_socket,
                )
                for name, cfg in existing.monitors.items()
            }
        return {
            DEFAULT_MONITOR: MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wallpaper_hash,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        }
