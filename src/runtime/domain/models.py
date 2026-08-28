"""Domain models for the derivation graph.

Pure, zero-I/O representations of wallpapers, palettes, effects, icons,
and per-monitor wallpaper configuration. No filesystem or network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Self


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


@dataclass(frozen=True, slots=True)
class WallpaperEntry:
    """A single wallpaper identified by its content hash."""

    hash_algorithm: str  # always "sha256"
    kind: str  # always "wallpaper"
    content_hash: str  # SHA-256 hex of the wallpaper file bytes
    source_path: str  # absolute path or empty string
    imported_at: str  # ISO-8601 UTC timestamp


@dataclass(frozen=True, slots=True)
class PaletteEntry:
    """A color palette derived from a wallpaper via CSG templates."""

    hash_algorithm: str  # always "sha256"
    kind: str  # always "palette"
    entry_hash: str  # SHA-256 hex of (wallpaper_hash || template_set_hash)
    source_wallpaper_hash: str  # reference to WallpaperEntry.content_hash
    input_template_hash: str  # canonicalized hash of CSG templates dir
    artifact_hashes: dict[str, str]  # keys: colors.yaml, colors.conf, colors.gtk.css -> SHA-256
    generated_at: str  # ISO-8601 UTC timestamp


@dataclass(frozen=True, slots=True)
class EffectsEntry:
    """Visual effects derived from a wallpaper."""

    hash_algorithm: str  # always "sha256"
    kind: str  # always "effects"
    entry_hash: str  # SHA-256 hex of (wallpaper_hash || catalog_hash)
    source_wallpaper_hash: str  # reference to WallpaperEntry.content_hash
    input_catalog_hash: str  # canonicalized hash of effects catalog
    artifact_hashes: dict[str, str]  # keys: <filename>.png -> SHA-256
    generated_at: str  # ISO-8601 UTC timestamp


@dataclass(frozen=True, slots=True)
class IconsEntry:
    """Icon set derived from a palette via icon templates."""

    hash_algorithm: str  # always "sha256"
    kind: str  # always "icons"
    entry_hash: str  # SHA-256 hex of (palette_hash || templates_hash || mappings_hash)
    source_palette_hash: str  # reference to PaletteEntry.entry_hash
    input_templates_hash: str  # canonicalized hash of icon templates dir
    input_mappings_hash: str  # canonicalized hash of icon mappings
    artifact_hashes: dict[str, str]  # keys: <name>.svg -> SHA-256
    generated_at: str  # ISO-8601 UTC timestamp


@dataclass(frozen=True, slots=True)
class MonitorWallpaperConfig:
    """Per-monitor wallpaper backend selection and parameters."""

    backend: BackendType
    source_hash: str  # SHA-256 hex of the wallpaper content
    fit_mode: FitMode  # default: cover
    mpv_options: str | None  # mpv passthrough options string (only for mpvpaper)
    ipc_socket: str | None  # absolute path to mpv IPC socket (only for mpvpaper)


@dataclass(frozen=True, slots=True)
class DesktopState:
    """Projection of current derivation outputs — maps to current.json."""

    schema_version: int  # always 2
    wallpaper: WallpaperEntry
    monitors: dict[str, MonitorWallpaperConfig]  # keyed by monitor name (e.g., "DP-1")
    palette: PaletteEntry | None  # None only when never derived
    effects: EffectsEntry | None  # None only when never derived
    icons: IconsEntry | None  # None only when never derived
    applied_at: str  # ISO-8601 UTC timestamp
