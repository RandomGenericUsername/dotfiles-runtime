"""Wallpaper top-strip luminance sampler (adapter).

I/O boundary for the icon-contrast guard: reads wallpaper pixels via a
use-site PIL import and returns the average WCAG relative luminance of
the top ``band_px`` rows (the bar backdrop signal). Returns ``None`` on
ANY failure — missing Pillow, missing/corrupt/unsupported file, empty
image — and never raises, so the caller falls back to the palette
backdrop.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from runtime.domain.icon_contrast import relative_luminance


def sample_top_strip_luminance(wallpaper: Path, band_px: int = 48) -> float | None:
    """Average relative luminance of the wallpaper's top ``band_px`` rows."""
    try:
        if band_px < 1:
            return None
        pil_image: Any = importlib.import_module("PIL.Image")
        with pil_image.open(wallpaper) as img:
            rgb = img.convert("RGB")
            width, height = rgb.size
            band = rgb.crop((0, 0, width, min(band_px, height)))
            band_width, band_height = band.size
            thumb = band.resize((max(1, min(band_width, 64)), max(1, min(band_height, 64))))
            pixels = list(thumb.getdata())
        if not pixels:
            return None
        total = 0.0
        for pixel in pixels:
            total += relative_luminance((int(pixel[0]), int(pixel[1]), int(pixel[2])))
        return total / len(pixels)
    except Exception:
        return None
