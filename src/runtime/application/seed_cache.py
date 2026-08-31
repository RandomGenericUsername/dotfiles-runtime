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
- Imports only ``ports/``, ``domain/models.py``, and ``adapters/``
- No raw ``os``/``json`` I/O in use case (delegated to adapters)
- Cross-package boundary respected (AD-15): never imports provisioning code
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
from runtime.ports.state_repository import IStateRepository
from runtime.ports.wallpaper_backend_factory import IWallpaperBackendFactory

# Default monitor name for Phase 2 (full detection deferred to later story)
_DEFAULT_MONITOR = "DP-1"


class SeedCacheUseCase:
    """First-run self-seeding orchestrator.

    When ``current.json`` is absent and provisioning's ``generated/``
    output exists, seeds the cache from default wallpaper/effects/icons,
    writes ``current.json`` with ``schema_version: 2``, creates
    ``current/`` symlinks, and appends ``history.jsonl``.

    The seeder performs the identical swap sequence as
    ``ReconcileDesktopStateUseCase`` (AD-6) but exactly once at first boot.

    Constructor receives ports (dependency inversion) and paths.
    Adapter I/O delegated to injected ``CacheSeeder``.
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
    ) -> None:
        self._state_repo = state_repo
        self._csg = csg
        self._weg = weg
        self._itr = itr
        self._factory = factory
        self._install_spine = install_spine
        self._state_root = state_root
        self._seeder = CacheSeeder(state_root)

    def run(self) -> None:
        """Execute first-run seeding if needed.

        Steps:
        1. Check if current.json exists (load_current returns non-None → no-op)
        2. Verify install_spine/generated/ exists
        3. Hash default.png to get wallpaper_hash
        4. Detect monitors (stub: single DP-1)
        5. Populate cache entries (wallpaper hardlink, CSG, WEG, ITR)
        6. Construct domain objects
        7. Perform swap sequence: repoint symlinks → save → append history

        Raises:
            RuntimeError: if provisioning output not found
            OSError: on filesystem operations
        """
        # 1. AC 4: Skip if current.json already exists (non-first-run)
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

        # 3. Hash default.png
        default_png = generated_dir / "default.png"
        if not default_png.is_file():
            raise RuntimeError(
                f"default wallpaper not found: {default_png} (install_spine={self._install_spine})"
            )
        wallpaper_hash = hash_file(default_png)

        # 4. Detect monitors (stub: single DP-1 for Phase 2)
        monitor_names = [_DEFAULT_MONITOR]

        # 5. Populate cache entries
        palette_entry_hash_value: str | None = None
        effects_entry_hash_value: str | None = None
        icons_entry_hash_value: str | None = None

        # 5a. Wallpaper: hardlink into cache
        cached_wallpaper = self._seeder.hardlink_wallpaper(default_png, wallpaper_hash)
        self._seeder.write_wallpaper_meta(
            wallpaper_hash=wallpaper_hash,
            source_path=str(default_png),
        )

        # 5b. Palette via CSG
        palette_entry_hash_value = self._populate_palette(default_png, wallpaper_hash)

        # 5c. Effects via WEG
        effects_entry_hash_value = self._populate_effects(default_png, wallpaper_hash)

        # 5d. Icons via ITR (requires palette)
        if palette_entry_hash_value is not None:
            icons_entry_hash_value = self._populate_icons(palette_entry_hash_value)

        # 6. Construct domain objects
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        wallpaper_entry = WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wallpaper_hash,
            source_path=str(default_png),
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

        palette = self._reconstruct_palette(palette_entry_hash_value, wallpaper_hash, now)
        effects = self._reconstruct_effects(effects_entry_hash_value, wallpaper_hash, now)
        icons = self._reconstruct_icons(icons_entry_hash_value, palette_entry_hash_value, now)

        state = DesktopState(
            schema_version=2,
            wallpaper=wallpaper_entry,
            monitors=monitors,
            palette=palette,
            effects=effects,
            icons=icons,
            applied_at=now,
        )

        # 7. Swap sequence (AD-6, AD-17)
        # 7a. Repoint current/ symlinks (last step before save)
        self._seeder.repoint_current_symlinks(
            wallpaper_target=cached_wallpaper,
            monitor_names=monitor_names,
            palette_entry_hash=palette_entry_hash_value,
            effects_entry_hash=effects_entry_hash_value,
            icons_entry_hash=icons_entry_hash_value,
        )

        # 7b. Write current.json via state_repo (atomic tmp + os.replace)
        self._state_repo.save(state)

        # 7c. Append history.jsonl (atomic O_APPEND + fsync)
        self._seeder.append_history(
            trigger="seed",
            wallpaper_hash=wallpaper_hash,
            palette_hash=palette_entry_hash_value,
            effects_hash=effects_entry_hash_value,
            icons_hash=icons_entry_hash_value,
            source_path="",
        )

    def _populate_palette(self, wallpaper_path: Path, wallpaper_hash: str) -> str | None:
        """Populate palette cache via CSG. Returns entry hash or None on failure."""
        try:
            from runtime.adapters.cache import cache_entry_path, populate_via_staging
            from runtime.adapters.hashing import canonical_hash_dir, palette_entry_hash

            templates_dir = self._find_templates_dir()
            if templates_dir is None:
                return None

            template_set_hash = canonical_hash_dir(templates_dir)
            peh = palette_entry_hash(wallpaper_hash, template_set_hash)
            target = cache_entry_path(self._state_root, "palettes", peh)

            def _populate(staging: Path) -> None:
                self._csg.generate(wallpaper_path, staging)

            created = populate_via_staging(target, _populate)

            # Write meta.json if we created or target already exists
            if created or target.exists():
                artifact_hashes: dict[str, str] = {}
                for name in ("colors.yaml", "colors.conf", "colors.gtk.css"):
                    p = target / name
                    if p.exists():
                        artifact_hashes[name] = hash_file(p)
                self._seeder.write_palette_meta(
                    entry_hash=peh,
                    source_wallpaper_hash=wallpaper_hash,
                    input_template_hash=template_set_hash,
                    artifact_hashes=artifact_hashes,
                )
                return peh
        except FileNotFoundError, RuntimeError, OSError:
            pass
        return None

    def _populate_effects(self, wallpaper_path: Path, wallpaper_hash: str) -> str | None:
        """Populate effects cache via WEG. Returns entry hash or None on failure."""
        try:
            from runtime.adapters.cache import cache_entry_path, populate_via_staging
            from runtime.adapters.hashing import effects_entry_hash

            catalog_path = self._find_effects_catalog()
            if catalog_path is None:
                return None

            catalog_hash = hash_file(catalog_path)
            eeh = effects_entry_hash(wallpaper_hash, catalog_hash)
            target = cache_entry_path(self._state_root, "effects", eeh)

            def _populate(staging: Path) -> None:
                self._weg.generate(wallpaper_path, staging)

            created = populate_via_staging(target, _populate)

            if created or target.exists():
                artifact_hashes: dict[str, str] = {}
                for p in target.rglob("*.png"):
                    if p.is_file():
                        artifact_hashes[p.name] = hash_file(p)
                self._seeder.write_effects_meta(
                    entry_hash=eeh,
                    source_wallpaper_hash=wallpaper_hash,
                    input_catalog_hash=catalog_hash,
                    artifact_hashes=artifact_hashes,
                )
                return eeh
        except FileNotFoundError, RuntimeError, OSError:
            pass
        return None

    def _populate_icons(self, palette_entry_hash: str) -> str | None:
        """Populate icons cache via ITR. Returns entry hash or None on failure."""
        try:
            from runtime.adapters.cache import cache_entry_path, populate_via_staging
            from runtime.adapters.hashing import canonical_hash_dir, icons_entry_hash

            templates_dir = self._find_icon_templates()
            mappings_path = self._find_icon_mappings()
            if templates_dir is None or mappings_path is None:
                return None

            if templates_dir.is_dir():
                templates_hash = canonical_hash_dir(templates_dir)
            elif templates_dir.is_file():
                templates_hash = hash_file(templates_dir)
            else:
                return None

            if mappings_path.is_dir():
                mappings_hash_val = canonical_hash_dir(mappings_path)
            elif mappings_path.is_file():
                mappings_hash_val = hash_file(mappings_path)
            else:
                return None

            ieh = icons_entry_hash(palette_entry_hash, templates_hash, mappings_hash_val)
            target = cache_entry_path(self._state_root, "icons", ieh)

            def _populate(staging: Path) -> None:
                self._itr.render(palette_entry_hash, templates_dir, mappings_path, staging)

            created = populate_via_staging(target, _populate)

            if created or target.exists():
                artifact_hashes: dict[str, str] = {}
                for p in target.rglob("*.svg"):
                    if p.is_file():
                        artifact_hashes[p.name] = hash_file(p)
                self._seeder.write_icons_meta(
                    entry_hash=ieh,
                    source_palette_hash=palette_entry_hash,
                    input_templates_hash=templates_hash,
                    input_mappings_hash=mappings_hash_val,
                    artifact_hashes=artifact_hashes,
                )
                return ieh
        except FileNotFoundError, RuntimeError, OSError:
            pass
        return None

    def _reconstruct_palette(
        self, entry_hash: str | None, wallpaper_hash: str, now: str
    ) -> PaletteEntry | None:
        """Reconstruct PaletteEntry from cache or return None."""
        if entry_hash is None:
            return None
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=entry_hash,
            source_wallpaper_hash=wallpaper_hash,
            input_template_hash="0" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml="0" * 64,
                colors_conf="0" * 64,
                colors_gtk_css="0" * 64,
            ),
            generated_at=now,
        )

    def _reconstruct_effects(
        self, entry_hash: str | None, wallpaper_hash: str, now: str
    ) -> EffectsEntry | None:
        """Reconstruct EffectsEntry from cache or return None."""
        if entry_hash is None:
            return None
        from runtime.domain.models import EffectsEntry

        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=entry_hash,
            source_wallpaper_hash=wallpaper_hash,
            input_catalog_hash="0" * 64,
            artifact_hashes={},
            generated_at=now,
        )

    def _reconstruct_icons(
        self,
        entry_hash: str | None,
        palette_entry_hash: str | None,
        now: str,
    ) -> IconsEntry | None:
        """Reconstruct IconsEntry from cache or return None."""
        if entry_hash is None:
            return None
        from runtime.domain.models import IconsEntry

        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=entry_hash,
            source_palette_hash=palette_entry_hash or "0" * 64,
            input_templates_hash="0" * 64,
            input_mappings_hash="0" * 64,
            artifact_hashes={},
            generated_at=now,
        )

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
