"""Domain models for the derivation graph.

Pure, zero-I/O representations of wallpapers, palettes, effects, icons,
and per-monitor wallpaper configuration. No filesystem or network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, TypedDict


class BackendType(StrEnum):
    """Supported wallpaper backends."""

    hyprpaper = "hyprpaper"
    swaybg = "swaybg"
    swww = "swww"
    mpvpaper = "mpvpaper"


class FitMode(StrEnum):
    """Wallpaper fit modes."""

    cover = "cover"
    contain = "contain"
    fill = "fill"
    tile = "tile"
    center = "center"
    stretch = "stretch"


class WallpaperEntryHashes(TypedDict, total=False):
    """Artifact hashes for wallpaper entries (currently empty — wallpaper has no derived artifacts)."""


@dataclass(frozen=True, slots=True)
class WallpaperEntry:
    """A single wallpaper identified by its content hash."""

    hash_algorithm: Literal["sha256"]
    kind: Literal["wallpaper"]
    content_hash: str  # SHA-256 hex of the wallpaper file bytes
    source_path: str  # absolute path or empty string
    imported_at: str  # ISO-8601 UTC timestamp


class PaletteArtifacts(TypedDict):
    """Artifact hashes for palette entries."""

    colors_yaml: str  # key: "colors.yaml"
    colors_conf: str  # key: "colors.conf"
    colors_gtk_css: str  # key: "colors.gtk.css"
    colors_adw_css: str  # key: "colors.adw.css"
    colors_sequences: str  # key: "colors.sequences"
    colors_rasi: str  # key: "colors.rasi"


@dataclass(frozen=True, slots=True)
class PaletteEntry:
    """A color palette derived from a wallpaper via CSG templates."""

    hash_algorithm: Literal["sha256"]
    kind: Literal["palette"]
    entry_hash: str  # SHA-256 hex of (wallpaper_hash || template_set_hash)
    source_wallpaper_hash: str  # reference to WallpaperEntry.content_hash
    input_template_hash: str  # canonicalized hash of CSG templates dir
    artifact_hashes: PaletteArtifacts
    generated_at: str  # ISO-8601 UTC timestamp


@dataclass(frozen=True, slots=True)
class ConsumerPointer:
    """One declarative consumer-pointer table entry (investigation §3).

    Pure contract data (gt-4.2 ``shared-data-contract.md`` ConsumerPointer
    table): ``path`` is posix-relative to the install spine (e.g.
    ``"config/ags/colors.css"``); ``target`` is posix-relative to
    ``state_root/current/`` (e.g. ``"colors.gtk.css"``). Zero I/O.
    """

    path: str  # spine-relative posix path of the consumer pointer
    target: str  # current/-relative posix path of the target artifact


@dataclass(frozen=True, slots=True)
class ConsumerPointerRules:
    """Rules governing consumer-pointer repoint semantics (investigation §3).

    All flags default ``True`` (the pinned table has no per-pointer
    variation); the seeder loop reads them once and implements the
    semantics exactly once. Zero I/O.
    """

    remove_on_null_palette: bool = True
    replace_regular_file: bool = True
    skip_on_missing_target: bool = True
    skip_on_missing_parent: bool = True


class EffectsArtifacts(TypedDict, total=False):
    """Artifact hashes for effects entries.

    Keys are '<filename>.png' — variable set depending on the effects catalog.
    """


@dataclass(frozen=True, slots=True)
class EffectsEntry:
    """Visual effects derived from a wallpaper."""

    hash_algorithm: Literal["sha256"]
    kind: Literal["effects"]
    entry_hash: str  # SHA-256 hex of (wallpaper_hash || catalog_hash)
    source_wallpaper_hash: str  # reference to WallpaperEntry.content_hash
    input_catalog_hash: str  # canonicalized hash of effects catalog
    artifact_hashes: EffectsArtifacts
    generated_at: str  # ISO-8601 UTC timestamp


class IconsArtifacts(TypedDict, total=False):
    """Artifact hashes for icon entries.

    Keys are '<name>.svg' — variable set depending on the icon templates.
    """


@dataclass(frozen=True, slots=True)
class IconsEntry:
    """Icon set derived from a palette via icon templates."""

    hash_algorithm: Literal["sha256"]
    kind: Literal["icons"]
    entry_hash: str  # SHA-256 hex of (palette_hash || templates_hash || mappings_hash)
    source_palette_hash: str  # reference to PaletteEntry.entry_hash
    input_templates_hash: str  # canonicalized hash of icon templates dir
    input_mappings_hash: str  # canonicalized hash of icon mappings
    artifact_hashes: IconsArtifacts
    generated_at: str  # ISO-8601 UTC timestamp


@dataclass(frozen=True, slots=True)
class MonitorWallpaperConfig:
    """Per-monitor wallpaper backend selection and parameters."""

    backend: BackendType
    source_hash: str  # SHA-256 hex of the wallpaper content
    fit_mode: FitMode  # default: cover
    mpv_options: str | None  # mpv passthrough options string (only for mpvpaper)
    ipc_socket: str | None  # absolute path to mpv IPC socket (only for mpvpaper)

    def __post_init__(self) -> None:
        if self.backend != BackendType.mpvpaper:
            if self.mpv_options is not None or self.ipc_socket is not None:
                raise ValueError("mpv_options and ipc_socket are only valid for mpvpaper backend")


# Default monitor name for Phase 2 (full monitor detection is Epic 2).
# Single source of truth for the seeder's and applier's default-monitor
# convention so they cannot drift (AD-18).
DEFAULT_MONITOR = "DP-1"


@dataclass(frozen=True, slots=True)
class DesktopState:
    """Projection of current derivation outputs — maps to current.json."""

    schema_version: Literal[2]
    wallpaper: WallpaperEntry
    monitors: dict[str, MonitorWallpaperConfig]  # keyed by monitor name (e.g., "DP-1")
    palette: PaletteEntry | None  # None only when never derived
    effects: EffectsEntry | None  # None only when never derived
    icons: IconsEntry | None  # None only when never derived
    applied_at: str  # ISO-8601 UTC timestamp


class SeedLockedError(RuntimeError):
    """Raised when another process already holds the seed mutex (AD-11)."""


class CorruptCacheError(RuntimeError):
    """Raised when a cache entry's bytes do not match its recorded digests (AD-26).

    Raised at populate time by ``adapters/cache.py::verify_staging`` (before
    the staging rename — the corrupt entry never becomes visible) and consumed
    by ``DoctorUseCase`` repair (Story 2.2: quarantine + repopulate). Lives in
    the domain so ``application/`` can catch it without importing
    ``adapters/`` (inward dependencies only).
    """


@dataclass(frozen=True, slots=True)
class EntryHealth:
    """Read-side health verdict for one cache entry (Story 3.1, FR-5).

    Shared domain value type: ``adapters/cache.py::verify_entry`` produces it
    and ``application/verify_cache.py`` consumes it, so neither has to import
    the other (inward dependencies only).
    """

    status: Literal["ok", "corrupt", "missing"]
    detail: str
    annotated: bool = False


@dataclass(frozen=True, slots=True)
class CacheEntryRef:
    """One cache entry + its recency timestamp (Story 3.2 prune).

    Shared domain value type: ``adapters/prune_source.py`` produces it and
    ``application/prune.py`` consumes it. ``timestamp`` is the entry's
    ``generated_at``/``imported_at`` (ISO-8601), or ``None`` when the meta is
    missing/corrupt/undated — an undated entry is PROTECTED by prune (never
    delete what cannot be classified).
    """

    entry_hash: str
    timestamp: str | None


@dataclass(frozen=True, slots=True)
class DesiredState:
    """Declared intent for Phase 4 declarative convergence (Story 4.2).

    Pure value type: the wallpaper the desktop should show, the keep policy
    the planner must obey, and explicitly pinned entry hashes. Carries no
    file-format version (validated at load, not carried) and performs no I/O.
    Nothing consumes this yet — the diff engine (Epic 3) will.
    """

    wallpaper: str
    keep: int
    pinned: tuple[str, ...]
