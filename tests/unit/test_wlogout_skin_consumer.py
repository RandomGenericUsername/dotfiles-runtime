"""wlogout as a generic v2 skin consumer (W-2.3/W-2.4).

The fixture is the released ``dotfiles-assets v0.15.0``
``skins/templates/wlogout.gtk.css.j2`` copied byte-for-byte (sha256
``e42a44e61deee2b00801252332abb9a5e4bc2b817db9b727b98ebef6313e32e3``).
These tests prove the existing skin -> appearance -> generation pipeline
derives the wlogout target with no wlogout-specific production code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from runtime.adapters.appearance import AppearanceGenerator
from runtime.adapters.contract_schemas import generation_manifest_validator
from runtime.adapters.generation_store import (
    GenerationSpec,
    GenerationStore,
    generation_intent_hash,
)
from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.application.doctor import DoctorUseCase
from runtime.domain.models import (
    AppearanceEntry,
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    PaletteEntry,
    SkinEntry,
    WallpaperEntry,
    skin_config_dirs,
)
from tests.support.palette_fakes import v1_gtk_css
from tests.unit.test_appearance_v2_assets import (
    _TEMPLATES,
    _WH,
    _write_palette,
    _write_skin,
)

_TEMPLATE = _TEMPLATES / "wlogout.gtk.css.j2"
_RELEASED_SHA256 = "e42a44e61deee2b00801252332abb9a5e4bc2b817db9b727b98ebef6313e32e3"
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Z0-9_]+)\s*\}\}")
_FORBIDDEN_PLACEHOLDER_PREFIXES = (
    "SKIN_LAYOUT_",
    "SKIN_STRUCTURE",
    "SKIN_VARIANT",
    "SKIN_ORDER",
    "SKIN_PRESENCE",
)
_LAYOUT_ID_RE = re.compile(r"#(?:lock|logout|reboot|shutdown|suspend|hibernate)\b")


def _desktop_state(*, palette: PaletteEntry, skin: SkinEntry) -> DesktopState:
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=_WH,
            source_path="/img/wall.png",
            imported_at="2026-11-09T00:00:00Z",
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
        palette=palette,
        effects=None,
        icons=None,
        applied_at="2026-11-09T00:00:00Z",
        skin=skin,
    )


def _generate_wlogout(
    tmp_path: Path,
) -> tuple[Path, GenerationStore, SkinEntry, PaletteEntry, AppearanceEntry]:
    state_root = tmp_path / "state"
    palette = _write_palette(state_root, "a" * 64, "dark")
    skin, manifest_path = _write_skin(state_root, "glass")
    spine = tmp_path / "install"
    (spine / "config" / "wlogout").mkdir(parents=True)
    entry, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=spine,
        manifest_path=manifest_path,
        templates_dir=_TEMPLATES,
    )
    return state_root, GenerationStore(state_root), skin, palette, entry


def test_fixture_is_the_released_v0_14_0_template() -> None:
    assert hash_file(_TEMPLATE) == _RELEASED_SHA256
    source = _TEMPLATE.read_text(encoding="utf-8")
    placeholders = set(_PLACEHOLDER_RE.findall(source))
    assert placeholders
    assert not any(
        placeholder.startswith(prefix)
        for placeholder in placeholders
        for prefix in _FORBIDDEN_PLACEHOLDER_PREFIXES
    )
    assert not re.search(r"#[0-9A-Fa-f]{3,8}\b", source)
    assert _LAYOUT_ID_RE.search(source) is None
    assert "transition" not in source
    assert "animation" not in source


def test_skin_expansion_exposes_the_wlogout_target(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    skin, _manifest_path = _write_skin(state_root, "glass")

    assert "wlogout.css" in skin.artifact_hashes
    assert (state_root / "cache" / "skins" / skin.entry_hash / "wlogout.css").is_file()
    assert "wlogout" in skin_config_dirs(skin.artifact_hashes)

    composed_root, _store, _skin, _palette, entry = _generate_wlogout(tmp_path / "world")
    assert "wlogout" in entry.config_dirs
    appearance_dir = composed_root / "cache" / "appearance" / entry.appearance_hash
    assert (appearance_dir / "wlogout.css").is_file()


def test_generation_commit_writes_and_validates_the_wlogout_target(tmp_path: Path) -> None:
    _state_root, store, skin, palette, entry = _generate_wlogout(tmp_path)
    state = _desktop_state(palette=palette, skin=skin)
    result = store.commit(
        GenerationSpec(
            state=state,
            appearance=entry,
            mode="dark",
            skin_name=skin.name,
            skin_hash=skin.entry_hash,
            intent_hash=generation_intent_hash(
                wallpaper=_WH,
                palette=palette.entry_hash,
                skin=skin.name,
                mode="dark",
            ),
            monitor_names=("DP-1",),
            source_kind="cli",
            created_at="2026-11-09T00:00:00Z",
        )
    )

    generation = result.path
    assert (generation / "appearance" / "wlogout.css").is_file()
    target = {"configDir": "wlogout", "path": "appearance/wlogout.css"}
    assert target in result.manifest["appearance"]["targets"]
    config_dirs = [
        item["configDir"] for item in result.manifest["appearance"]["targets"]
    ]
    assert config_dirs == sorted(config_dirs)

    # W-2.4: the committed manifest is a generation-manifest fixture for the
    # embedded contracts v0.9.0 schema.
    generation_manifest_validator()(result.manifest)
    on_disk = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == result.manifest


def test_composed_wlogout_sheet_is_palette_plus_paint_only_skin(tmp_path: Path) -> None:
    state_root, _store, _skin, _palette, entry = _generate_wlogout(tmp_path)
    sheet = (
        state_root / "cache" / "appearance" / entry.appearance_hash / "wlogout.css"
    ).read_text(encoding="utf-8")

    legacy = v1_gtk_css().rstrip("\n")
    assert sheet.startswith(legacy)
    assert "@define-color primary #000000;" in sheet
    assert "/* appearance: palette roles */" in sheet
    skin_section = sheet.split("/* appearance: skin */", 1)[1]
    assert "button {" in skin_section
    assert "background-color: alpha(@surface, 0.94);" in skin_section
    assert "border-radius: 7px;" in skin_section
    assert "{{SKIN_" not in sheet

    # The skin half is paint/rhythm only: palette hex lives in the palette
    # half; no transition/animation, layout IDs, or structure/variant tokens.
    assert "#" not in skin_section
    assert "transition" not in skin_section
    assert "animation" not in skin_section
    assert _LAYOUT_ID_RE.search(skin_section) is None
    assert not any(
        placeholder.startswith(prefix)
        for placeholder in _PLACEHOLDER_RE.findall(skin_section)
        for prefix in _FORBIDDEN_PLACEHOLDER_PREFIXES
    )


def test_wlogout_stable_link_survives_skin_and_mode_reconverge(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    spine = tmp_path / "install"
    (spine / "config" / "wlogout").mkdir(parents=True)
    palette = _write_palette(state_root, "a" * 64, "dark")
    skin, manifest_path = _write_skin(state_root, "glass")
    entry, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=spine,
        manifest_path=manifest_path,
        templates_dir=_TEMPLATES,
    )
    assert "wlogout" in entry.config_dirs
    seeder = CacheSeeder(state_root)
    seeder.publish_appearance(spine, entry)

    link = spine / "config" / "wlogout" / "appearance.css"
    stable_target = state_root / "current" / "appearance" / "wlogout.css"
    assert link.is_symlink()
    assert link.readlink() == stable_target
    assert link.resolve().is_file()

    # A mode reconverge produces a new appearance generation; the stable link
    # text never changes and resolves through the one `current` hop.
    light_palette = _write_palette(state_root, "b" * 64, "light")
    light_entry, _hit = AppearanceGenerator(state_root).ensure(
        palette_entry=light_palette,
        skin_entry=skin,
        install_spine=spine,
        manifest_path=manifest_path,
        templates_dir=_TEMPLATES,
    )
    assert light_entry.mode == "light"
    assert light_entry.appearance_hash != entry.appearance_hash
    seeder.publish_appearance(spine, light_entry)
    assert link.is_symlink()
    assert link.readlink() == stable_target
    assert link.resolve() == (
        state_root / "cache" / "appearance" / light_entry.appearance_hash / "wlogout.css"
    ).resolve()

    # The v3 converge cleanup must preserve the same provisioning-owned link.
    seeder.prune_runtime_spine_pointers(
        spine, published=light_entry.config_dirs
    )
    assert link.is_symlink()
    assert link.resolve().is_file()

    # Commit the light generation so doctor has a current state to check.
    state = _desktop_state(palette=light_palette, skin=skin)
    repo = JsonStateRepository(state_root=state_root)
    repo.save(state)
    GenerationStore(state_root).commit(
        GenerationSpec(
            state=state,
            appearance=light_entry,
            mode="light",
            skin_name=skin.name,
            skin_hash=skin.entry_hash,
            intent_hash=generation_intent_hash(
                wallpaper=_WH,
                palette=light_palette.entry_hash,
                skin=skin.name,
                mode="light",
            ),
            monitor_names=("DP-1",),
            source_kind="cli",
            created_at="2026-11-09T00:00:00Z",
        )
    )

    # BUG-016's doctor rule accepts the stable link: no consumer drift row.
    items = DoctorUseCase(
        state_repo=repo,
        state_root=state_root,
        install_spine=spine,
    ).check().items
    assert "consumer/config/wlogout/appearance.css" not in {
        item.name for item in items
    }
