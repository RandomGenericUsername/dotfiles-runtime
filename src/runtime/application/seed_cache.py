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
from typing import cast

from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.domain.models import (
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

# Default monitor name for Phase 2 (full detection deferred to later story)
_DEFAULT_MONITOR = "DP-1"


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
        self._csg = csg
        self._weg = weg
        self._itr = itr
        self._factory = factory
        self._install_spine = install_spine
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex

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
        monitor_names = [_DEFAULT_MONITOR]

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
        """Populate palette cache via CSG. Returns the palette entry.

        Palette is a hard dependency: any failure raises (aborts seeding)
        with a ``palette seeding failed:`` context prefix. The adapter
        generates into ``staging/<peh>`` (its output-dir name contract),
        artifacts are drained into the staging root, and ``meta.json`` is
        written there so ``populate_via_staging``'s rename-to-target
        contract is satisfied with real hashes throughout.
        """
        try:
            return self._populate_palette_inner(wallpaper_path, wallpaper_hash)
        except Exception as exc:
            raise RuntimeError(f"palette seeding failed: {exc}") from exc

    def _populate_palette_inner(
        self, wallpaper_path: Path, wallpaper_hash: str
    ) -> PaletteEntry:
        from runtime.adapters.cache import cache_entry_path, populate_via_staging
        from runtime.adapters.hashing import canonical_hash_dir, palette_entry_hash

        templates_dir = self._find_templates_dir()
        if templates_dir is None:
            raise RuntimeError(
                f"CSG templates dir not found (install_spine={self._install_spine})"
            )

        template_set_hash = canonical_hash_dir(templates_dir)
        peh = palette_entry_hash(wallpaper_hash, template_set_hash)
        target = cache_entry_path(self._state_root, "palettes", peh)
        entry_holder: list[PaletteEntry] = []

        def _populate(staging: Path) -> None:
            work = staging / peh
            generated = self._csg.generate(wallpaper_path, work)
            if generated.entry_hash != peh:
                raise RuntimeError(
                    f"adapter entry hash mismatch: adapter={generated.entry_hash} "
                    f"computed={peh} (templates dir divergence)"
                )
            self._seeder.drain_work_dir(work, staging)
            self._seeder.write_palette_meta_in(
                staging,
                entry_hash=peh,
                source_wallpaper_hash=wallpaper_hash,
                input_template_hash=template_set_hash,
                artifact_hashes={
                    "colors.yaml": generated.artifact_hashes["colors_yaml"],
                    "colors.conf": generated.artifact_hashes["colors_conf"],
                    "colors.gtk.css": generated.artifact_hashes["colors_gtk_css"],
                },
                generated_at=generated.generated_at,
            )
            entry_holder.append(
                PaletteEntry(
                    hash_algorithm="sha256",
                    kind="palette",
                    entry_hash=peh,
                    source_wallpaper_hash=wallpaper_hash,
                    input_template_hash=template_set_hash,
                    artifact_hashes=generated.artifact_hashes,
                    generated_at=generated.generated_at,
                )
            )

        created = populate_via_staging(target, _populate)
        if created:
            return entry_holder[0]
        # Entry already existed (crashed prior run): rebuild from its
        # meta.json so the state still carries real hashes.
        return self._seeder.load_palette_entry(target)

    def _populate_effects(
        self, wallpaper_path: Path, wallpaper_hash: str
    ) -> EffectsEntry | None:
        """Populate effects cache via WEG. None (with warning) on failure."""
        try:
            from runtime.adapters.cache import cache_entry_path, populate_via_staging
            from runtime.adapters.hashing import effects_entry_hash

            catalog_path = self._find_effects_catalog()
            if catalog_path is None:
                logger.warning("seeding: effects catalog not found; effects disabled")
                return None

            catalog_hash = hash_file(catalog_path)
            eeh = effects_entry_hash(wallpaper_hash, catalog_hash)
            target = cache_entry_path(self._state_root, "effects", eeh)
            entry_holder: list[EffectsEntry] = []

            def _populate(staging: Path) -> None:
                work = staging / eeh
                generated = self._weg.generate(wallpaper_path, work)
                if generated.entry_hash != eeh:
                    raise RuntimeError(
                        f"adapter entry hash mismatch: adapter={generated.entry_hash} "
                        f"computed={eeh}"
                    )
                self._seeder.drain_work_dir(work, staging)
                self._seeder.write_effects_meta_in(
                    staging,
                    entry_hash=eeh,
                    source_wallpaper_hash=wallpaper_hash,
                    input_catalog_hash=catalog_hash,
                    artifact_hashes=cast("dict[str, str]", generated.artifact_hashes),
                    generated_at=generated.generated_at,
                )
                entry_holder.append(
                    EffectsEntry(
                        hash_algorithm="sha256",
                        kind="effects",
                        entry_hash=eeh,
                        source_wallpaper_hash=wallpaper_hash,
                        input_catalog_hash=catalog_hash,
                        artifact_hashes=generated.artifact_hashes,
                        generated_at=generated.generated_at,
                    )
                )

            created = populate_via_staging(target, _populate)
            if created:
                return entry_holder[0]
            return self._seeder.load_effects_entry(target)
        except Exception as exc:
            logger.warning("seeding: effects generation failed; continuing: %s", exc)
            return None

    def _populate_icons(self, palette_entry_hash: str) -> IconsEntry | None:
        """Populate icons cache via ITR. None (with warning) on failure."""
        try:
            from runtime.adapters.cache import cache_entry_path, populate_via_staging
            from runtime.adapters.hashing import canonical_hash_dir, icons_entry_hash

            templates_dir = self._find_icon_templates()
            mappings_path = self._find_icon_mappings()
            if templates_dir is None or mappings_path is None:
                logger.warning("seeding: icon templates/mappings not found; icons disabled")
                return None

            if templates_dir.is_dir():
                templates_hash = canonical_hash_dir(templates_dir)
            elif templates_dir.is_file():
                templates_hash = hash_file(templates_dir)
            else:
                logger.warning("seeding: icon templates path missing; icons disabled")
                return None

            if mappings_path.is_dir():
                mappings_hash_val = canonical_hash_dir(mappings_path)
            elif mappings_path.is_file():
                mappings_hash_val = hash_file(mappings_path)
            else:
                logger.warning("seeding: icon mappings path missing; icons disabled")
                return None

            ieh = icons_entry_hash(palette_entry_hash, templates_hash, mappings_hash_val)
            target = cache_entry_path(self._state_root, "icons", ieh)
            entry_holder: list[IconsEntry] = []

            def _populate(staging: Path) -> None:
                work = staging / ieh
                generated = self._itr.render(
                    palette_entry_hash, templates_dir, mappings_path, work
                )
                if generated.entry_hash != ieh:
                    raise RuntimeError(
                        f"adapter entry hash mismatch: adapter={generated.entry_hash} "
                        f"computed={ieh}"
                    )
                self._seeder.drain_work_dir(work, staging)
                self._seeder.write_icons_meta_in(
                    staging,
                    entry_hash=ieh,
                    source_palette_hash=palette_entry_hash,
                    input_templates_hash=templates_hash,
                    input_mappings_hash=mappings_hash_val,
                    artifact_hashes=cast("dict[str, str]", generated.artifact_hashes),
                    generated_at=generated.generated_at,
                )
                entry_holder.append(
                    IconsEntry(
                        hash_algorithm="sha256",
                        kind="icons",
                        entry_hash=ieh,
                        source_palette_hash=palette_entry_hash,
                        input_templates_hash=templates_hash,
                        input_mappings_hash=mappings_hash_val,
                        artifact_hashes=generated.artifact_hashes,
                        generated_at=generated.generated_at,
                    )
                )

            created = populate_via_staging(target, _populate)
            if created:
                return entry_holder[0]
            return self._seeder.load_icons_entry(target)
        except Exception as exc:
            logger.warning("seeding: icon rendering failed; continuing: %s", exc)
            return None

    def _find_templates_dir(self) -> Path | None:
        """Discover CSG templates directory."""
        # Check install spine first
        spine_templates = self._install_spine / "config" / "color-scheme-generator" / "templates"
        try:
            if spine_templates.is_dir():
                return spine_templates
        except OSError:
            pass

        # Repo fallback
        for parent in self._install_spine.parents:
            candidates = [
                parent
                / "src"
                / "cli-tools"
                / "color-scheme-generator"
                / "src"
                / "color_scheme_generator"
                / "defaults"
                / "templates",
                parent / "src" / "cli-tools" / "color-scheme-generator" / "defaults" / "templates",
            ]
            for candidate in candidates:
                try:
                    if candidate.is_dir():
                        return candidate
                except OSError:
                    continue
        return None

    def _find_effects_catalog(self) -> Path | None:
        """Discover WEG effects catalog."""
        # Check install spine first
        spine_catalog = self._install_spine / "config" / "weg" / "effects.yaml"
        try:
            if spine_catalog.is_file():
                return spine_catalog
        except OSError:
            pass

        # Repo fallback
        for parent in self._install_spine.parents:
            candidates = [
                parent
                / "src"
                / "cli-tools"
                / "wallpaper-effects-generator"
                / "src"
                / "wallpaper_effects_generator"
                / "defaults"
                / "effects.yaml",
                parent
                / "src"
                / "cli-tools"
                / "wallpaper-effects-generator"
                / "defaults"
                / "effects.yaml",
            ]
            for candidate in candidates:
                try:
                    if candidate.is_file():
                        return candidate
                except OSError:
                    continue
        return None

    def _find_icon_templates(self) -> Path | None:
        """Discover ITR icon templates directory."""
        # Check install spine first
        spine_templates = self._install_spine / "config" / "icon-templates-renderer" / "templates"
        try:
            if spine_templates.is_dir():
                return spine_templates
        except OSError:
            pass

        # Repo fallback
        for parent in self._install_spine.parents:
            candidates = [
                parent
                / "src"
                / "cli-tools"
                / "icon-templates-renderer"
                / "src"
                / "icon_templates_renderer"
                / "defaults"
                / "templates",
                parent / "src" / "cli-tools" / "icon-templates-renderer" / "defaults" / "templates",
            ]
            for candidate in candidates:
                try:
                    if candidate.is_dir():
                        return candidate
                except OSError:
                    continue
        return None

    def _find_icon_mappings(self) -> Path | None:
        """Discover ITR icon mappings."""
        # Check install spine first
        spine_mappings = self._install_spine / "config" / "icon-templates-renderer" / "icons.yaml"
        try:
            if spine_mappings.is_file():
                return spine_mappings
        except OSError:
            pass

        # Repo fallback
        for parent in self._install_spine.parents:
            candidates = [
                parent
                / "src"
                / "cli-tools"
                / "icon-templates-renderer"
                / "src"
                / "icon_templates_renderer"
                / "defaults"
                / "icons.yaml",
                parent
                / "src"
                / "cli-tools"
                / "icon-templates-renderer"
                / "defaults"
                / "icons.yaml",
            ]
            for candidate in candidates:
                try:
                    if candidate.exists():
                        return candidate
                except OSError:
                    continue
        return None
