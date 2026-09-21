"""Unit tests for the icon-contrast guard domain service."""

from __future__ import annotations

import pytest

from runtime.domain.icon_contrast import (
    BAR_GROUPS,
    DEFAULT_THRESHOLD,
    PLACEHOLDERS,
    contrast_ratio,
    decide_overrides,
    hex_to_rgb,
    pick_best_token,
    relative_luminance,
)

DARK_BACKDROP = "#000000"
LIGHT_BACKDROP = "#ffffff"

PALETTE: dict[str, str] = {
    "background": "#101418",
    "foreground": "#e8eaed",
    "cursor": "#e8eaed",
    "color0": "#000000",
    "color1": "#ff5555",
    "color2": "#50fa7b",
    "color3": "#f1fa8c",
    "color4": "#bd93f9",
    "color5": "#ff79c6",
    "color6": "#8be9fd",
    "color7": "#bbbbbb",
    "color8": "#44475a",
    "color9": "#ff5555",
    "color10": "#50fa7b",
    "color11": "#f1fa8c",
    "color12": "#bd93f9",
    "color13": "#ff79c6",
    "color14": "#8be9fd",
    "color15": "#ffffff",
    # NOTE: no `surface`/`accent` keys — the guard must tolerate their absence.
}


def test_hex_to_rgb_vectors() -> None:
    assert hex_to_rgb("#000000") == (0, 0, 0)
    assert hex_to_rgb("#ffffff") == (255, 255, 255)
    assert hex_to_rgb("ff0000") == (255, 0, 0)
    assert hex_to_rgb("#abc") == (170, 187, 204)
    with pytest.raises(ValueError):
        hex_to_rgb("#zzzzzz")
    with pytest.raises(ValueError):
        hex_to_rgb("#12345")


def test_wcag_black_white_vector() -> None:
    assert relative_luminance((0, 0, 0)) == pytest.approx(0.0)
    assert relative_luminance((255, 255, 255)) == pytest.approx(1.0)
    assert relative_luminance((255, 0, 0)) == pytest.approx(0.2126)
    black_white = contrast_ratio(relative_luminance((0, 0, 0)), relative_luminance((255, 255, 255)))
    assert black_white == pytest.approx(21.0)
    assert contrast_ratio(0.5, 0.5) == pytest.approx(1.0)


def test_constants() -> None:
    assert DEFAULT_THRESHOLD == 4.5
    assert PLACEHOLDERS == ("COLOR_FOREGROUND", "COLOR_JOIN")
    assert set(BAR_GROUPS) == {
        "battery",
        "network",
        "btop",
        "thunderbird",
        "tray",
        "ui",
        "power-menu",
        "email-client",
        "wallpaper-selector",
        "settings",
        "volume",
        "microphone",
    }


def test_light_wallpaper_flips_bright_icon() -> None:
    token, before, after = pick_best_token(LIGHT_BACKDROP, "color15", PALETTE)
    assert before == pytest.approx(1.0)
    assert before < DEFAULT_THRESHOLD
    assert token == "color0"
    assert after == pytest.approx(21.0)
    assert after >= DEFAULT_THRESHOLD


def test_dark_wallpaper_noop() -> None:
    token, before, after = pick_best_token(DARK_BACKDROP, "color15", PALETTE)
    assert before == pytest.approx(21.0)
    assert token == "color15"
    assert after == before
    groups = {"battery": {"COLOR_FOREGROUND": "color15", "COLOR_JOIN": "color15"}}
    assert decide_overrides(PALETTE, DARK_BACKDROP, groups) == {}


def test_decide_overrides_light_flip_scope() -> None:
    groups = {
        "battery": {"COLOR_FOREGROUND": "color15", "COLOR_JOIN": "color15"},
        "network": {"COLOR_FOREGROUND": "color15"},
        "capture-tool": {"COLOR_FOREGROUND": "color15"},  # non-bar: never rewritten
        "battery2": {"COLOR_FOREGROUND": "color15"},  # not allowlisted: never rewritten
    }
    overrides = decide_overrides(PALETTE, LIGHT_BACKDROP, groups)
    assert overrides == {
        ("battery", "COLOR_FOREGROUND"): "color0",
        ("battery", "COLOR_JOIN"): "color0",
        ("network", "COLOR_FOREGROUND"): "color0",
    }


def test_literal_passthrough() -> None:
    token, before, after = pick_best_token(LIGHT_BACKDROP, "#123456", PALETTE)
    assert token == "#123456"
    assert after == before
    groups = {"battery": {"COLOR_FOREGROUND": "#ffffff"}}
    assert decide_overrides(PALETTE, LIGHT_BACKDROP, groups) == {}


def test_missing_surface_tolerance() -> None:
    assert "surface" not in PALETTE
    # Dangling token reference: tolerated (left unchanged), never emitted.
    groups = {"battery": {"COLOR_FOREGROUND": "surface"}}
    assert decide_overrides(PALETTE, LIGHT_BACKDROP, groups) == {}
    # Normal evaluation still works and never emits an absent key.
    groups = {"battery": {"COLOR_FOREGROUND": "color15"}}
    overrides = decide_overrides(PALETTE, LIGHT_BACKDROP, groups)
    assert overrides[("battery", "COLOR_FOREGROUND")] in PALETTE


def test_tie_keeps_original() -> None:
    tied = dict(PALETTE)
    tied["color7"] = "#ffffff"  # exact tie with color15 on a dark backdrop
    token, _, _ = pick_best_token(DARK_BACKDROP, "color15", tied)
    assert token == "color15"


def test_threshold_boundary() -> None:
    groups = {"battery": {"COLOR_FOREGROUND": "color15"}}
    _, ratio_before, _ = pick_best_token(LIGHT_BACKDROP, "color15", PALETTE)
    # Exactly at threshold: no retarget (strict `<` triggers).
    assert decide_overrides(PALETTE, LIGHT_BACKDROP, groups, threshold=ratio_before) == {}
    # A hair above: retarget fires.
    overrides = decide_overrides(PALETTE, LIGHT_BACKDROP, groups, threshold=ratio_before + 1e-9)
    assert overrides == {("battery", "COLOR_FOREGROUND"): "color0"}
