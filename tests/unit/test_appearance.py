"""Appearance plane v2: one generation, one pointer, mode-aware identity.

Covers the REQ-014 acceptance surface: generation layout/schema, the
``f(palette, skin, mode)`` identity, cache independence, the single-hop
pointer + stable convenience links, the retained v1 skin pointers,
prune/idempotency, inspect/doctor reporting and the v1/v2 skew.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast

import pytest

from runtime.adapters.appearance import (
    AppearanceGenerator,
    detect_palette_mode,
    load_appearance_entry,
    prune_appearance_entries,
    prune_stale_appearance_consumers,
    read_current_appearance,
    resolve_tokens,
    tokens_from_v1_skin,
)
from runtime.adapters.cache import cache_entry_path
from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
from runtime.adapters.contract_schemas import (
    appearance_artifact_validator,
    appearance_json_validator,
)
from runtime.adapters.hashing import appearance_generation_hash, hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.adapters.skin_expander import SkinExpander
from runtime.application.doctor import DoctorUseCase
from runtime.application.inspect import InspectStateUseCase
from runtime.application.reconcile import ReconcileDesktopStateUseCase
from runtime.domain.models import (
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

WH = hashlib.sha256(b"wallpaper").hexdigest()
PH = "b" * 64
PH2 = "c" * 64
SH = "d" * 64
SH2 = "e" * 64
TS = "2026-11-06T00:00:00Z"
PALETTE_FILES = (
    "colors.conf",
    "colors.gtk.css",
    "colors.yaml",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
    "colors.kitty",
    "colors.btop.theme",
)


def _skin_json() -> dict[str, object]:
    return {
        "version": 1,
        "shape": {
            "radius-panel": 12,
            "radius-card": 9,
            "radius-cell": 7,
            "radius-pill": 20,
            "border-w": 1.5,
            "hairline-w": 1,
        },
        "surface": {
            "bg-panel": "alpha(@surface, 0.94)",
            "bg-card": "alpha(@surface_container_high, 0.18)",
            "border": "@primary",
            "shadow": "none",
            "blur": "none",
        },
        "ink": {
            "ink": "@on_surface",
            "ink-dim": "alpha(@on_surface, 0.62)",
            "accent": "@primary",
            "ink-over": "auto",
        },
        "type": {"font-family": "inherit", "title-size": 12.5, "text-size": 13},
        "rhythm": {"gap": 8, "cell-pad": "8px 9px"},
        "layout": {
            "panel-width": 312,
            "bt-list-spacing": 4,
            "bar-row-height": 0,
            "chip-min": 0,
        },
    }


def _write_palette(state_root: Path, entry_hash: str = PH, *, mode: str | None = None) -> PaletteEntry:
    entry = cache_entry_path(state_root, "palettes", entry_hash)
    entry.mkdir(parents=True, exist_ok=True)
    colors = v1_colors_yaml()
    if mode is not None:
        colors = f'mode: "{mode}"\n' + colors
    files = {name: f"/* {name} */\n".encode() for name in PALETTE_FILES}
    files["colors.yaml"] = colors.encode()
    files["colors.gtk.css"] = v1_gtk_css().encode()
    hashes: dict[str, str] = {}
    for name, data in files.items():
        (entry / name).write_bytes(data)
        hashes[name] = hash_file(entry / name)
    CacheSeeder(state_root).write_palette_meta(
        entry_hash=entry_hash,
        source_wallpaper_hash=WH,
        input_template_hash="t" * 64,
        artifact_hashes=hashes,
        generated_at=TS,
    )
    return PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash=entry_hash,
        source_wallpaper_hash=WH,
        input_template_hash="t" * 64,
        artifact_hashes={},  # type: ignore[typeddict-item]
        generated_at=TS,
    )


def _write_skin(
    state_root: Path,
    entry_hash: str = SH,
    *,
    name: str = "glass",
    config_dirs: tuple[str, ...] = ("ags", "ags-clipboard"),
) -> SkinEntry:
    entry = cache_entry_path(state_root, "skins", entry_hash)
    entry.mkdir(parents=True, exist_ok=True)
    artifacts: dict[str, str] = {}
    for config_dir in config_dirs:
        filename = f"{config_dir}.css"
        (entry / filename).write_text(
            f".{config_dir} {{ border-radius: {config_dir}; }}\n", encoding="utf-8"
        )
        artifacts[filename] = hash_file(entry / filename)
    (entry / "skin.json").write_text(json.dumps(_skin_json()), encoding="utf-8")
    artifacts["skin.json"] = hash_file(entry / "skin.json")
    CacheSeeder(state_root).write_skin_meta(
        entry_hash=entry_hash,
        name=name,
        input_templates_hash="1" * 64,
        input_manifest_hash="2" * 64,
        artifact_hashes=artifacts,
        generated_at=TS,
    )
    return SkinEntry(
        hash_algorithm="sha256",
        kind="skin",
        entry_hash=entry_hash,
        name=name,
        input_templates_hash="1" * 64,
        input_manifest_hash="2" * 64,
        artifact_hashes=cast("SkinArtifacts", artifacts),
        generated_at=TS,
    )


def _spine(tmp_path: Path) -> Path:
    spine = tmp_path / "install"
    for config_dir in ("ags", "ags-clipboard"):
        (spine / "config" / config_dir).mkdir(parents=True, exist_ok=True)
    return spine


def _write_wallpaper_cache(state_root: Path) -> None:
    entry = cache_entry_path(state_root, "wallpapers", WH)
    entry.mkdir(parents=True, exist_ok=True)
    (entry / "wallpaper.png").write_bytes(b"wallpaper")


def _generate(
    tmp_path: Path,
    *,
    palette_hash: str = PH,
    skin_hash: str = SH,
    mode: str | None = None,
    manifest_path: Path | None = None,
    templates_dir: Path | None = None,
) -> tuple[object, bool, Path]:
    state_root = tmp_path / "state"
    palette = _write_palette(state_root, palette_hash, mode=mode)
    skin = _write_skin(state_root, skin_hash)
    entry, hit = AppearanceGenerator(state_root).ensure(
        palette_entry=palette,
        skin_entry=skin,
        install_spine=_spine(tmp_path),
        manifest_path=manifest_path,
        templates_dir=templates_dir,
    )
    return entry, hit, state_root


class TestGeneration:
    def test_layout_and_schema(self, tmp_path: Path) -> None:
        entry, hit, state_root = _generate(tmp_path)
        assert hit is False
        entry_dir = state_root / "cache" / "appearance" / entry.appearance_hash  # type: ignore[attr-defined]
        assert (entry_dir / "colors.yaml").read_bytes() == (
            cache_entry_path(state_root, "palettes", PH) / "colors.yaml"
        ).read_bytes()
        tokens = json.loads((entry_dir / "tokens.json").read_text(encoding="utf-8"))
        appearance_json_validator()(tokens)
        manifest = json.loads((entry_dir / "appearance.json").read_text(encoding="utf-8"))
        appearance_artifact_validator()(manifest)
        assert manifest == {
            "version": 2,
            "skin": "glass",
            "mode": "dark",
            "paletteHash": PH,
            "skinHash": SH,
            "appearanceHash": appearance_generation_hash(PH, SH, "dark"),
            "generationHash": manifest["generationHash"],
            "tokenRef": "tokens.json",
        }
        assert tokens["surface"]["panel"]["bg"] == "alpha(@surface, 0.94)"
        window = tokens["surface"]["window"]
        assert set(window) == {
            "bg",
            "border",
            "border-width",
            "radius",
            "padding",
            "shadow",
            "blur",
            "opacity",
        }
        assert window["opacity"] == 1.0
        composed = (entry_dir / "ags.css").read_text(encoding="utf-8")
        legacy = v1_gtk_css().rstrip("\n")
        assert composed.startswith(legacy)
        assert "@define-color primary #000000;" in composed
        assert ".ags { border-radius: ags; }" in composed
        # The skin CSS is last: it must win over the palette block.
        assert composed.index("@define-color primary") < composed.index(".ags {")

    def test_one_entry_per_identity_and_idempotent(self, tmp_path: Path) -> None:
        first, hit1, state_root = _generate(tmp_path)
        second, hit2, _ = _generate(tmp_path)
        assert hit1 is False and hit2 is True
        assert first.appearance_hash == second.appearance_hash  # type: ignore[attr-defined]
        entries = [
            path
            for path in (state_root / "cache" / "appearance").iterdir()
            if path.is_dir()
        ]
        assert [path.name for path in entries] == [first.appearance_hash]  # type: ignore[attr-defined]
        assert list((state_root / "cache").rglob("*.tmp.*")) == []

    def test_mode_is_part_of_identity(self, tmp_path: Path) -> None:
        dark, _, _ = _generate(tmp_path, mode="dark")
        light, _, _ = _generate(tmp_path, mode="light")
        assert dark.appearance_hash != light.appearance_hash  # type: ignore[attr-defined]
        assert dark.mode == "dark" and light.mode == "light"  # type: ignore[attr-defined]
        assert dark.appearance_hash == appearance_generation_hash(PH, SH, "dark")  # type: ignore[attr-defined]
        assert light.appearance_hash == appearance_generation_hash(PH, SH, "light")  # type: ignore[attr-defined]

    def test_mode_falls_back_to_dark_when_palette_has_no_mode(self) -> None:
        assert detect_palette_mode(v1_colors_yaml()) == "dark"
        assert detect_palette_mode('scheme: "light"\n' + v1_colors_yaml()) == "light"
        assert detect_palette_mode("is_dark: false\n" + v1_colors_yaml()) == "light"

    def test_v2_mode_override_is_applied_before_tokens(self, tmp_path: Path) -> None:
        groups = tokens_from_v1_skin(_skin_json())
        groups.pop("version")
        groups["ink"] = {
            "ink": "@on_surface",
            "ink-dim": "@on_surface_variant",
            "accent": "@primary",
            "accent-muted": "@primary",
            "ink-over": "auto",
        }
        light_ink = dict(groups["ink"])
        light_ink["accent"] = "@tertiary"
        manifest = {
            "schemaVersion": 2,
            "name": "glass",
            "groups": groups,
            "mode": {"light": {"ink": light_ink}},
        }
        manifest_path = tmp_path / "glass.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        templates_dir = tmp_path / "templates"
        templates_dir.mkdir()
        for config_dir in ("ags", "ags-clipboard"):
            (templates_dir / f"{config_dir}.gtk.css.j2").write_text(
                ".x { color: {{SKIN_INK_ACCENT}}; }\n", encoding="utf-8"
            )
        entry, _, state_root = _generate(
            tmp_path,
            mode="light",
            manifest_path=manifest_path,
            templates_dir=templates_dir,
        )
        tokens = json.loads(
            (state_root / "cache" / "appearance" / entry.appearance_hash / "tokens.json").read_text(  # type: ignore[attr-defined]
                encoding="utf-8"
            )
        )
        assert tokens["ink"]["accent"] == "@tertiary"
        appearance_json_validator()(tokens)

    def test_resolve_tokens_validates_v1_translation(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        skin = _write_skin(state_root)
        tokens = resolve_tokens(
            skin,
            cache_entry_path(state_root, "skins", SH),
            manifest_path=None,
            mode="dark",
        )
        assert tokens["version"] == 2
        assert tokens["layout"]["bar"]["panel-width"] == 312
        appearance_json_validator()(tokens)


class TestCacheIndependence:
    def test_palette_change_reuses_skin_bytes(self, tmp_path: Path) -> None:
        first, _, state_root = _generate(tmp_path, palette_hash=PH)
        skin_dir = cache_entry_path(state_root, "skins", SH)
        before = {path.name: path.read_bytes() for path in skin_dir.iterdir()}
        second, hit2, _ = _generate(tmp_path, palette_hash=PH2)
        after = {path.name: path.read_bytes() for path in skin_dir.iterdir()}
        assert hit2 is False
        assert first.appearance_hash != second.appearance_hash  # type: ignore[attr-defined]
        assert before == after

    def test_skin_change_reuses_palette_bytes(self, tmp_path: Path) -> None:
        first, _, state_root = _generate(tmp_path, skin_hash=SH)
        palette_dir = cache_entry_path(state_root, "palettes", PH)
        before = {path.name: path.read_bytes() for path in palette_dir.iterdir()}
        second, hit2, _ = _generate(tmp_path, skin_hash=SH2)
        after = {path.name: path.read_bytes() for path in palette_dir.iterdir()}
        assert hit2 is False
        assert first.appearance_hash != second.appearance_hash  # type: ignore[attr-defined]
        assert before == after


class TestPointerAndConsumers:
    def test_single_hop_pointer_and_stable_convenience_links(self, tmp_path: Path) -> None:
        entry, _, state_root = _generate(tmp_path)
        spine = _spine(tmp_path)
        seeder = CacheSeeder(state_root)
        created = seeder.publish_appearance(spine, entry)  # type: ignore[arg-type]
        pointer = state_root / "current" / "appearance"
        target = state_root / "cache" / "appearance" / entry.appearance_hash  # type: ignore[attr-defined]
        assert pointer.is_symlink()
        assert pointer.resolve() == target.resolve()
        assert pointer.readlink() == target
        for config_dir in ("ags", "ags-clipboard"):
            link = spine / "config" / config_dir / "appearance.css"
            assert link.is_symlink()
            assert link.readlink() == pointer / f"{config_dir}.css"
            assert link.resolve() == target / f"{config_dir}.css"
        assert len(created) == 3

    def test_v1_skin_pointers_are_pruned_while_publishing_appearance(
        self, tmp_path: Path
    ) -> None:
        entry, _, state_root = _generate(tmp_path)
        spine = _spine(tmp_path)
        seeder = CacheSeeder(state_root)
        (state_root / "current").mkdir(parents=True, exist_ok=True)
        (state_root / "current" / "skins").mkdir(exist_ok=True)
        (state_root / "current" / "skins" / "ags.css").symlink_to(
            cache_entry_path(state_root, "skins", SH) / "ags.css"
        )
        (state_root / "current" / "skin.json").symlink_to(
            cache_entry_path(state_root, "skins", SH) / "skin.json"
        )
        (spine / "config" / "ags" / "skin.css").symlink_to(
            state_root / "current" / "skins" / "ags.css"
        )
        (spine / "config" / "ags" / "skin.json").symlink_to(
            state_root / "current" / "skin.json"
        )
        seeder.publish_appearance(spine, entry)  # type: ignore[arg-type]
        assert not (spine / "config" / "ags" / "skin.css").is_symlink()
        assert not (spine / "config" / "ags" / "skin.json").is_symlink()
        assert (spine / "config" / "ags" / "appearance.css").is_symlink()

    def test_prune_stale_appearance_entries_and_links(self, tmp_path: Path) -> None:
        entry, _, state_root = _generate(tmp_path)
        spine = _spine(tmp_path)
        seeder = CacheSeeder(state_root)
        seeder.publish_appearance(spine, entry)  # type: ignore[arg-type]
        root = state_root / "cache" / "appearance"
        old = root / ("f" * 64)
        old.mkdir()
        (old / "appearance.json").write_text("{}", encoding="utf-8")
        os.utime(old, (1, 1))
        removed = prune_appearance_entries(state_root, keep=1, grace_seconds=0)
        assert old in removed
        assert (root / entry.appearance_hash).is_dir()  # type: ignore[attr-defined]
        stale = spine / "config" / "ags" / "appearance.css"
        stale.unlink()
        (spine / "config" / "ags-other").mkdir()
        stale_other = spine / "config" / "ags-other" / "appearance.css"
        stale_other.symlink_to(state_root / "current" / "appearance" / "ags-other.css")
        user = spine / "config" / "ags-other" / "notes.txt"
        user.write_text("keep\n", encoding="utf-8")
        seeder.publish_appearance(spine, entry)  # type: ignore[arg-type]
        assert not stale_other.is_symlink()
        assert user.read_text(encoding="utf-8") == "keep\n"
        assert stale.is_symlink()

    def test_non_ags_stable_link_survives_publish_and_converge_prune(
        self, tmp_path: Path
    ) -> None:
        entry, _, state_root = _generate(tmp_path)
        spine = _spine(tmp_path)
        seeder = CacheSeeder(state_root)
        seeder.publish_appearance(spine, entry)  # type: ignore[arg-type]
        cache_dir = state_root / "cache" / "appearance" / entry.appearance_hash  # type: ignore[attr-defined]
        target = cache_dir / "wlogout.css"
        target.write_text("/* wlogout */\n", encoding="utf-8")
        pointer = state_root / "current" / "appearance"
        wlogout = spine / "config" / "wlogout"
        wlogout.mkdir()
        stable = wlogout / "appearance.css"
        stable.symlink_to(pointer / "wlogout.css")

        assert stable.resolve() == target.resolve()

        # A second publication (skin/mode reconverge) keeps the stable link.
        seeder.publish_appearance(spine, entry)  # type: ignore[arg-type]
        assert stable.is_symlink()
        assert stable.readlink() == pointer / "wlogout.css"

        # Even if wlogout is part of the published skin target set, it is not
        # an AGS runtime consumer and its provisioning-owned link is kept.
        seeder.prune_runtime_spine_pointers(
            spine, published=(*entry.config_dirs, "wlogout")  # type: ignore[attr-defined]
        )
        assert stable.is_symlink()
        assert stable.readlink() == pointer / "wlogout.css"

    def test_ags_stale_link_is_pruned_while_non_ags_and_non_current_links_survive(
        self, tmp_path: Path
    ) -> None:
        spine = _spine(tmp_path)
        state_root = tmp_path / "state"
        appearance_dir = state_root / "current" / "appearance"
        appearance_dir.mkdir(parents=True)
        for name in ("ags-stale", "wlogout", "ags-old"):
            target = appearance_dir / f"{name}.css"
            target.write_text("/* link */\n", encoding="utf-8")
            directory = spine / "config" / name
            directory.mkdir()
            (directory / "appearance.css").symlink_to(target)
        legacy_target = state_root / "cache" / "appearance" / ("a" * 64) / "ags-old.css"
        legacy_target.parent.mkdir(parents=True)
        legacy_target.write_text("/* old */\n", encoding="utf-8")
        (spine / "config" / "ags-old" / "appearance.css").unlink()
        (spine / "config" / "ags-old" / "appearance.css").symlink_to(legacy_target)

        removed = prune_stale_appearance_consumers(
            spine,
            set(),
            published=("ags", "ags-stale", "wlogout", "ags-old"),
        )

        assert {path.parent.name for path in removed} == {"ags-stale"}
        assert not (spine / "config" / "ags-stale" / "appearance.css").is_symlink()
        assert (spine / "config" / "wlogout" / "appearance.css").is_symlink()
        # The narrow target check still preserves a non-current/appearance link.
        assert (spine / "config" / "ags-old" / "appearance.css").is_symlink()

    def test_symlink_config_dir_is_never_followed_by_the_prune(
        self, tmp_path: Path
    ) -> None:
        spine = _spine(tmp_path)
        state_root = tmp_path / "state"
        target = state_root / "current" / "appearance" / "ags-linked.css"
        target.parent.mkdir(parents=True)
        target.write_text("/* link */\n", encoding="utf-8")
        app_tag = tmp_path / "apps" / "bar" / "v1"
        app_tag.mkdir(parents=True)
        app_link = app_tag / "appearance.css"
        app_link.symlink_to(target)
        (spine / "config" / "ags-linked").symlink_to(app_tag, target_is_directory=True)

        removed = prune_stale_appearance_consumers(
            spine,
            set(),
            published=("ags-linked",),
        )

        assert removed == []
        assert app_link.is_symlink()


def _state(state_root: Path) -> DesktopState:
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=WH,
            source_path="/img/wall.png",
            imported_at=TS,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=WH,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=PH,
            source_wallpaper_hash=WH,
            input_template_hash="t" * 64,
            artifact_hashes={},  # type: ignore[typeddict-item]
            generated_at=TS,
        ),
        effects=None,
        icons=None,
        applied_at=TS,
        skin=_write_skin(state_root),
    )


class TestReconcileInspectDoctor:
    def test_reconcile_publishes_appearance_in_the_same_flow(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        spine = _spine(tmp_path)
        _write_palette(state_root)
        _write_skin(state_root)
        _write_wallpaper_cache(state_root)
        JsonStateRepository(state_root=state_root).save(_state(state_root))
        result = ReconcileDesktopStateUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            palette_producer=cast("object", object()),  # type: ignore[arg-type]
            weg=cast("object", object()),  # type: ignore[arg-type]
            itr=cast("object", object()),  # type: ignore[arg-type]
            install_spine=spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_NoLock(),
            skin_name="glass",
            skin_expander=SkinExpander(),
        ).run()
        assert result.appearance_hash == appearance_generation_hash(PH, SH, "dark")
        assert result.appearance_skin == "glass" and result.appearance_mode == "dark"
        assert result.generation_hash is not None
        generation = state_root / "generations" / result.generation_hash
        assert (generation / "manifest.json").is_file()
        assert (generation / "appearance" / "ags.css").is_file()
        assert (generation / "appearance" / "tokens.json").is_file()
        assert (state_root / "current").is_symlink()
        # v3 locality: no runtime convenience link in the install spine.
        assert not (spine / "config" / "ags" / "appearance.css").exists()
        assert not (spine / "config" / "ags" / "skin.css").exists()
        assert not (spine / "config" / "ags" / "skin.json").exists()

    def test_inspect_reports_appearance_and_doctor_has_no_skew(
        self, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state"
        spine = _spine(tmp_path)
        _write_palette(state_root)
        _write_skin(state_root)
        _write_wallpaper_cache(state_root)
        JsonStateRepository(state_root=state_root).save(_state(state_root))
        ReconcileDesktopStateUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            palette_producer=cast("object", object()),  # type: ignore[arg-type]
            weg=cast("object", object()),  # type: ignore[arg-type]
            itr=cast("object", object()),  # type: ignore[arg-type]
            install_spine=spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_NoLock(),
            skin_name="glass",
            skin_expander=SkinExpander(),
        ).run()

        inspected = InspectStateUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            install_spine=spine,
            consumer_spec=StaticConsumerPathSpec(),
        ).run()
        from runtime.adapters.generation_store import GenerationStore

        generation_hash = GenerationStore(state_root).current_hash()
        assert inspected.appearance_hash == generation_hash
        assert inspected.appearance_skin == "glass"
        assert inspected.appearance_mode == "dark"
        assert inspected.appearance_pointer is not None
        assert inspected.appearance_pointer.status == "ok"
        assert inspected.appearance_watch in ("ok", "unknown")
        assert "config/ags/appearance.css" not in inspected.consumer_pointers
        assert "config/ags/skin.css" not in inspected.consumer_pointers
        assert "config/ags/skin.json" not in inspected.consumer_pointers

        report = DoctorUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            install_spine=spine,
        ).check()
        by_name = {item.name: item for item in report.items}
        assert by_name["generation:identity"].status == "ok"
        assert by_name["appearance:watch"].status == "ok"
        assert "appearance:v1v2-skew" not in by_name

        # A runtime-owned v1 pointer reappearing after the cutover is real
        # drift, never the old expected-migration warning.
        (spine / "config" / "ags" / "skin.css").symlink_to(
            state_root / "current" / "skins" / "ags.css"
        )
        reappeared = DoctorUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            install_spine=spine,
        ).check()
        skew = {
            item.name: item for item in reappeared.items
        }["consumer/config/ags/skin.css"]
        assert skew.status == "diverged"
        assert "retired v1 skin pointer" in skew.detail

    def test_stale_generation_is_diverged_against_the_state_identity(
        self, tmp_path: Path
    ) -> None:
        from runtime.adapters.generation_store import (
            GenerationSpec,
            GenerationStore,
            generation_intent_hash,
        )

        state_root = tmp_path / "state"
        spine = _spine(tmp_path)
        _write_palette(state_root, mode="dark")
        skin = _write_skin(state_root)
        _write_wallpaper_cache(state_root)
        state = _state(state_root)
        JsonStateRepository(state_root=state_root).save(state)
        entry, _, _ = _generate(tmp_path, mode="dark")
        assert entry is not None
        intent_hash = generation_intent_hash(
            wallpaper=state.wallpaper.content_hash,
            palette=state.palette.entry_hash,  # type: ignore[union-attr]
            skin=skin.name,
            mode="dark",
        )
        GenerationStore(state_root).commit(
            GenerationSpec(
                state=state,
                appearance=entry,  # type: ignore[arg-type]
                mode="dark",
                skin_name=skin.name,
                skin_hash=skin.entry_hash,
                intent_hash=intent_hash,
                monitor_names=("DP-1",),
                source_kind="cli",
            )
        )

        # The palette now projects light mode, but the committed generation
        # still records dark: the manifest-vs-state identity must diverge.
        _write_palette(state_root, mode="light")
        inspected = InspectStateUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            install_spine=spine,
            consumer_spec=StaticConsumerPathSpec(),
        ).run()
        assert inspected.appearance_hash == GenerationStore(state_root).current_hash()
        assert inspected.appearance_pointer is not None
        assert inspected.appearance_pointer.status == "ok"
        report = DoctorUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            install_spine=spine,
        ).check()
        by_name = {item.name: item for item in report.items}
        assert by_name["generation:identity"].status == "diverged"

    def test_read_current_appearance_is_pointer_relative(self, tmp_path: Path) -> None:
        entry, _, state_root = _generate(tmp_path)
        CacheSeeder(state_root).publish_appearance(
            _spine(tmp_path), entry  # type: ignore[arg-type]
        )
        loaded = read_current_appearance(state_root)
        assert loaded is not None
        assert loaded.appearance_hash == entry.appearance_hash  # type: ignore[attr-defined]
        assert load_appearance_entry(
            state_root / "cache" / "appearance" / entry.appearance_hash  # type: ignore[attr-defined]
        ) == loaded


class _NoLock:
    def hold(self, blocking: bool = True) -> "_NoLock":
        return self

    def __enter__(self) -> "_NoLock":
        return self

    def __exit__(self, *exc: object) -> None:
        return None
