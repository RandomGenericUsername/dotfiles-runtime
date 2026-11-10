"""Format-aware appearance artifacts (contracts v0.10.0, Phase 3A-3).

The CSS family stays frozen: a skin with only ``*.gtk.css.j2`` templates
renders exactly as before and publishes no ``appearance.artifacts`` key. A
known non-CSS suffix publishes ``appearance/<consumer>.<ext>`` as a separate
manifest family, while an unknown ``*.j2`` suffix fails closed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import cast

import pytest

from runtime.adapters.appearance import AppearanceGenerator, load_appearance_entry
from runtime.adapters.cache import cache_entry_path
from runtime.adapters.generation_store import (
    GenerationManifestError,
    GenerationSpec,
    GenerationStore,
    generation_intent_hash,
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
    discover_templates,
    expand_v2_template,
    load_skin_manifest,
    render_v2_templates,
    resolve_v2_token_document,
    validate_v2_manifest,
)
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.domain.models import (
    AppearanceArtifact,
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    PaletteEntry,
    SkinArtifacts,
    SkinEntry,
    WallpaperEntry,
)
from tests.support.palette_fakes import v1_colors_yaml, v1_gtk_css

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
_V2 = _FIXTURES / "skins-v2"
_FORMAT = _FIXTURES / "skins-v2-format" / "templates"
_MANIFEST = _V2 / "manifests" / "glass.toml"
_WH = hashlib.sha256(b"format-artifact-wallpaper").hexdigest()
_PH = "b" * 64
_TS = "2026-11-10T00:00:00Z"
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


def _templates(tmp_path: Path, *artifacts: str) -> Path:
    """Copy the frozen v2 CSS templates and add selected artifact fixtures."""
    templates = tmp_path / "templates"
    shutil.copytree(_V2 / "templates", templates)
    for name in artifacts:
        shutil.copy2(_FORMAT / name, templates / name)
    return templates


def _skin(
    state_root: Path,
    templates: Path,
    *,
    name: str = "glass",
) -> tuple[SkinEntry, Path]:
    manifest_path = _MANIFEST
    manifest = load_skin_manifest(manifest_path)
    validate_v2_manifest(manifest, name, manifest_path)
    work = cache_entry_path(
        state_root,
        "skins",
        skin_entry_hash(canonical_hash_dir(templates), hash_file(manifest_path)),
    )
    entry = SkinExpander().expand(name, manifest_path, templates, work)
    CacheSeeder(state_root).write_skin_meta(
        entry_hash=entry.entry_hash,
        name=entry.name,
        input_templates_hash=entry.input_templates_hash,
        input_manifest_hash=entry.input_manifest_hash,
        artifact_hashes=dict(entry.artifact_hashes),
        generated_at=entry.generated_at,
    )
    return entry, manifest_path


def _palette(state_root: Path, mode: str = "dark") -> PaletteEntry:
    entry = cache_entry_path(state_root, "palettes", _PH)
    entry.mkdir(parents=True, exist_ok=True)
    files = {name: f"/* {name} */\n".encode() for name in _PALETTE_FILES}
    files["colors.yaml"] = (f'mode: "{mode}"\n' + v1_colors_yaml()).encode()
    files["colors.gtk.css"] = v1_gtk_css().encode()
    hashes: dict[str, str] = {}
    for name, data in files.items():
        (entry / name).write_bytes(data)
        hashes[name] = hash_file(entry / name)
    CacheSeeder(state_root).write_palette_meta(
        entry_hash=_PH,
        source_wallpaper_hash=_WH,
        input_template_hash="t" * 64,
        artifact_hashes=hashes,
        generated_at=_TS,
    )
    return PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash=_PH,
        source_wallpaper_hash=_WH,
        input_template_hash="t" * 64,
        artifact_hashes={},  # type: ignore[typeddict-item]
        generated_at=_TS,
    )


def _appearance(
    tmp_path: Path, templates: Path
) -> tuple[object, Path, SkinEntry]:
    state_root = tmp_path / "state"
    palette = _palette(state_root)
    skin, manifest_path = _skin(state_root, templates)
    entry, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=tmp_path / "install",
        manifest_path=manifest_path,
        templates_dir=templates,
    )
    return entry, state_root, skin


def _commit(tmp_path: Path, entry: object, skin: SkinEntry) -> tuple[GenerationStore, str]:
    state_root = tmp_path / "state"
    wallpaper_entry = cache_entry_path(state_root, "wallpapers", _WH)
    wallpaper_entry.mkdir(parents=True, exist_ok=True)
    (wallpaper_entry / "wallpaper.png").write_bytes(b"wallpaper")
    state = DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=_WH,
            source_path="/img/wall.png",
            imported_at=_TS,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=_WH,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=_palette(state_root),
        effects=None,
        icons=None,
        applied_at=_TS,
        skin=skin,
    )
    JsonStateRepository(state_root=state_root).save(state)
    store = GenerationStore(state_root)
    result = store.commit(
        GenerationSpec(
            state=state,
            appearance=entry,  # type: ignore[arg-type]
            mode=cast("object", entry).mode,  # type: ignore[attr-defined]
            skin_name=skin.name,
            skin_hash=skin.entry_hash,
            intent_hash=generation_intent_hash(
                wallpaper=_WH,
                palette=_PH,
                skin=skin.name,
                mode=cast("object", entry).mode,  # type: ignore[attr-defined]
            ),
            monitor_names=("DP-1",),
            source_kind="cli",
        )
    )
    return store, result.generation_hash


def test_discovery_maps_every_known_suffix(tmp_path: Path) -> None:
    templates = _templates(
        tmp_path, "rofi.rasi.j2", "kitty.conf.j2", "btop.theme.j2"
    )
    discovered = {spec.path.name: spec for spec in discover_templates(templates)}

    assert discovered["ags.gtk.css.j2"].family == "targets"
    assert discovered["ags.gtk.css.j2"].format == "css"
    assert discovered["ags.gtk.css.j2"].output == "ags.css"
    assert discovered["rofi.rasi.j2"].family == "artifacts"
    assert discovered["rofi.rasi.j2"].consumer == "rofi"
    assert discovered["rofi.rasi.j2"].format == "rasi"
    assert discovered["rofi.rasi.j2"].output == "rofi.rasi"
    assert discovered["kitty.conf.j2"].output == "kitty.conf"
    assert discovered["btop.theme.j2"].output == "btop.theme"


@pytest.mark.parametrize("name", ["foo.scss.j2", "foo.css.j2", "foo.j2"])
def test_unknown_j2_suffix_fails_closed(tmp_path: Path, name: str) -> None:
    templates = _templates(tmp_path)
    (templates / name).write_text("x\n", encoding="utf-8")
    work = tmp_path / "out"

    with pytest.raises(ValueError, match="unknown suffix"):
        SkinExpander().expand("glass", _MANIFEST, templates, work)
    assert not work.exists()


def test_duplicate_consumer_across_families_fails_closed(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    (templates / "ags.rasi.j2").write_text("* {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="collide on consumer 'ags'"):
        SkinExpander().expand("glass", _MANIFEST, templates, tmp_path / "out")


def test_css_only_generation_is_unchanged_and_has_no_artifacts(
    tmp_path: Path,
) -> None:
    templates = _templates(tmp_path)
    manifest = load_skin_manifest(_MANIFEST)
    tokens = resolve_v2_token_document(manifest, "dark")

    rendered = render_v2_templates(templates, tokens)
    assert set(rendered) == {
        f"{path.name[: -len('.gtk.css.j2')]}.css"
        for path in templates.glob("*.gtk.css.j2")
    }
    for path in templates.glob("*.gtk.css.j2"):
        expected = expand_v2_template(path.read_text(encoding="utf-8"), tokens)
        assert rendered[f"{path.name[: -len('.gtk.css.j2')]}.css"] == expected

    entry, state_root, _skin_entry = _appearance(tmp_path, templates)
    assert cast("object", entry).artifacts == ()  # type: ignore[attr-defined]
    entry_dir = (
        state_root
        / "cache"
        / "appearance"
        / cast("object", entry).appearance_hash  # type: ignore[attr-defined]
    )
    assert not (entry_dir / "rofi.rasi").exists()

    store, generation = _commit(tmp_path, entry, _skin_entry)
    manifest_data = store.verify(generation)
    assert set(manifest_data["appearance"]) == {"tokensPath", "targets"}
    assert manifest_data["appearance"]["targets"] == [
        {"configDir": spec.consumer, "path": f"appearance/{spec.output}"}
        for spec in sorted(
            (spec for spec in discover_templates(templates) if spec.family == "targets"),
            key=lambda spec: spec.consumer,
        )
    ]
    assert (store.generation_path(generation) / "appearance" / "ags.css").is_file()


def test_valid_rasi_artifact_generation_and_manifest(tmp_path: Path) -> None:
    templates = _templates(tmp_path, "rofi.rasi.j2")
    entry, state_root, skin = _appearance(tmp_path, templates)
    appearance = cast("object", entry)
    entry_dir = state_root / "cache" / "appearance" / appearance.appearance_hash  # type: ignore[attr-defined]

    assert appearance.artifacts == (  # type: ignore[attr-defined]
        AppearanceArtifact(
            consumer="rofi",
            path="appearance/rofi.rasi",
            format="rasi",
        ),
    )
    text = (entry_dir / "rofi.rasi").read_text(encoding="utf-8")
    assert "@surface" in text
    assert "@on-surface" in text
    assert "@primary" in text
    assert "12px" in text and "1.5px" in text
    assert "alpha(" not in text
    assert "on_surface" not in text
    assert "#" not in text
    assert "structure" not in text and "variant" not in text

    store, generation = _commit(tmp_path, entry, skin)
    generation_manifest = store.verify(generation)
    assert generation_manifest["appearance"]["artifacts"] == [
        {"consumer": "rofi", "path": "appearance/rofi.rasi", "format": "rasi"}
    ]
    assert all(
        target["path"].endswith(".css")
        for target in generation_manifest["appearance"]["targets"]
    )
    assert (store.generation_path(generation) / "appearance" / "rofi.rasi").read_text(
        encoding="utf-8"
    ) == text


def test_conf_and_theme_reserved_shapes(tmp_path: Path) -> None:
    templates = _templates(tmp_path, "kitty.conf.j2", "btop.theme.j2")
    entry, state_root, skin = _appearance(tmp_path, templates)
    appearance = cast("object", entry)
    assert appearance.artifacts == (  # type: ignore[attr-defined]
        AppearanceArtifact("btop", "appearance/btop.theme", "theme"),
        AppearanceArtifact("kitty", "appearance/kitty.conf", "conf"),
    )
    entry_dir = state_root / "cache" / "appearance" / appearance.appearance_hash  # type: ignore[attr-defined]
    assert (entry_dir / "kitty.conf").is_file()
    assert (entry_dir / "btop.theme").is_file()

    store, generation = _commit(tmp_path, entry, skin)
    artifacts = store.verify(generation)["appearance"]["artifacts"]
    assert artifacts == [
        {"consumer": "btop", "path": "appearance/btop.theme", "format": "theme"},
        {"consumer": "kitty", "path": "appearance/kitty.conf", "format": "conf"},
    ]


def test_conf_paint_fails_closed(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    (templates / "kitty.conf.j2").write_text(
        "background {{SKIN_SURFACE_PANEL_BG}}\n", encoding="utf-8"
    )

    work = tmp_path / "out"
    work = work.parent / skin_entry_hash(canonical_hash_dir(templates), hash_file(_MANIFEST))
    with pytest.raises(ValueError, match="cannot be projected into conf"):
        SkinExpander().expand("glass", _MANIFEST, templates, work)
    assert not work.exists()


def test_artifact_template_participates_in_skin_and_generation_identity(
    tmp_path: Path,
) -> None:
    css_templates = _templates(tmp_path / "css")
    artifact_templates = _templates(tmp_path / "artifact", "rofi.rasi.j2")

    css_entry, _css_state, css_skin = _appearance(tmp_path / "css", css_templates)
    art_entry, _art_state, art_skin = _appearance(
        tmp_path / "artifact", artifact_templates
    )
    css_store, css_generation = _commit(
        tmp_path / "css", css_entry, css_skin
    )
    art_store, art_generation = _commit(
        tmp_path / "artifact", art_entry, art_skin
    )

    assert css_skin.entry_hash != art_skin.entry_hash
    assert cast("object", css_entry).appearance_hash != cast(  # type: ignore[attr-defined]
        "object", art_entry
    ).appearance_hash  # type: ignore[attr-defined]
    assert css_generation != art_generation
    assert (css_store.generation_path(css_generation) / "appearance" / "rofi.rasi").exists() is False
    assert (art_store.generation_path(art_generation) / "appearance" / "rofi.rasi").is_file()


def test_manifest_rejects_duplicate_consumer_and_path(tmp_path: Path) -> None:
    templates = _templates(tmp_path, "rofi.rasi.j2")
    entry, _state_root, skin = _appearance(tmp_path, templates)
    store, generation = _commit(tmp_path, entry, skin)
    original = store.load_manifest(generation)
    assert original is not None

    duplicate_consumer = json.loads(json.dumps(original))
    duplicate_consumer["appearance"]["artifacts"].append(
        {"consumer": "ags", "path": "appearance/ags.rasi", "format": "rasi"}
    )
    with pytest.raises(GenerationManifestError, match="duplicated"):
        store._validate_manifest(duplicate_consumer, generation)

    duplicate_path = json.loads(json.dumps(original))
    duplicate_path["appearance"]["artifacts"].append(
        {"consumer": "other", "path": "appearance/rofi.rasi", "format": "rasi"}
    )
    with pytest.raises(GenerationManifestError, match="duplicate appearance path"):
        store._validate_manifest(duplicate_path, generation)

    mismatch = json.loads(json.dumps(original))
    mismatch["appearance"]["artifacts"][0]["path"] = "appearance/rofi.conf"
    with pytest.raises(GenerationManifestError, match="does not match its format"):
        store._validate_manifest(mismatch, generation)


def test_missing_artifact_fails_verify(tmp_path: Path) -> None:
    templates = _templates(tmp_path, "rofi.rasi.j2")
    entry, _state_root, skin = _appearance(tmp_path, templates)
    store, generation = _commit(tmp_path, entry, skin)
    (store.generation_path(generation) / "appearance" / "rofi.rasi").unlink()

    with pytest.raises(GenerationManifestError, match="artifact missing"):
        store.verify(generation)


def test_gc_protects_current_generation_artifact(tmp_path: Path) -> None:
    templates = _templates(tmp_path, "rofi.rasi.j2")
    entry, state_root, skin = _appearance(tmp_path, templates)
    store, generation = _commit(tmp_path, entry, skin)
    appearance_hash = cast("object", entry).appearance_hash  # type: ignore[attr-defined]

    old = state_root / "cache" / "appearance" / ("ab" * 32)
    old.mkdir(parents=True)
    (old / "appearance.json").write_text("{}", encoding="utf-8")
    import os

    os.utime(old, (1, 1))

    store.gc(
        keep=0,
        grace_seconds=0,
        acknowledged_generations=[generation],
        current_generation=generation,
    )

    assert (state_root / "cache" / "appearance" / appearance_hash).is_dir()
    assert (state_root / "current" / "appearance" / "rofi.rasi").is_file()
    assert not old.exists()
