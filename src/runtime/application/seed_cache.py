"""First-run self-seeding use case — bootstraps desktop state from provisioning output.

Implements AD-1 (hexagonal), AD-5 (state_root), AD-6 (swap symlinks lead),
AD-11 (first-run self-seeding), AD-14 (domain purity), AD-15 (cross-package
boundary), AD-16 (hardlink), AD-17 (consumer wiring).

Scope boundary (Story 1.11):
- One-time bootstrap when ``current.json`` is absent and provisioning's
  ``generated/`` output exists.
- Does NOT implement ApplyWallpaperUseCase (1.13), ReconcileDesktopState
  (Epic 2), InspectState (Epic 3), full monitor detection, or desktop
  reload adapters.

Architecture:
- Lives in ``application/`` (use-case layer) per AD-1, AD-13
- Orchestrates injected ports and the injected ``CacheSeeder`` adapter;
  constructs no concrete adapters itself (composition root wires them)
- Single-flight: acquires the injected ``ISeedMutex`` and re-checks
  ``load_current()`` inside the critical section (double-checked locking),
  so concurrent CLI invocations cannot double-seed
- Failure policy: the palette is a hard dependency (consumers and icon
  rendering both require it) — a CSG failure aborts seeding entirely so
  the next command retries. Effects/icons degrade gracefully with a
  visible warning.
- No raw ``os``/``json`` I/O in use case (delegated to adapters)
- Cross-package boundary respected (AD-15): never imports provisioning code
"""

from __future__ import annotations

import logging
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
from runtime.ports.wallpaper_backend_factory import IWallpaperBackendFactory

logger = logging.getLogger(__name__)


