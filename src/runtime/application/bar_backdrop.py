"""Derive the active wallpaper appearance used by the bar's live indicators."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

from runtime.adapters.icon_contrast_sampler import sample_top_strip_luminance
from runtime.domain.models import MonitorWallpaperConfig


def classify_bar_backdrop(luminance: float | None) -> Literal["light", "dark"] | None:
    """Classify average WCAG relative luminance; missing/invalid samples are unknown."""
    if luminance is None or not 0.0 <= luminance <= 1.0:
        return None
    return "light" if luminance >= 0.5 else "dark"


def with_bar_backdrops(
    monitors: dict[str, MonitorWallpaperConfig],
    state_root: Path,
    *,
    source_paths: dict[str, Path] | None = None,
) -> dict[str, MonitorWallpaperConfig]:
    """Return monitor configs with appearance sampled from their wallpaper bytes.

    ``source_paths`` allows an apply/seed caller to provide the just-used source
    directly. Other source hashes resolve through the runtime wallpaper cache.
    Sampling failures are represented as ``None`` and never fail wallpaper set.
    """
    direct_paths = source_paths or {}
    result: dict[str, MonitorWallpaperConfig] = {}
    for name, config in monitors.items():
        path = direct_paths.get(config.source_hash)
        if path is None:
            path = state_root / "cache" / "wallpapers" / config.source_hash / "wallpaper.png"
        appearance = classify_bar_backdrop(sample_top_strip_luminance(path))
        result[name] = replace(config, bar_backdrop=appearance)
    return result
