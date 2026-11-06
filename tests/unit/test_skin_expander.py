"""Skin manifest v1 parser + deterministic expansion (FROZEN SKIN SPEC v1).

The manifest fixture is byte-identical to the real
``dotfiles-assets/skins/manifests/glass.toml``; the template fixture mirrors
the canonical ``<configDir>.gtk.css.j2`` name and placeholder set.
"""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from runtime.adapters.hashing import canonical_hash_dir, hash_file, skin_entry_hash
from runtime.adapters.skin_expander import SkinExpander

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "skins"
_MANIFEST = _FIXTURES / "manifests" / "glass.toml"
_TEMPLATES = _FIXTURES / "templates"
_TEMPLATE = _TEMPLATES / "ags.gtk.css.j2"


def _entry_dir(manifest: Path, templates: Path, tmp_path: Path) -> Path:
    expected = skin_entry_hash(canonical_hash_dir(templates), hash_file(manifest))
    return tmp_path / "cache" / "skins" / expected


def _copy_templates(tmp_path: Path, *, template: str | None = None) -> Path:
    templates = tmp_path / "templates"
    templates.mkdir()
    shutil.copy2(_TEMPLATE, templates / "ags.gtk.css.j2")
    if template is not None:
        (templates / "ags.gtk.css.j2").write_text(template, encoding="utf-8")
    return templates


def _copy_manifest(tmp_path: Path, text: str | None = None, *, suffix: str = ".toml") -> Path:
    manifest = tmp_path / f"glass{suffix}"
    if text is None:
        shutil.copy2(_MANIFEST, manifest)
    else:
        manifest.write_text(text, encoding="utf-8")
    return manifest


def test_real_glass_fixture_expands_to_the_canonical_css_and_token_map(
    tmp_path: Path,
) -> None:
    work = _entry_dir(_MANIFEST, _TEMPLATES, tmp_path)

    entry = SkinExpander().expand("glass", _MANIFEST, _TEMPLATES, work)

    assert entry.name == "glass"
    assert entry.entry_hash == work.name
    assert entry.input_templates_hash == canonical_hash_dir(_TEMPLATES)
    assert entry.input_manifest_hash == hash_file(_MANIFEST)
    assert set(entry.artifact_hashes) == {"ags.css", "skin.json"}
    assert entry.artifact_hashes["ags.css"] == hash_file(work / "ags.css")

    css = (work / "ags.css").read_text(encoding="utf-8")
    assert "border-radius: 12px;" in css
    assert "border: 1.5px solid @primary;" in css
    assert "padding: 8px 9px;" in css
    assert "min-width: 312px;" in css
    assert "font-size: 12.5px;" in css
    assert "font-size: 13px;" in css
    assert "margin-bottom: 4px;" in css
    assert "min-height: 0px;" in css

    skin = json.loads((work / "skin.json").read_text(encoding="utf-8"))
    assert list(skin) == [
        "version",
        "shape",
        "surface",
        "ink",
        "type",
        "rhythm",
        "layout",
    ]
    assert skin["version"] == 1
    assert skin["shape"] == {
        "radius-panel": 12,
        "radius-card": 9,
        "radius-cell": 7,
        "radius-pill": 20,
        "border-w": 1.5,
    }
    assert skin["rhythm"] == {"gap": 8, "cell-pad": "8px 9px"}
    assert skin["surface"] == {
        "bg-panel": "alpha(@surface, 0.94)",
        "bg-card": "alpha(@surface_container_high, 0.18)",
        "border": "@primary",
        "shadow": "none",
        "blur": "none",
    }
    assert skin["ink"]["ink-dim"] == "alpha(@on_surface, 0.62)"
    assert skin["type"] == {
        "font-family": "inherit",
        "title-size": 12.5,
        "text-size": 13,
    }
    assert skin["layout"] == {
        "panel-width": 312,
        "bt-list-spacing": 4,
        "bar-row-height": 0,
        "chip-min": 0,
    }
    assert "tokens" not in skin and "apps" not in skin


def test_json_manifest_is_equivalent(tmp_path: Path) -> None:
    data = tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))
    manifest = _copy_manifest(tmp_path, json.dumps(data), suffix=".json")
    work = _entry_dir(manifest, _TEMPLATES, tmp_path)

    entry = SkinExpander().expand("glass", manifest, _TEMPLATES, work)

    assert entry.input_manifest_hash == hash_file(manifest)
    css = (work / "ags.css").read_text(encoding="utf-8")
    assert "border-radius: 12px;" in css
    assert "padding: 8px 9px;" in css