class SeedCacheUseCase:
    """First-run self-seeding orchestrator.

    When ``current.json`` is absent and provisioning's ``generated``
    output exists, seeds the cache from default wallpaper/effects/icons,
    writes ``current.json`` with ``schema_version: 2``, creates
    ``current/`` symlinks, and appends ``history.jsonl``.

    The seeder performs the identical swap sequence as
    ``ReconcileDesktopStateUseCase`` (AD-6) but exactly once at first boot.

    Constructor receives ports and the ``CacheSeeder`` adapter (dependency
    inversion): the use case wires no concrete adapters itself.
    """

    def __init__(
        self,
        state_repo: IStateRepository,
        csg: IColorSchemeGenerator,
        weg: IEffectsGenerator,
        itr: IIconRenderer,
        factory: IWallpaperBackendFactory,
        install_spine: Path,
        state_root: Path,
        seeder: CacheSeeder,
        mutex: ISeedMutex,
    ) -> None:
        self._state_repo = state_repo
        self._factory = factory
        self._install_spine = install_spine
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex
        # Shared derivation plumbing (Story 1.13): spine discovery + per-layer
        # ensure-entry pattern live in application/derive.py; seed and apply
        # compose the same pipeline so behavior stays identical.
        self._pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
        )

    def run(self) -> None:
        """Execute first-run seeding if needed.

        Steps:
        1. Fail loudly if current.json exists but is corrupt/incompatible
           (seeding must not silently paper over unreadable state)
        2. Verify install_spine/generated/default.png exists
        3. Acquire the seed mutex and re-check load_current() — concurrent
           first runs serialize; the loser observes the winner's state and
           no-ops
        4. Seed: cache entries → symlinks → current.json → history.jsonl

        Raises:
            RuntimeError: if provisioning output not found, or palette
                (hard dependency) generation fails
            ValueError: propagated from a corrupt/incompatible current.json
            OSError: on filesystem operations
        """
        # 1. AC 4 fast path — also the corrupt-state guard (D3): a
        # ValueError/RuntimeError from load_current() propagates loudly
        # instead of being swallowed into a reseed.
        existing = self._state_repo.load_current()
        if existing is not None:
            return

        # 2. Verify provisioning output exists
        generated_dir = self._install_spine / "generated"
        if not generated_dir.is_dir():
            raise RuntimeError(
                f"provisioning output not found: {generated_dir} "
                f"(install_spine={self._install_spine})"
            )

        default_png = generated_dir / "default.png"
        if not default_png.is_file():
            raise RuntimeError(
                f"default wallpaper not found: {default_png} (install_spine={self._install_spine})"
            )

        # 3. Single-flight: double-checked locking around load_current()
        with self._mutex.hold():
            existing = self._state_repo.load_current()
            if existing is not None:
                return
            self._seed(default_png)

    def _seed(self, default_png: Path) -> None:
        """Perform the seeding swap sequence. Caller holds the seed mutex."""
        wallpaper_hash = hash_file(default_png)

        # Detect monitors (stub: single DP-1 for Phase 2)
        monitor_names = [DEFAULT_MONITOR]

        # Populate cache entries
        # 5a. Wallpaper: hardlink into cache (idempotent after a crashed run)
        cached_wallpaper = self._seeder.hardlink_wallpaper(default_png, wallpaper_hash)
        self._seeder.write_wallpaper_meta(
            wallpaper_hash=wallpaper_hash,
            source_path=str(default_png),
        )

        # 5b. Palette via CSG — hard dependency (AC 2): failure aborts seeding
        palette_entry = self._populate_palette(default_png, wallpaper_hash)

        # 5c/5d. Effects via WEG, icons via ITR — degrade gracefully with a
        # visible warning; history schema allows null entries (AC 3)
        effects_entry = self._populate_effects(default_png, wallpaper_hash)
        icons_entry = self._populate_icons(palette_entry.entry_hash)

        # 6. Construct domain objects (real hashes from the adapters — the
        # seeded state must agree with the meta.json beside each cache entry)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        wallpaper_entry = WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wallpaper_hash,
            source_path="",
            imported_at=now,
        )

        monitors: dict[str, MonitorWallpaperConfig] = {}
        for name in monitor_names:
            monitors[name] = MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wallpaper_hash,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )

        state = DesktopState(
            schema_version=2,
            wallpaper=wallpaper_entry,
            monitors=monitors,
            palette=palette_entry,
            effects=effects_entry,
            icons=icons_entry,
            applied_at=now,
        )

        # 7. Swap sequence (AD-6, AD-17)
        # 7a. Repoint current/ symlinks (last step before save)
        self._seeder.repoint_current_symlinks(
            wallpaper_target=cached_wallpaper,
            monitor_names=monitor_names,
            palette_entry_hash=palette_entry.entry_hash,
            effects_entry_hash=effects_entry.entry_hash if effects_entry else None,
            icons_entry_hash=icons_entry.entry_hash if icons_entry else None,
        )

        # 7b. Write current.json via state_repo (atomic tmp + os.replace)
        self._state_repo.save(state)

        # 7c. Append history.jsonl (atomic O_APPEND + fsync)
        self._seeder.append_history(
            trigger="seed",
            wallpaper_hash=wallpaper_hash,
            palette_hash=palette_entry.entry_hash,
            effects_hash=effects_entry.entry_hash if effects_entry else None,
            icons_hash=icons_entry.entry_hash if icons_entry else None,
            source_path="",
        )

    def _populate_palette(self, wallpaper_path: Path, wallpaper_hash: str) -> PaletteEntry:
        """Populate palette cache via the shared pipeline. Hard dependency.

        Any failure raises (aborts seeding) with a ``palette seeding
        failed:`` context prefix.
        """
        try:
            entry, _cache_hit = self._pipeline.ensure_palette(wallpaper_path, wallpaper_hash)
            return entry
        except Exception as exc:
            raise RuntimeError(f"palette seeding failed: {exc}") from exc

    def _populate_effects(self, wallpaper_path: Path, wallpaper_hash: str) -> EffectsEntry | None:
        """Populate effects cache via the shared pipeline. None (with warning) on failure."""
        try:
            entry, _cache_hit = self._pipeline.ensure_effects(wallpaper_path, wallpaper_hash)
            return entry
        except Exception as exc:
            logger.warning("seeding: effects generation failed; continuing: %s", exc)
            return None

    def _populate_icons(self, palette_entry_hash: str) -> IconsEntry | None:
        """Populate icons cache via the shared pipeline. None (with warning) on failure."""
        try:
            entry, _cache_hit = self._pipeline.ensure_icons(palette_entry_hash)
            return entry
        except Exception as exc:
            logger.warning("seeding: icon rendering failed; continuing: %s", exc)
            return None
