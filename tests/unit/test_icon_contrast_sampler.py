"""Unit tests for the icon-contrast sampler adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.adapters.icon_contrast_sampler import sample_top_strip_luminance


def test_sampler_none_on_missing() -> None:
    assert sample_top_strip_luminance(Path("/nonexistent/wallpaper.png")) is None


def test_sampler_none_on_corrupt(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"this is not an image")
    assert sample_top_strip_luminance(corrupt) is None
    assert sample_top_strip_luminance(corrupt, band_px=0) is None


def test_sampler_solid_colors(tmp_path: Path) -> None:
    pytest.importorskip("PIL.Image")
    from PIL import Image

    white = tmp_path / "white.png"
    black = tmp_path / "black.png"
    Image.new("RGB", (64, 64), (255, 255, 255)).save(white)
    Image.new("RGB", (64, 64), (0, 0, 0)).save(black)
    assert sample_top_strip_luminance(white) == pytest.approx(1.0)
    assert sample_top_strip_luminance(black) == pytest.approx(0.0)
