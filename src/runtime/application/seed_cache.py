"""First-run self-seeding use case — bootstraps desktop state from provisioning output.

Implements AD-1 (hexagonal), AD-5 (state_root), AD-6 (swap symlinks lead),
AD-11 (first-run self-seeding), AD-14 (domain purity), AD-15 (cross-package
boundary), AD-16 (hardlink), AD-17 (consumer wiring).

Scope boundary (Story 1.11):
- One-time bootstrap when ``current.json`` is absent and provisioning's
  ``generated/`` output exists.
- Does NOT implement ApplyWallpaperUseCase (1.13), ReconcileDesktopState
  (Epic 2), InspectState (Epic 3), or desktop reload adapters. Monitor
  names come from the injected ``IMonitorSource`` (real detection via
  ``HyprlandMonitorSource``); when no source is injected or detection is
  unavailable, the legacy single ``DEFAULT_MONITOR`` default is used.

Architecture:
- Lives in ``application/`` (use-case layer) per AD-1, AD-13
- Orchestrates injected ports and the injected ``CacheSeeder`` adapter;
  constructs no concrete adapters itself (composition root wires them)
- Single-flight: acquires the injected ``ISeedMutex`` and re-checks
  ``load_current()`` inside the critical section (double-checked locking),
  so concurrent CLI invocations cannot double-seed
- Failure policy: the palette is a hard dependency (consumers and icon
  rendering both require it) — a palette-producer failure aborts seeding entirely so
  the next command retries. Effects/icons degrade gracefully with a
  visible warning.
- No raw ``os``/``json`` I/O in use case (delegated to adapters)
- Cross-package boundary respected (AD-15): never imports provisioning code
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.appearance import AppearanceGenerator
from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.application.bar_backdrop import with_bar_backdrops
from runtime.application.derive import DerivationPipeline, find_skin_manifest
from runtime.domain.models import (
    DEFAULT_MONITOR,
    DEFAULT_SKIN,
    AppearanceEntry,
    BackendType,
    DesktopState,
    EffectsEntry,
    FitMode,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteEntry,
    SkinEntry,
    SkinUnavailableError,
    WallpaperEntry,
)
from runtime.ports.appearance_generator import IAppearanceGenerator
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.icon_renderer import IIconRenderer
from runtime.ports.monitor_source import IMonitorSource
from runtime.ports.seed_mutex import ISeedMutex
from runtime.ports.skin_expander import ISkinExpander
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

    Constructor receives ports, the ``CacheSeeder`` adapter and an
    injected ``IMonitorSource`` (dependency inversion): the use case
    wires no concrete adapters itself. When no monitor source is
    injected, seeding falls back to the legacy single ``DEFAULT_MONITOR``
    (the historical Phase-2 default).
    """

    def __init__(
        self,
        state_repo: IStateRepository,
        palette_producer: IColorSchemeGenerator,
        weg: IEffectsGenerator,
        itr: IIconRenderer,
        factory: IWallpaperBackendFactory,
        install_spine: Path,
        state_root: Path,
        seeder: CacheSeeder,
        mutex: ISeedMutex,
        monitor_source: IMonitorSource | None = None,
        skin_name: str = DEFAULT_SKIN,
        skin_expander: ISkinExpander | None = None,
        appearance_generator: IAppearanceGenerator | None = None,
        appearance_keep: int = 5,
    ) -> None:
        self._state_repo = state_repo
        self._factory = factory
        self._install_spine = install_spine
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex
        self._monitor_source = monitor_source
        self._skin_name = skin_name
        #: The v2 composed appearance plane. Default to the real generator:
        #: publication is part of the normal seed flow, not an opt-in.
        self._appearance = appearance_generator or AppearanceGenerator(state_root)
        self._appearance_keep = appearance_keep
        # Shared derivation plumbing (Story 1.13): spine discovery + per-layer
        # ensure-entry pattern live in application/derive.py; seed and apply
        # compose the same pipeline so behavior stays identical.
        self._pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            skins=skin_expander,
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

        # 2. Verify provisioning output exists. The wallpaper itself lives
        # in the assets role's output (Story 2-6 AC 2): the
        # ``wallpapers.tar.gz`` tarball unpacks to ``<install>/wallpapers/``
        # including ``default.png``. The cached-model spec pins this
        # location: ``Hash <install>/wallpapers/default.png → wh``.
        # Palette/effects/icons are re-derived here via the shared
        # DerivationPipeline (palette/WEG/ITR subprocess calls), NOT imported
        # from ``<install>/generated/`` — the seed re-runs the
        # derivations so the cache entry hashes match the inputs WEG/ITR
        # will use at runtime (same hash formula).
        wallpapers_dir = self._install_spine / "wallpapers"
        default_png = wallpapers_dir / "default.png"
        if not default_png.is_file():
            raise RuntimeError(
                f"default wallpaper not found: {default_png} "
                f"(install_spine={self._install_spine}); the assets role "
                f"(Story 2-6) must run before the runtime's first seed"
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

        # Monitor names from the injected source when available (real
        # outputs via HyprlandMonitorSource); fall back to the legacy
        # single DEFAULT_MONITOR when detection is unavailable.
        detected = (
            self._monitor_source.detect_monitors() if self._monitor_source is not None else []
        )
        monitor_names = detected or [DEFAULT_MONITOR]

        # Populate cache entries
        # 5a. Wallpaper: hardlink into cache (idempotent after a crashed run)
        cached_wallpaper = self._seeder.hardlink_wallpaper(default_png, wallpaper_hash)
        self._seeder.write_wallpaper_meta(
            wallpaper_hash=wallpaper_hash,
            source_path=str(default_png),
        )

        # 5b. Palette via the active producer — hard dependency (AC 2): failure aborts seeding
        palette_entry = self._populate_palette(default_png, wallpaper_hash)

        # 5c/5d. Effects via WEG, icons via ITR — degrade gracefully with a
        # visible warning; history schema allows null entries (AC 3)
        effects_entry = self._populate_effects(default_png, wallpaper_hash)
        icons_entry = self._populate_icons(palette_entry.entry_hash)
        skin_entry = self._populate_skin()
        appearance_entry = self._populate_appearance(palette_entry, skin_entry)

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

        monitors = with_bar_backdrops(
            monitors,
            self._state_root,
            source_paths={wallpaper_hash: default_png},
        )

        state = DesktopState(
            schema_version=2,
            wallpaper=wallpaper_entry,
            monitors=monitors,
            palette=palette_entry,
            effects=effects_entry,
            icons=icons_entry,
            applied_at=now,
            skin=skin_entry,
        )

        # 7. Swap sequence (AD-6, AD-17)
        # 7a. Repoint current/ symlinks (last step before save)
        self._seeder.repoint_current_symlinks(
            wallpaper_target=cached_wallpaper,
            monitor_names=monitor_names,
            palette_entry_hash=palette_entry.entry_hash,
            effects_entry_hash=effects_entry.entry_hash if effects_entry else None,
            icons_entry_hash=icons_entry.entry_hash if icons_entry else None,
            skin_entry=skin_entry,
        )

        # 7a1. The one appearance pointer + convenience links. The v1 skin
        # pointers are still repointed below (migration window).
        if appearance_entry is not None:
            self._seeder.publish_appearance(self._install_spine, appearance_entry)

        # 7a2. R2 consumer-path symlinks (Epic 4): point the spine consumer
        # path at current/ so desktop consumers read the active palette.
        # Runs after the current/ repoint (the target must exist first).
        self._seeder.repoint_consumer_symlinks(
            self._install_spine,
            palette_entry.entry_hash,
            skin_entry=skin_entry,
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
            skin_hash=skin_entry.entry_hash if skin_entry else None,
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

    def _populate_appearance(
        self, palette_entry: PaletteEntry, skin_entry: SkinEntry | None
    ) -> AppearanceEntry | None:
        """Compose the v2 appearance generation. Fail-closed on any error."""
        if skin_entry is None:
            return None
        try:
            manifest_path, _source = find_skin_manifest(
                skin_entry.name, self._install_spine
            )
            entry, _cache_hit = self._appearance.ensure(
                palette_entry=palette_entry,
                skin_entry=skin_entry,
                install_spine=self._install_spine,
                manifest_path=manifest_path,
                keep=self._appearance_keep,
            )
            return entry
        except Exception as exc:
            logger.error(
                "seeding: appearance generation failed; previous appearance kept: %s", exc
            )
            return None

    def _populate_skin(self) -> SkinEntry | None:
        """Populate the selected skin. Fail-closed: None (with structured log) on failure."""
        if not self._pipeline.has_skin_expander:
            return None
        try:
            entry, _cache_hit = self._pipeline.ensure_skin(self._skin_name)
            return entry
        except SkinUnavailableError as exc:
            logger.error(
                "seeding: skin derivation failed; continuing without a skin: "
                "skin=%s reason=%s manifest=%s templates=%s",
                exc.name,
                exc.reason,
                exc.manifest_path,
                exc.templates_dir,
            )
            return None
        except Exception as exc:
            logger.error("seeding: skin derivation failed unexpectedly; continuing: %s", exc)
            return None
