"""Real v2 appearance assets: TOML manifests, nested groups, modes, units.

The fixtures under ``tests/fixtures/skins-v2`` are byte-identical copies of
``dotfiles-assets v0.12.0`` ``skins/manifests/{glass,flat,paper}.toml`` and the
seven AGS ``skins/templates/*.gtk.css.j2`` targets, plus the released
``dotfiles-assets v0.14.0`` ``skins/templates/wlogout.gtk.css.j2``. They are
copied so the suite is hermetic; the same files are expanded directly from the
sibling assets checkout in the ticket evidence.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from runtime.adapters.appearance import AppearanceGenerator, detect_palette_mode
from runtime.adapters.cache import cache_entry_path
from runtime.adapters.contract_schemas import (
    appearance_artifact_validator,
    appearance_json_validator,
)
from runtime.adapters.hashing import (
    appearance_generation_hash,
    canonical_hash_dir,
    hash_file,
    skin_entry_hash,
)
from runtime.adapters.seeder import CacheSeeder
from runtime.adapters.skin_expander import (
    SkinExpander,
    expand_v2_template,
    load_skin_manifest,
    manifest_schema_version,
    render_v2_templates,
    resolve_v2_token_document,
    validate_v2_manifest,
)
from runtime.domain.models import PaletteEntry, SkinArtifacts, SkinEntry, skin_config_dirs
from tests.support.palette_fakes import v1_colors_yaml, v1_gtk_css

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "skins-v2"
_MANIFESTS = _FIXTURES / "manifests"
_TEMPLATES = _FIXTURES / "templates"
_NAMES = ("glass", "flat", "paper")
_MODES = ("dark", "light")
_CONFIG_DIRS = tuple(
    sorted(path.name[: -len(".gtk.css.j2")] for path in _TEMPLATES.glob("*.gtk.css.j2"))
)
_WH = "ab" * 32
_TS = "2026-11-06T00:00:00Z"
_PALETTE_FILES = (
    "colors.conf",
    "colors.gtk.css",
    "colors.yaml",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
    "colors.kitty",
    "colors.btop.theme",
)


def _manifest(name: str) -> tuple[dict[str, object], Path]:
    path = _MANIFESTS / f"{name}.toml"
    return load_skin_manifest(path), path


def _entry_dir(tmp_path: Path, manifest: Path) -> Path:
    expected = skin_entry_hash(canonical_hash_dir(_TEMPLATES), hash_file(manifest))
    return tmp_path / "cache" / "skins" / expected


def _write_palette(state_root: Path, entry_hash: str, mode: str) -> PaletteEntry:
    entry = cache_entry_path(state_root, "palettes", entry_hash)
    entry.mkdir(parents=True, exist_ok=True)
    colors = f'mode: "{mode}"\n' + v1_colors_yaml()
    files = {name: f"/* {name} */\n".encode() for name in _PALETTE_FILES}
    files["colors.yaml"] = colors.encode()
    files["colors.gtk.css"] = v1_gtk_css().encode()
    hashes: dict[str, str] = {}
    for name, data in files.items():
        (entry / name).write_bytes(data)
        hashes[name] = hash_file(entry / name)
    CacheSeeder(state_root).write_palette_meta(
        entry_hash=entry_hash,
        source_wallpaper_hash=_WH,
        input_template_hash="t" * 64,
        artifact_hashes=hashes,
        generated_at=_TS,
    )
    return PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash=entry_hash,
        source_wallpaper_hash=_WH,
        input_template_hash="t" * 64,
        artifact_hashes={},  # type: ignore[typeddict-item]
        generated_at=_TS,
    )


def _write_skin(state_root: Path, name: str) -> tuple[SkinEntry, Path]:
    manifest, manifest_path = _manifest(name)
    version = manifest_schema_version(manifest, manifest_path)
    assert version == 2
    validate_v2_manifest(manifest, name, manifest_path)
    expected = skin_entry_hash(canonical_hash_dir(_TEMPLATES), hash_file(manifest_path))
    work = cache_entry_path(state_root, "skins", expected)
    entry = SkinExpander().expand(name, manifest_path, _TEMPLATES, work)
    CacheSeeder(state_root).write_skin_meta(
        entry_hash=entry.entry_hash,
        name=entry.name,
        input_templates_hash=entry.input_templates_hash,
        input_manifest_hash=entry.input_manifest_hash,
        artifact_hashes=dict(entry.artifact_hashes),
        generated_at=entry.generated_at,
    )
    return entry, manifest_path


def _spine(tmp_path: Path) -> Path:
    spine = tmp_path / "install"
    for config_dir in _CONFIG_DIRS:
        (spine / "config" / config_dir).mkdir(parents=True, exist_ok=True)
    return spine


def _compose(
    tmp_path: Path, name: str, mode: str
) -> tuple[object, Path, Path, SkinEntry]:
    state_root = tmp_path / "state"
    palette = _write_palette(state_root, ("a" if mode == "dark" else "b") * 64, mode)
    skin, manifest_path = _write_skin(state_root, name)
    entry, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=_spine(tmp_path),
        manifest_path=manifest_path,
        templates_dir=_TEMPLATES,
    )
    return entry, state_root, _spine(tmp_path), skin


@pytest.mark.parametrize("name", _NAMES)
def test_real_v2_manifest_expands_to_v2_tokens_and_css(tmp_path: Path, name: str) -> None:
    manifest, manifest_path = _manifest(name)
    assert manifest_schema_version(manifest, manifest_path) == 2
    validate_v2_manifest(manifest, name, manifest_path)
    appearance_json_validator()(resolve_v2_token_document(manifest, "dark"))

    work = _entry_dir(tmp_path, manifest_path)
    entry = SkinExpander().expand(name, manifest_path, _TEMPLATES, work)

    assert entry.entry_hash == work.name
    assert set(entry.artifact_hashes) == {
        *(f"{config_dir}.css" for config_dir in _CONFIG_DIRS),
        "skin.json",
    }
    skin_doc = json.loads((work / "skin.json").read_text(encoding="utf-8"))
    assert set(skin_doc) == {"version", "surface", "state", "type", "ink", "layout"}
    assert skin_doc["version"] == 2 and "groups" not in skin_doc
    appearance_json_validator()(skin_doc)

    css = (work / "ags.css").read_text(encoding="utf-8")
    assert "{{SKIN_" not in css
    if name == "glass":
        assert "border: 1.5px solid @primary;" in css
        assert "letter-spacing: 0.05em;" in css
        assert "font-weight: 600;" in css


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("mode", _MODES)
def test_real_v2_tokens_validate_and_templates_expand(
    name: str, mode: str
) -> None:
    manifest, manifest_path = _manifest(name)
    validate_v2_manifest(manifest, name, manifest_path)
    tokens = resolve_v2_token_document(manifest, mode)
    assert set(tokens) == {"version", "surface", "state", "type", "ink", "layout"}
    assert tokens["version"] == 2
    appearance_json_validator()(tokens)

    rendered = render_v2_templates(_TEMPLATES, tokens)
    assert set(rendered) == {f"{config_dir}.css" for config_dir in _CONFIG_DIRS}
    assert all("{{SKIN_" not in text for text in rendered.values())


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("mode", _MODES)
def test_v2_flattened_placeholder_and_unit_rules(name: str, mode: str) -> None:
    manifest, manifest_path = _manifest(name)
    validate_v2_manifest(manifest, name, manifest_path)
    tokens = resolve_v2_token_document(manifest, mode)

    template = (
        "bg={{SKIN_SURFACE_PANEL_BG}}\n"
        "hover={{SKIN_SURFACE_CELL_HOVER_BG}}\n"
        "radius={{SKIN_SURFACE_CARD_RADIUS}}\n"
        "body={{SKIN_TYPE_SIZE_BODY}}\n"
        "track={{SKIN_SURFACE_PILL_LETTER_SPACING}}\n"
        "weight={{SKIN_SURFACE_CONTROL_FONT_WEIGHT}}\n"
    )
    lines = expand_v2_template(template, tokens).splitlines()
    by_key = dict(line.split("=", 1) for line in lines)

    assert by_key["bg"] == tokens["surface"]["panel"]["bg"]  # type: ignore[index]
    assert by_key["hover"] == tokens["surface"]["cell"]["hover-bg"]  # type: ignore[index]
    assert by_key["radius"].endswith("px")
    assert by_key["body"].endswith("px")
    assert by_key["track"].endswith("em")
    assert not by_key["weight"].endswith(("px", "em"))


def test_v2_mode_overrides_are_whole_group_replacements() -> None:
    glass, glass_path = _manifest("glass")
    flat, flat_path = _manifest("flat")
    paper, paper_path = _manifest("paper")
    for name, manifest, path in (
        ("glass", glass, glass_path),
        ("flat", flat, flat_path),
        ("paper", paper, paper_path),
    ):
        validate_v2_manifest(manifest, name, path)

    # No authored overrides: both modes resolve to the base groups.
    assert resolve_v2_token_document(glass, "dark") == resolve_v2_token_document(
        glass, "light"
    )
    assert resolve_v2_token_document(flat, "dark") == resolve_v2_token_document(
        flat, "light"
    )

    dark = resolve_v2_token_document(paper, "dark")
    light = resolve_v2_token_document(paper, "light")
    # paper authored mode.light.surface/state/ink; type/layout inherit.
    assert dark["surface"] != light["surface"]
    assert dark["state"] != light["state"]
    # The authored ink group may equal the base; the rule under test is that the
    # complete override group is selected as a whole, not deep-merged.
    assert light["ink"] == paper["mode"]["light"]["ink"]  # type: ignore[index]
    assert dark["type"] == light["type"]
    assert dark["layout"] == light["layout"]
    assert light["surface"]["cell"]["hover-bg"] == "alpha(@on_surface, 0.06)"  # type: ignore[index]
    assert light["surface"]["panel"]["shadow"] == "0 8px 22px alpha(@shadow, 0.16)"  # type: ignore[index]


def test_v2_incomplete_mode_override_fails_closed() -> None:
    paper, paper_path = _manifest("paper")
    incomplete = json.loads(json.dumps(paper))
    del incomplete["mode"]["light"]["surface"]["card"]["radius"]  # type: ignore[index]
    with pytest.raises(ValueError):
        validate_v2_manifest(incomplete, "paper", paper_path)


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        ("schema-version", lambda d: d.__setitem__("schemaVersion", 1)),
        ("structure", lambda d: d.__setitem__("structure", {"foo": "bar"})),
        (
            "variant-token",
            lambda d: d["groups"]["surface"]["panel"].__setitem__("variant", "x"),
        ),
        (
            "literal-hex",
            lambda d: d["groups"]["surface"]["panel"].__setitem__("bg", "#ffffff"),
        ),
        (
            "transition",
            lambda d: d["groups"]["surface"]["panel"].__setitem__(
                "bg", "transition: color 1s"
            ),
        ),
        (
            "unit-string",
            lambda d: d["groups"]["surface"]["panel"].__setitem__("radius", "12px"),
        ),
    ],
)
def test_invalid_v2_manifest_fails_closed(
    tmp_path: Path, case: str, mutate: object
) -> None:
    manifest, _path = _manifest("glass")
    data = copy.deepcopy(manifest)
    mutate(data)  # type: ignore[operator]
    manifest_path = tmp_path / "glass.json"
    manifest_path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        SkinExpander().expand("glass", manifest_path, _TEMPLATES, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_v2_placeholder_errors_fail_closed() -> None:
    manifest, manifest_path = _manifest("glass")
    validate_v2_manifest(manifest, "glass", manifest_path)
    tokens = resolve_v2_token_document(manifest, "dark")

    with pytest.raises(ValueError, match="unknown placeholder"):
        expand_v2_template("{{SKIN_NOPE}}", tokens)
    with pytest.raises(ValueError, match="layout placeholder"):
        expand_v2_template("{{SKIN_LAYOUT_AGS_PANEL_WIDTH}}", tokens)
    with pytest.raises(ValueError, match="conditional"):
        expand_v2_template("{% if mode == 'dark' %}x{% endif %}", tokens)
    with pytest.raises(ValueError, match="unresolved placeholder"):
        expand_v2_template("{{SKIN_SURFACE_PANEL_BG", tokens)


@pytest.mark.parametrize(("name", "mode"), [("glass", "dark"), ("paper", "light")])
def test_composed_generation_uses_mode_resolved_v2_css(
    tmp_path: Path, name: str, mode: str
) -> None:
    entry, state_root, spine, skin = _compose(tmp_path, name, mode)
    assert entry.mode == mode  # type: ignore[attr-defined]
    gen = state_root / "cache" / "appearance" / entry.appearance_hash  # type: ignore[attr-defined]

    tokens = json.loads((gen / "tokens.json").read_text(encoding="utf-8"))
    appearance_json_validator()(tokens)
    metadata = json.loads((gen / "appearance.json").read_text(encoding="utf-8"))
    appearance_artifact_validator()(metadata)
    assert entry.appearance_hash == appearance_generation_hash(  # type: ignore[attr-defined]
        metadata["paletteHash"], skin.entry_hash, mode
    )

    css = (gen / "ags.css").read_text(encoding="utf-8")
    assert css.startswith(v1_gtk_css().rstrip("\n"))
    assert "@define-color primary #000000;" in css
    assert "/* appearance: skin */" in css
    assert "{{SKIN_" not in css
    if name == "glass":
        assert "border: 1.5px solid @primary;" in css
    else:
        # paper mode.light override is applied before expansion.
        assert "0 8px 22px alpha(@shadow, 0.16)" in css
        assert "rgba(0,0,0,0.22)" not in css

    link = state_root / "current" / "appearance"
    CacheSeeder(state_root).publish_appearance(spine, entry)  # type: ignore[arg-type]
    assert link.is_symlink()
    assert link.readlink() == gen
    for config_dir in skin_config_dirs(skin.artifact_hashes):
        consumer = spine / "config" / config_dir / "appearance.css"
        assert consumer.is_symlink()
        assert consumer.readlink() == link / f"{config_dir}.css"


@pytest.mark.parametrize("name", _NAMES)
def test_real_v2_skin_cache_identity_is_templates_plus_manifest(
    tmp_path: Path, name: str
) -> None:
    manifest, manifest_path = _manifest(name)
    expected = skin_entry_hash(canonical_hash_dir(_TEMPLATES), hash_file(manifest_path))
    entry = SkinExpander().expand(name, manifest_path, _TEMPLATES, _entry_dir(tmp_path, manifest_path))
    assert entry.entry_hash == expected
    assert entry.input_manifest_hash == hash_file(manifest_path)
    assert entry.input_templates_hash == canonical_hash_dir(_TEMPLATES)


def test_palette_mode_defaults_to_dark_when_absent() -> None:
    assert detect_palette_mode(v1_colors_yaml()) == "dark"
    assert detect_palette_mode('mode: "light"\n' + v1_colors_yaml()) == "light"
    assert detect_palette_mode('scheme: "light"\n' + v1_colors_yaml()) == "light"
    assert detect_palette_mode("is_dark: false\n" + v1_colors_yaml()) == "light"
    assert detect_palette_mode("dark: false\n" + v1_colors_yaml()) == "light"
