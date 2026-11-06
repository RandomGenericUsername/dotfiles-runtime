"""Skin manifest v1 parser + deterministic expansion (FROZEN SKIN SPEC v1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.hashing import canonical_hash_dir, hash_file, skin_entry_hash
from runtime.adapters.skin_expander import SkinExpander

_AGS = "/* {{SKIN_NAME}} */\n.panel { border-radius: {{SKIN_RADIUS}}; opacity: {{SKIN_SURFACE_ALPHA}}; }\n"
_GTK = "window { border-radius: {{SKIN_RADIUS}}; }\n"


def _templates(tmp_path: Path) -> Path:
    templates = tmp_path / "skin-templates"
    templates.mkdir()
    (templates / "ags.css.tpl").write_text(_AGS, encoding="utf-8")
    (templates / "gtk.css.tpl").write_text(_GTK, encoding="utf-8")
    return templates


def _manifest(tmp_path: Path, *, name: str = "glass", fmt: str = "toml") -> Path:
    if fmt == "toml":
        text = (
            f'name = "{name}"\n'
            "version = 1\n"
            "[tokens]\n"
            'name = "glass"\n'
            'radius = "12px"\n'
            'surface_alpha = "0.82"\n'
            "[apps]\n"
            'ags = "ags.css.tpl"\n'
            'gtk = "gtk.css.tpl"\n'
        )
        path = tmp_path / f"{name}.toml"
    else:
        payload = {
            "name": name,
            "version": 1,
            "tokens": {"name": "glass", "radius": "12px", "surface_alpha": "0.82"},
            "apps": {"ags": "ags.css.tpl", "gtk": "gtk.css.tpl"},
        }
        path = tmp_path / f"{name}.json"
        text = json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    return path


def _entry_dir(manifest: Path, templates: Path, tmp_path: Path) -> Path:
    expected = skin_entry_hash(canonical_hash_dir(templates), hash_file(manifest))
    return tmp_path / "cache" / "skins" / expected


def test_toml_manifest_expands_deterministically(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    manifest = _manifest(tmp_path)
    work = _entry_dir(manifest, templates, tmp_path)

    entry = SkinExpander().expand("glass", manifest, templates, work)

    assert entry.name == "glass"
    assert entry.entry_hash == work.name
    assert entry.input_templates_hash == canonical_hash_dir(templates)
    assert entry.input_manifest_hash == hash_file(manifest)
    assert set(entry.artifact_hashes) == {"ags.css", "gtk.css", "skin.json"}
    assert (work / "ags.css").read_text() == (
        "/* glass */\n.panel { border-radius: 12px; opacity: 0.82; }\n"
    )
    assert json.loads((work / "skin.json").read_text()) == {
        "name": "glass",
        "radius": "12px",
        "surface_alpha": "0.82",
    }
    assert entry.artifact_hashes["ags.css"] == hash_file(work / "ags.css")


def test_json_manifest_is_equivalent(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    manifest = _manifest(tmp_path, fmt="json")
    work = _entry_dir(manifest, templates, tmp_path)

    entry = SkinExpander().expand("glass", manifest, templates, work)

    assert entry.input_manifest_hash == hash_file(manifest)
    assert (work / "ags.css").read_text().startswith("/* glass */")


def test_unresolved_placeholder_fails_closed(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    (templates / "ags.css.tpl").write_text("{{SKIN_MISSING}}", encoding="utf-8")
    manifest = _manifest(tmp_path)
    work = _entry_dir(manifest, templates, tmp_path)

    with pytest.raises(ValueError, match="undefined token"):
        SkinExpander().expand("glass", manifest, templates, work)
    assert not work.exists()


def test_manifest_name_must_match_selection(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    manifest = _manifest(tmp_path, name="flat")
    work = _entry_dir(manifest, templates, tmp_path)

    with pytest.raises(ValueError, match="does not match selected skin"):
        SkinExpander().expand("glass", manifest, templates, work)


@pytest.mark.parametrize(
    "payload",
    [
        'name = "glass"\nversion = 1\n[tokens]\n[apps]\n',
        'name = "glass"\nversion = 2\n[apps]\nags = "ags.css.tpl"\n',
        'name = "glass"\nversion = 1\nbogus = true\n[apps]\nags = "ags.css.tpl"\n',
        'name = "glass"\nversion = 1\n[apps]\nags = "../escape.css.tpl"\n',
        'name = "glass"\nversion = 1\n[tokens]\nradius = ["x"]\n[apps]\nags = "ags.css.tpl"\n',
    ],
)
def test_invalid_manifest_shape_fails_loud(tmp_path: Path, payload: str) -> None:
    templates = _templates(tmp_path)
    manifest = tmp_path / "glass.toml"
    manifest.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError):
        SkinExpander().expand("glass", manifest, templates, tmp_path / "out")
    assert not (tmp_path / "out").exists()
