"""WCAG 2.1 icon-contrast guard (pure domain service).

Evaluates the contrast of bar-icon foreground colors against the bar
backdrop and selects the highest-contrast palette-resident replacement
token. Pure: no I/O — wallpaper sampling lives in
``runtime.adapters.icon_contrast_sampler`` and the caller falls back to
the palette backdrop when it yields ``None``.

Candidate set = palette keys ∩ ``{foreground, background, cursor,
color0..color15}`` — never a dangling token. Literals starting with
``#`` pass through untouched. Ties keep the original token. Selection
is best-effort: the max-ratio token is picked even when it still misses
the threshold.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

#: Bar groups whose group-level placeholders may be retargeted (D5).
BAR_GROUPS: Final[tuple[str, ...]] = (
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
    # Audio bar indicators (add-pipewire-audio-control): the level-aware
    # output glyph and the microphone indicator float over the wallpaper like
    # every other bar icon, so they need the same light-wallpaper retarget.
    "volume",
    "microphone",
)

#: Placeholders eligible for retargeting (D5). Literals (``#…``),
#: variant-level overrides, and ``bar_mappings`` are never rewritten.
PLACEHOLDERS: Final[tuple[str, ...]] = ("COLOR_FOREGROUND", "COLOR_JOIN")

#: Default minimum WCAG contrast ratio for bar icons.
DEFAULT_THRESHOLD: Final[float] = 4.5

#: Canonical candidate-token order. Intersected with the loaded palette,
#: so absent keys (e.g. a ``surface`` the palette did not emit) can never
#: be selected.
CANDIDATE_TOKENS: Final[tuple[str, ...]] = (
    "foreground",
    "background",
    "cursor",
    *(f"color{i}" for i in range(16)),
)


def hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Parse ``#rrggbb`` (``#`` optional, ``#rgb`` shorthand allowed).

    Raises ``ValueError`` on malformed input.
    """
    text = hex_color.strip()
    if text.startswith("#"):
        text = text[1:]
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"invalid hex color: {hex_color!r}")
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except ValueError as exc:
        raise ValueError(f"invalid hex color: {hex_color!r}") from exc
    return (red, green, blue)


def _linearize(channel: int) -> float:
    """Linearize one sRGB channel (WCAG 2.1 § relative luminance)."""
    scaled = channel / 255.0
    if scaled <= 0.03928:
        return scaled / 12.92
    return float(((scaled + 0.055) / 1.055) ** 2.4)


def relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.1 relative luminance of an sRGB color in ``[0.0, 1.0]``."""
    red, green, blue = rgb
    return 0.2126 * _linearize(red) + 0.7152 * _linearize(green) + 0.0722 * _linearize(blue)


def contrast_ratio(lum_a: float, lum_b: float) -> float:
    """WCAG 2.1 contrast ratio ``(L1 + 0.05) / (L2 + 0.05)`` in ``[1.0, 21.0]``."""
    lighter = max(lum_a, lum_b)
    darker = min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance_of_hex(hex_color: str) -> float:
    """Relative luminance of a ``#rrggbb`` color string."""
    return relative_luminance(hex_to_rgb(hex_color))


def pick_best_token(
    backdrop_hex: str,
    current_token: str,
    palette: Mapping[str, str],
) -> tuple[str, float, float]:
    """Pick the highest-contrast palette token against ``backdrop_hex``.

    Returns ``(token, ratio_before, ratio_after)``. Literals starting
    with ``#`` pass through (returned unchanged, ``ratio_before ==
    ratio_after``). Ties keep the original token. Best effort: the
    max-ratio token wins even when still under threshold.

    Raises ``ValueError`` when ``backdrop_hex`` is malformed, when
    ``current_token`` is neither a hex literal nor a palette-resident
    key, or when the current token's hex is malformed. Candidates whose
    own hex is malformed are skipped (tolerance), never selected.
    """
    backdrop_lum = _luminance_of_hex(backdrop_hex)
    if current_token.startswith("#"):
        ratio = contrast_ratio(_luminance_of_hex(current_token), backdrop_lum)
        return (current_token, ratio, ratio)
    if current_token not in palette:
        raise ValueError(f"token {current_token!r} not present in palette and is not a hex literal")
    ratio_before = contrast_ratio(_luminance_of_hex(palette[current_token]), backdrop_lum)
    # Current first + strict `>` ratchets ties toward the original token.
    ordered = [current_token, *(t for t in CANDIDATE_TOKENS if t != current_token and t in palette)]
    best_token = current_token
    best_ratio = ratio_before
    for token in ordered[1:]:
        try:
            candidate_lum = _luminance_of_hex(palette[token])
        except ValueError:
            continue
        ratio = contrast_ratio(candidate_lum, backdrop_lum)
        if ratio > best_ratio:
            best_token = token
            best_ratio = ratio
    return (best_token, ratio_before, best_ratio)


def decide_overrides(
    palette: Mapping[str, str],
    backdrop_hex: str,
    groups_config: Mapping[str, Mapping[str, str]],
    threshold: float = DEFAULT_THRESHOLD,
) -> dict[tuple[str, str], str]:
    """Decide ``{(group, placeholder): new_token}`` retargets.

    Only group-level ``PLACEHOLDERS`` of ``BAR_GROUPS`` are considered;
    literals, non-bar groups, unknown placeholders, and unresolvable
    entries are left unchanged (tolerance). An override is emitted only
    when the current ratio is strictly below ``threshold`` and the best
    token differs from the current one.
    """
    overrides: dict[tuple[str, str], str] = {}
    for group, placeholders in groups_config.items():
        if group not in BAR_GROUPS:
            continue
        for placeholder, current in placeholders.items():
            if placeholder not in PLACEHOLDERS:
                continue
            if current.startswith("#"):
                continue
            try:
                best, ratio_before, _ = pick_best_token(backdrop_hex, current, palette)
            except ValueError:
                continue
            if ratio_before < threshold and best != current:
                overrides[(group, placeholder)] = best
    return overrides
