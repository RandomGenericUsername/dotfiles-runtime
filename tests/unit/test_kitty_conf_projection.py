"""Kitty ``conf`` projection: bare points, unitless opacity, first family.

The assets ``skins/templates/kitty.conf.j2`` emits no unit suffix. The runtime
``conf`` renderer owns the docs/113 §7.3 px→pt rule and §7.4 family rule; this
module pins the exact terminal-host/terminal/paper/glass output, the clamps, the
no-``px``/``pt`` invariant, byte-identical CSS/rasi output and a live
``kitty +runpy`` parse (explicitly skipped when kitty is absent).
"""

from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path

import pytest

from runtime.adapters.skin_expander import (
    expand_v2_template,
    load_skin_manifest,
    resolve_v2_token_document,
)

_MANIFESTS = Path(__file__).resolve().parents[1] / "fixtures" / "skins-v2" / "manifests"
_KITTY_TEMPLATE = (
    "font_family {{SKIN_TYPE_FONT_FAMILY}}\n"
    "font_size {{SKIN_TYPE_SIZE_BODY}}\n"
    "window_padding_width {{SKIN_SURFACE_WINDOW_PADDING}}\n"
    "background_opacity {{SKIN_SURFACE_WINDOW_OPACITY}}\n"
)


def _tokens(name: str, mode: str = "dark") -> dict[str, object]:
    manifest = load_skin_manifest(_MANIFESTS / f"{name}.toml")
    return resolve_v2_token_document(manifest, mode)


def _render(tokens: dict[str, object], output_format: str = "conf") -> str:
    return expand_v2_template(
        _KITTY_TEMPLATE, tokens, format=output_format  # type: ignore[arg-type]
    )


def _values(rendered: str) -> dict[str, str]:
    return dict(line.split(" ", 1) for line in rendered.splitlines())


@pytest.mark.parametrize(
    ("name", "mode", "family", "font_size", "padding", "opacity"),
    [
        ("terminal-host", "dark", "JetBrainsMono Nerd Font", "9", "6.75", "1"),
        ("terminal", "dark", "JetBrainsMono Nerd Font", "9", "6.75", "1"),
        ("terminal", "light", "JetBrainsMono Nerd Font", "9", "6.75", "1"),
        ("paper", "dark", "Cantarell", "9.75", "8.25", "1"),
        ("paper", "light", "Cantarell", "9.75", "8.25", "1"),
        ("glass", "dark", "monospace", "9.75", "8.25", "0.94"),
        ("flat", "dark", "Cantarell", "9.75", "8.25", "1"),
    ],
)
def test_kitty_conf_exact_projection(
    name: str,
    mode: str,
    family: str,
    font_size: str,
    padding: str,
    opacity: str,
) -> None:
    rendered = _render(_tokens(name, mode))

    assert _values(rendered) == {
        "font_family": family,
        "font_size": font_size,
        "window_padding_width": padding,
        "background_opacity": opacity,
    }
    # Kitty rejects both suffixes; a bare decimal is the only accepted form.
    assert "px" not in rendered
    assert "pt" not in rendered
    # Paint-free: no palette include, literal colour or comment leak.
    assert "include " not in rendered
    assert "#" not in rendered


def test_conf_length_clamps_and_half_up_rounding() -> None:
    base = _tokens("terminal")
    for raw, expected in (
        (4, "6"),
        (8, "6"),
        (12, "9"),
        (12.5, "9.38"),
        (13, "9.75"),
        (48, "36"),
        (100, "36"),
    ):
        tokens = copy.deepcopy(base)
        tokens["type"]["size-body"] = raw  # type: ignore[index]
        rendered = _render(tokens)
        assert _values(rendered)["font_size"] == expected

    for raw, expected in (
        (0, "0"),
        (1, "0.75"),
        (9, "6.75"),
        (11, "8.25"),
        (43, "32"),
        (100, "32"),
    ):
        tokens = copy.deepcopy(base)
        tokens["surface"]["window"]["padding"] = raw  # type: ignore[index]
        rendered = _render(tokens)
        assert _values(rendered)["window_padding_width"] == expected


def test_conf_opacity_is_unitless_and_not_derived_from_paint() -> None:
    base = _tokens("glass")
    for raw, expected in ((0, "0"), (0.1, "0.1"), (0.94, "0.94"), (1.0, "1")):
        tokens = copy.deepcopy(base)
        tokens["surface"]["window"]["opacity"] = raw  # type: ignore[index]
        rendered = _render(tokens)
        opacity = _values(rendered)["background_opacity"]
        assert opacity == expected
        assert opacity == opacity.rstrip("pxpt")

    # Changing the window paint never changes the opacity value.
    tokens = copy.deepcopy(base)
    tokens["surface"]["window"]["bg"] = "transparent"  # type: ignore[index]
    assert _values(_render(tokens))["background_opacity"] == "0.94"


def test_css_and_rasi_output_are_byte_identical() -> None:
    tokens = _tokens("terminal")
    template = (
        "font-family: {{SKIN_TYPE_FONT_FAMILY}};\n"
        "font-size: {{SKIN_TYPE_SIZE_BODY}};\n"
        "padding: {{SKIN_SURFACE_WINDOW_PADDING}};\n"
        "opacity: {{SKIN_SURFACE_WINDOW_OPACITY}};\n"
    )

    css = expand_v2_template(template, tokens, format="css")
    assert "font-family: JetBrainsMono Nerd Font, monospace;" in css
    assert "font-size: 12px;" in css
    assert "padding: 9px;" in css
    assert "opacity: 1;" in css

    rasi = expand_v2_template(template, tokens, format="rasi")
    assert "font-size: 12px;" in rasi
    assert "padding: 9px;" in rasi
    assert "opacity: 1;" in rasi


def test_conf_paint_still_fails_closed() -> None:
    tokens = _tokens("terminal")
    with pytest.raises(ValueError, match="cannot be projected into conf"):
        expand_v2_template(
            "background {{SKIN_SURFACE_WINDOW_BG}}\n", tokens, format="conf"
        )


def test_generated_kitty_conf_has_no_bad_lines(tmp_path: Path) -> None:
    kitty = shutil.which("kitty")
    if kitty is None:
        pytest.skip("kitty is not installed; the +runpy parse gate needs a live kitty")

    artifact = tmp_path / "kitty.conf"
    artifact.write_text(_render(_tokens("terminal-host")), encoding="utf-8")
    code = (
        "from kitty.config import load_config\n"
        "bad = []\n"
        f"load_config({str(artifact)!r}, accumulate_bad_lines=bad)\n"
        "print(bad)\n"
        "assert bad == []\n"
    )
    result = subprocess.run(
        [kitty, "+runpy", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert "BadLine" not in result.stdout
