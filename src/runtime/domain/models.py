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