def test_every_gtk_css_j2_template_is_rendered_to_its_config_dir(tmp_path: Path) -> None:
    templates = _copy_templates(tmp_path)
    (templates / "ags-notifications.gtk.css.j2").write_text(
        ".notice { color: {{SKIN_ACCENT}}; }\n", encoding="utf-8"
    )
    work = _entry_dir(_MANIFEST, templates, tmp_path)

    entry = SkinExpander().expand("glass", _MANIFEST, templates, work)

    assert set(entry.artifact_hashes) == {
        "ags.css",
        "ags-notifications.css",
        "skin.json",
    }
    assert (work / "ags-notifications.css").read_text(encoding="utf-8") == (
        ".notice { color: @primary; }\n"
    )


def test_unknown_placeholder_fails_closed(tmp_path: Path) -> None:
    templates = _copy_templates(tmp_path, template="{{SKIN_NOPE}}\n")
    work = _entry_dir(_MANIFEST, templates, tmp_path)

    with pytest.raises(ValueError, match="unknown placeholder"):
        SkinExpander().expand("glass", _MANIFEST, templates, work)
    assert not work.exists()


def test_non_skin_placeholder_fails_closed(tmp_path: Path) -> None:
    templates = _copy_templates(tmp_path, template="{{NAME}}\n")
    work = _entry_dir(_MANIFEST, templates, tmp_path)

    with pytest.raises(ValueError, match="unknown placeholder"):
        SkinExpander().expand("glass", _MANIFEST, templates, work)


def test_unresolved_placeholder_fails_closed(tmp_path: Path) -> None:
    templates = _copy_templates(tmp_path, template="{{SKIN_RADIUS_PANEL\n")
    work = _entry_dir(_MANIFEST, templates, tmp_path)

    with pytest.raises(ValueError, match="unresolved placeholder"):
        SkinExpander().expand("glass", _MANIFEST, templates, work)


def test_missing_template_set_fails_closed(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "ags.css.tpl").write_text("x", encoding="utf-8")
    work = _entry_dir(_MANIFEST, templates, tmp_path)

    with pytest.raises(ValueError, match=r"no '\*\.gtk\.css\.j2' files"):
        SkinExpander().expand("glass", _MANIFEST, templates, work)


def test_manifest_name_must_match_selection(tmp_path: Path) -> None:
    text = _MANIFEST.read_text(encoding="utf-8").replace('name = "glass"', 'name = "flat"')
    manifest = _copy_manifest(tmp_path, text)
    work = _entry_dir(manifest, _TEMPLATES, tmp_path)

    with pytest.raises(ValueError, match="does not match selected skin"):
        SkinExpander().expand("glass", manifest, _TEMPLATES, work)


def _invalid_manifest_variants() -> list[tuple[str, str]]:
    text = _MANIFEST.read_text(encoding="utf-8")
    return [
        ("schema-major", text.replace("schema_major = 1", "schema_major = 2")),
        ("schema-major-missing", text.replace("schema_major = 1\n", "")),
        ("tokens-table", text + '\n[tokens]\nradius = "12px"\n'),
        ("apps-table", text + '\n[apps]\nags = "ags.gtk.css.j2"\n'),
        ("structure-group", text + '\n[structure]\nfoo = "bar"\n'),
        ("missing-group", text.replace("[layout]", "[layout-extra]")),
        ("unknown-token", text.replace("[shape]\n", "[shape]\nunknown = 1\n")),
        ("missing-token", text.replace("radius-panel = 12\n", "")),
        ("dimension-string", text.replace("border-w = 1.5", 'border-w = "1.5px"')),
        ("dimension-bool", text.replace("radius-panel = 12", "radius-panel = true")),
        ("string-token-number", text.replace('font-family = "inherit"', "font-family = 12")),
    ]


@pytest.mark.parametrize(
    ("case", "payload"),
    _invalid_manifest_variants(),
    ids=[case for case, _ in _invalid_manifest_variants()],
)
def test_invalid_manifest_shape_fails_loud(
    tmp_path: Path, case: str, payload: str
) -> None:
    manifest = _copy_manifest(tmp_path, payload)

    with pytest.raises(ValueError):
        SkinExpander().expand("glass", manifest, _TEMPLATES, tmp_path / "out")
    assert not (tmp_path / "out").exists()
