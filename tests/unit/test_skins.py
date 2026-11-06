"""Skins derivation layer: identity, cache, fallback, pointers, appearance.

Covers the frozen v1 requirements: palette-independent cache identity, template
and manifest misses, fail-closed fallback to the previous skin, atomic current
pointers, appearance-hash participation and the ``glass`` default.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import cast

import pytest

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
from runtime.adapters.appearance_ledger import appearance_hash_for_state
from runtime.adapters.desired_state_reader import read_desired_state, resolve_desired_path
from runtime.adapters.hashing import (
    appearance_hash,
    canonical_hash_dir,
    hash_file,
    skin_entry_hash,
)
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.adapters.skin_expander import SkinExpander
from runtime.application.derive import DerivationPipeline
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
from runtime.ports.skin_expander import ISkinExpander
from tests.support.palette_fakes import v1_colors_yaml, v1_gtk_css

WH = __import__("hashlib").sha256(b"wallpaper").hexdigest()
PH = "aa" * 32
TS = "2026-11-05T00:00:00Z"
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


class _Unused:
    """Stand-ins for palette/effects/icon producers (never invoked by skin)."""


class _CountingExpander(ISkinExpander):
    def __init__(self) -> None:
        self._inner = SkinExpander()
        self.calls = 0

    def expand(
        self,
        name: str,
        manifest_path: Path,
        templates_dir: Path,
        output_dir: Path,
    ) -> SkinEntry:
        self.calls += 1
        return self._inner.expand(name, manifest_path, templates_dir, output_dir)


def _write_skin_assets(
    spine: Path,
    *,
    name: str = "glass",
    body: str = ".panel { border-radius: {{SKIN_RADIUS_PANEL}}; }\n",
    radius: str = "12",
    config_dirs: tuple[str, ...] = ("ags",),
) -> tuple[Path, Path]:
    """Write the canonical assets layout: skins/manifests + skins/templates."""
    templates = spine / "skins" / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    for config_dir in config_dirs:
        (templates / f"{config_dir}.gtk.css.j2").write_text(body, encoding="utf-8")
    manifests = spine / "skins" / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest = manifests / f"{name}.toml"
    manifest.write_text(
        f'schema_major = 1\nname = "{name}"\n'
        "[shape]\n"
        f"radius-panel = {radius}\n"
        "radius-card = 9\n"
        "radius-cell = 7\n"
        "radius-pill = 20\n"
        "border-w = 1.5\n"
        "hairline-w = 1\n"
        "[rhythm]\n"
        "gap = 8\n"
        'cell-pad = "8px 9px"\n'
        "[surface]\n"
        'bg-panel = "alpha(@surface, 0.94)"\n'
        'bg-card = "alpha(@surface_container_high, 0.18)"\n'
        'border = "@primary"\n'
        'shadow = "none"\n'
        'blur = "none"\n'
        "[ink]\n"
        'ink = "@on_surface"\n'
        'ink-dim = "alpha(@on_surface, 0.62)"\n'
        'accent = "@primary"\n'
        'ink-over = "auto"\n'
        "[type]\n"
        'font-family = "inherit"\n'
        "title-size = 12.5\n"
        "text-size = 13\n"
        "[layout]\n"
        "panel-width = 312\n"
        "bt-list-spacing = 4\n"
        "bar-row-height = 0\n"
        "chip-min = 0\n",
        encoding="utf-8",
    )
    return manifest, templates


def _pipeline(tmp_path: Path, spine: Path, expander: ISkinExpander) -> DerivationPipeline:
    return DerivationPipeline(
        state_root=tmp_path / "state",
        seeder=CacheSeeder(tmp_path / "state"),
        palette_producer=cast("object", _Unused()),  # type: ignore[arg-type]
        weg=cast("object", _Unused()),  # type: ignore[arg-type]
        itr=cast("object", _Unused()),  # type: ignore[arg-type]
        install_spine=spine,
        skins=expander,
    )


def _write_palette_cache(state_root: Path) -> None:
    entry = cache_entry_path(state_root, "palettes", PH)
    entry.mkdir(parents=True)
    data = {name: name.encode() for name in PALETTE_FILES}
    data["colors.yaml"] = v1_colors_yaml().encode()
    data["colors.gtk.css"] = v1_gtk_css().encode()
    hashes = {}
    for name, raw in data.items():
        (entry / name).write_bytes(raw)
        import hashlib

        hashes[name] = hashlib.sha256(raw).hexdigest()
    (entry / "meta.json").write_text(
        json.dumps(
            {
                "hash_algorithm": "sha256",
                "kind": "palette",
                "entry_hash": PH,
                "source_wallpaper_hash": WH,
                "input_template_hash": "t" * 64,
                "artifact_hashes": hashes,
                "generated_at": TS,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _write_wallpaper_cache(state_root: Path) -> Path:
    entry = cache_entry_path(state_root, "wallpapers", WH)
    entry.mkdir(parents=True, exist_ok=True)
    image = entry / "wallpaper.png"
    image.write_bytes(b"wallpaper")
    return image


def _desktop_state(skin: SkinEntry | None = None) -> DesktopState:
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
            artifact_hashes={},
            generated_at=TS,
        ),
        effects=None,
        icons=None,
        applied_at=TS,
        skin=skin,
    )


def _reconcile(tmp_path: Path, spine: Path, expander: ISkinExpander) -> ReconcileDesktopStateUseCase:
    state_root = tmp_path / "state"
    return ReconcileDesktopStateUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        palette_producer=cast("object", _Unused()),  # type: ignore[arg-type]
        weg=cast("object", _Unused()),  # type: ignore[arg-type]
        itr=cast("object", _Unused()),  # type: ignore[arg-type]
        install_spine=spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_NoLock(),
        skin_name="glass",
        skin_expander=expander,
    )


class _NoLock:
    def hold(self, blocking: bool = True) -> "_NoLock":
        return self

    def __enter__(self) -> "_NoLock":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _make_reconcile_world(
    tmp_path: Path,
    *,
    config_dirs: tuple[str, ...] = ("ags",),
    installed_config_dirs: tuple[str, ...] = ("ags",),
) -> tuple[Path, Path, _CountingExpander]:
    spine = tmp_path / "install"
    _write_skin_assets(spine, config_dirs=config_dirs)
    for config_dir in installed_config_dirs:
        (spine / "config" / config_dir).mkdir(parents=True, exist_ok=True)
    state_root = tmp_path / "state"
    _write_palette_cache(state_root)
    _write_wallpaper_cache(state_root)
    JsonStateRepository(state_root=state_root).save(_desktop_state())
    return spine, state_root, _CountingExpander()


def test_skin_cache_hit_when_only_wallpaper_or_palette_changes(tmp_path: Path) -> None:
    spine = tmp_path / "install"
    _write_skin_assets(spine)
    expander = _CountingExpander()
    pipeline = _pipeline(tmp_path, spine, expander)

    first, hit = pipeline.ensure_skin("glass")
    assert hit is False and expander.calls == 1
    second, hit = pipeline.ensure_skin("glass")
    assert hit is True and expander.calls == 1
    assert second.entry_hash == first.entry_hash

    # Unrelated layers churn; the skin key has no wallpaper/palette input.
    other_wall = cache_entry_path(tmp_path / "state", "wallpapers", "1" * 64)
    other_wall.mkdir(parents=True)
    (other_wall / "wallpaper.png").write_bytes(b"different")
    other_pal = cache_entry_path(tmp_path / "state", "palettes", "2" * 64)
    other_pal.mkdir(parents=True)
    (other_pal / "colors.yaml").write_text("x", encoding="utf-8")

    third, hit = pipeline.ensure_skin("glass")
    assert hit is True and expander.calls == 1
    assert third.entry_hash == first.entry_hash


def test_skin_cache_miss_on_template_and_manifest_change(tmp_path: Path) -> None:
    spine = tmp_path / "install"
    manifest, templates = _write_skin_assets(spine)
    expander = _CountingExpander()
    pipeline = _pipeline(tmp_path, spine, expander)

    first, hit = pipeline.ensure_skin("glass")
    assert hit is False and expander.calls == 1

    (templates / "ags.gtk.css.j2").write_text(
        ".panel { border-radius: {{SKIN_RADIUS_PANEL}}; padding: 1px; }\n",
        encoding="utf-8",
    )
    after_template, hit = pipeline.ensure_skin("glass")
    assert hit is False and expander.calls == 2
    assert after_template.entry_hash != first.entry_hash
    assert after_template.input_templates_hash != first.input_templates_hash

    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "radius-panel = 12", "radius-panel = 16"
        ),
        encoding="utf-8",
    )
    after_manifest, hit = pipeline.ensure_skin("glass")
    assert hit is False and expander.calls == 3
    assert after_manifest.entry_hash != after_template.entry_hash
    assert after_manifest.input_manifest_hash != after_template.input_manifest_hash


def test_invalid_manifest_keeps_previous_skin_and_never_touches_css(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spine, state_root, expander = _make_reconcile_world(tmp_path)
    reconcile = _reconcile(tmp_path, spine, expander)

    first = reconcile.run()
    assert first.state.skin is not None
    skin_hash = first.state.skin.entry_hash
    cache_css = cache_entry_path(state_root, "skins", skin_hash) / "ags.css"
    css_before = cache_css.read_bytes()

    manifest = spine / "skins" / "manifests" / "glass.toml"
    manifest.write_text("schema_major = [not valid", encoding="utf-8")
    with caplog.at_level("ERROR"):
        second = reconcile.run()

    assert second.state.skin is not None
    assert second.state.skin.entry_hash == skin_hash
    assert cache_css.read_bytes() == css_before
    assert not (state_root / "current" / "skins").exists()
    assert not (spine / "config" / "ags" / "skin.css").exists()
    assert any("skin derivation failed" in record.getMessage() for record in caplog.records)


def test_current_skin_links_are_not_published(tmp_path: Path) -> None:
    spine, state_root, _ = _make_reconcile_world(tmp_path)
    seeder = CacheSeeder(state_root)
    image = _write_wallpaper_cache(state_root)

    seeder.repoint_current_symlinks(
        wallpaper_target=image,
        monitor_names=["DP-1"],
    )

    assert not (state_root / "current" / "skins").exists()
    assert not (state_root / "current" / "skin.json").exists()
    assert not (spine / "config" / "ags" / "skin.css").exists()
    assert not (spine / "config" / "ags" / "skin.json").exists()
    assert list((state_root / "current").rglob("*.tmp.*")) == []


def test_appearance_hash_changes_with_skin_hash() -> None:
    base = appearance_hash(
        palette_hash=PH,
        effects_hash=None,
        icons_hash=None,
        icon_templates_hash=None,
        icon_mappings_hash=None,
        skins_hash="a" * 64,
    )
    other = appearance_hash(
        palette_hash=PH,
        effects_hash=None,
        icons_hash=None,
        icon_templates_hash=None,
        icon_mappings_hash=None,
        skins_hash="b" * 64,
    )
    assert base != other


def test_appearance_hash_for_state_includes_skin() -> None:
    plain = _desktop_state()
    skinned = _desktop_state(
        SkinEntry(
            hash_algorithm="sha256",
            kind="skin",
            entry_hash="a" * 64,
            name="glass",
            input_templates_hash="b" * 64,
            input_manifest_hash="c" * 64,
            artifact_hashes=cast("SkinArtifacts", {"ags.css": "d" * 64, "skin.json": "e" * 64}),
            generated_at=TS,
        )
    )
    assert appearance_hash_for_state(plain) != appearance_hash_for_state(skinned)


def test_desired_state_defaults_to_glass(tmp_path: Path) -> None:
    path = resolve_desired_path(tmp_path / "config")
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"version": 1, "wallpaper": "/img/w.png", "keep": 5, "pinned": []}),
        encoding="utf-8",
    )
    desired = read_desired_state(path)
    assert desired is not None and desired.skin == "glass"

    path.write_text(
        json.dumps(
            {"version": 1, "wallpaper": "/img/w.png", "keep": 5, "pinned": [], "skin": "flat"}
        ),
        encoding="utf-8",
    )
    desired = read_desired_state(path)
    assert desired is not None and desired.skin == "flat"

    path.write_text(
        json.dumps(
            {"version": 1, "wallpaper": "/img/w.png", "keep": 5, "pinned": [], "skin": "../x"}
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="skin"):
        read_desired_state(path)


def test_inspect_prints_resolved_skin_and_tokens(tmp_path: Path) -> None:
    spine, state_root, expander = _make_reconcile_world(tmp_path)
    result = _reconcile(tmp_path, spine, expander).run()

    inspected = InspectStateUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        install_spine=spine,
        consumer_spec=StaticConsumerPathSpec(),
    ).run()

    assert inspected.skin == result.state.skin.entry_hash  # type: ignore[union-attr]
    assert inspected.skin_name == "glass"
    assert inspected.skin_tokens == {
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
        "type": {
            "font-family": "inherit",
            "title-size": 12.5,
            "text-size": 13,
        },
        "rhythm": {"gap": 8, "cell-pad": "8px 9px"},
        "layout": {
            "panel-width": 312,
            "bt-list-spacing": 4,
            "bar-row-height": 0,
            "chip-min": 0,
        },
    }
    assert inspected.consumer_pointers["config/ags/colors.css"].status == "ok"
    assert "config/ags/skin.css" not in inspected.consumer_pointers
    assert "config/ags/skin.json" not in inspected.consumer_pointers


def test_v1_skin_pointers_are_retired_and_appearance_links_are_derived() -> None:
    spec = StaticConsumerPathSpec()
    assert all("skin" not in pointer.path for pointer in spec.consumer_pointers())
    assert not hasattr(spec, "skin_pointers")

    pointers = spec.appearance_pointers(["ags", "ags-clipboard", "custom-app"])
    by_path = {pointer.path: pointer.target for pointer in pointers}

    assert by_path == {
        "config/ags/appearance.css": "appearance/ags.css",
        "config/ags-clipboard/appearance.css": "appearance/ags-clipboard.css",
        "config/custom-app/appearance.css": "appearance/custom-app.css",
    }
    assert spec.appearance_pointers([]) == ()
    assert spec.appearance_pointers(["../escape"]) == ()


def test_every_appearance_target_gets_a_link_and_no_v1_pointer(tmp_path: Path) -> None:
    config_dirs = ("ags", "ags-clipboard", "ags-notifications")
    spine, state_root, expander = _make_reconcile_world(
        tmp_path,
        config_dirs=config_dirs,
        installed_config_dirs=config_dirs,
    )

    result = _reconcile(tmp_path, spine, expander).run()

    assert result.state.skin is not None
    for config_dir in config_dirs:
        appearance = spine / "config" / config_dir / "appearance.css"
        assert appearance.is_symlink()
        assert appearance.resolve() == (
            state_root / "current" / "appearance" / f"{config_dir}.css"
        ).resolve()
        assert not (spine / "config" / config_dir / "skin.css").exists()
        assert not (spine / "config" / config_dir / "skin.json").exists()
    assert not (state_root / "current" / "skins").exists()
    assert not (state_root / "current" / "skin.json").exists()


def test_absent_app_config_dir_is_skipped_not_created(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spine, state_root, expander = _make_reconcile_world(
        tmp_path,
        config_dirs=("ags", "ags-capture"),
        installed_config_dirs=("ags",),
    )

    with caplog.at_level(logging.DEBUG, logger="runtime.adapters.seeder"):
        _reconcile(tmp_path, spine, expander).run()

    assert (spine / "config" / "ags" / "appearance.css").is_symlink()
    assert not (spine / "config" / "ags" / "skin.css").exists()
    assert not (spine / "config" / "ags-capture").exists()
    assert any(
        "config dir absent" in record.getMessage()
        and "ags-capture" in record.getMessage()
        for record in caplog.records
    )


def test_inspect_and_doctor_report_appearance_not_v1_pointers(tmp_path: Path) -> None:
    config_dirs = ("ags", "ags-clipboard")
    spine, state_root, expander = _make_reconcile_world(
        tmp_path,
        config_dirs=(*config_dirs, "ags-icme"),
        installed_config_dirs=config_dirs,
    )
    _reconcile(tmp_path, spine, expander).run()

    inspected = InspectStateUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        install_spine=spine,
        consumer_spec=StaticConsumerPathSpec(),
    ).run()
    for config_dir in config_dirs:
        assert inspected.consumer_pointers[f"config/{config_dir}/appearance.css"].status == "ok"
        assert f"config/{config_dir}/skin.css" not in inspected.consumer_pointers
        assert f"config/{config_dir}/skin.json" not in inspected.consumer_pointers
    assert "config/ags-icme/skin.css" not in inspected.consumer_pointers

    report = DoctorUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        install_spine=spine,
    ).check()
    consumer = {
        item.name: item.status for item in report.items if item.kind == "consumer"
    }
    for config_dir in config_dirs:
        assert consumer[f"consumer/config/{config_dir}/appearance.css"] == "ok"
        assert f"consumer/config/{config_dir}/skin.css" not in consumer
    assert "consumer/config/ags-icme/skin.css" not in consumer
    assert "appearance:v1v2-skew" not in {item.name for item in report.items}


def test_retired_v1_skin_links_are_pruned_on_repoint(tmp_path: Path) -> None:
    spine = tmp_path / "install"
    for config_dir in ("ags", "ags-clipboard"):
        (spine / "config" / config_dir).mkdir(parents=True)
    state_root = tmp_path / "state"
    current = state_root / "current"
    (current / "skins").mkdir(parents=True)
    (current / "skins" / "ags.css").write_text("/* v1 */\n", encoding="utf-8")
    (current / "skin.json").write_text("{}\n", encoding="utf-8")
    for config_dir in ("ags", "ags-clipboard"):
        (spine / "config" / config_dir / "skin.css").symlink_to(
            current / "skins" / f"{config_dir}.css"
        )
        (spine / "config" / config_dir / "skin.json").symlink_to(current / "skin.json")
    appearance = spine / "config" / "ags" / "appearance.css"
    appearance.symlink_to(current / "appearance" / "ags.css")
    user_file = spine / "config" / "ags-clipboard" / "notes.txt"
    user_file.write_text("user-owned\n", encoding="utf-8")
    user_link = spine / "config" / "ags-clipboard" / "skin.css.user"
    user_link.symlink_to(tmp_path / "elsewhere.css")

    removed = CacheSeeder(state_root).repoint_consumer_symlinks(spine, PH)

    for config_dir in ("ags", "ags-clipboard"):
        assert not (spine / "config" / config_dir / "skin.css").is_symlink()
        assert not (spine / "config" / config_dir / "skin.json").is_symlink()
    assert appearance.is_symlink()
    assert user_file.read_text(encoding="utf-8") == "user-owned\n"
    assert user_link.is_symlink()
    assert set(removed) == {
        spine / "config" / "ags" / "skin.css",
        spine / "config" / "ags" / "skin.json",
        spine / "config" / "ags-clipboard" / "skin.css",
        spine / "config" / "ags-clipboard" / "skin.json",
    }


def test_stale_v1_links_pruned_but_palette_and_user_files_kept(
    tmp_path: Path,
) -> None:
    spine = tmp_path / "install"
    for config_dir in ("ags", "ags-clipboard"):
        (spine / "config" / config_dir).mkdir(parents=True)
    state_root = tmp_path / "state"
    current = state_root / "current"
    current.mkdir(parents=True)
    for name in ("colors.gtk.css", "colors.adw.css", "colors.rasi", "colors.btop.theme"):
        (current / name).write_text(f"/* {name} */\n", encoding="utf-8")
    seeder = CacheSeeder(state_root)

    seeder.repoint_consumer_symlinks(spine, PH)
    ags_colors_before = (spine / "config" / "ags" / "colors.css").readlink()
    for config_dir in ("ags", "ags-clipboard"):
        (spine / "config" / config_dir / "skin.css").symlink_to(
            current / "skins" / f"{config_dir}.css"
        )
        (spine / "config" / config_dir / "skin.json").symlink_to(current / "skin.json")
    user_file = spine / "config" / "ags-clipboard" / "notes.txt"
    user_file.write_text("user-owned\n", encoding="utf-8")
    user_link = spine / "config" / "ags-clipboard" / "skin.css.user"
    user_link.symlink_to(tmp_path / "elsewhere.css")

    seeder.repoint_consumer_symlinks(spine, PH)

    for config_dir in ("ags", "ags-clipboard"):
        assert not (spine / "config" / config_dir / "skin.css").is_symlink()
        assert not (spine / "config" / config_dir / "skin.json").is_symlink()
    assert (spine / "config" / "ags" / "colors.css").readlink() == ags_colors_before
    assert user_file.read_text(encoding="utf-8") == "user-owned\n"
    assert user_link.is_symlink()


def test_retired_v1_links_are_pruned_through_a_symlinked_config_dir(
    tmp_path: Path,
) -> None:
    """The live spine exposes ``config/<configDir>`` as a symlink to the app."""
    spine = tmp_path / "install"
    app = spine / "apps" / "bar" / "v0.1.0"
    app.mkdir(parents=True)
    (spine / "config").mkdir(parents=True)
    link = spine / "config" / "ags"
    link.symlink_to(app, target_is_directory=True)
    state_root = tmp_path / "state"
    current = state_root / "current"
    (current / "skins").mkdir(parents=True)
    (current / "skin.json").write_text("{}\n", encoding="utf-8")
    (app / "skin.css").symlink_to(current / "skins" / "ags.css")
    (app / "skin.json").symlink_to(current / "skin.json")
    appearance = app / "appearance.css"
    appearance.symlink_to(current / "appearance" / "ags.css")
    user_file = app / "notes.txt"
    user_file.write_text("user-owned\n", encoding="utf-8")

    removed = CacheSeeder(state_root).prune_v1_skin_pointers(spine)

    assert set(removed) == {link / "skin.css", link / "skin.json"}
    assert not (app / "skin.css").is_symlink()
    assert not (app / "skin.json").is_symlink()
    assert appearance.is_symlink()
    assert user_file.read_text(encoding="utf-8") == "user-owned\n"


def test_custom_app_gets_no_v1_pointer(tmp_path: Path) -> None:
    spine = tmp_path / "install"
    (spine / "config" / "ags").mkdir(parents=True)
    (spine / "config" / "custom-app").mkdir(parents=True)
    state_root = tmp_path / "state"

    CacheSeeder(state_root).repoint_consumer_symlinks(spine, PH)

    for config_dir in ("custom-app", "ags"):
        assert not (spine / "config" / config_dir / "skin.css").exists()
        assert not (spine / "config" / config_dir / "skin.json").exists()


def test_skin_entry_hash_ignores_wallpaper_and_palette() -> None:
    templates_hash = "a" * 64
    manifest_hash = "b" * 64
    assert skin_entry_hash(templates_hash, manifest_hash) == skin_entry_hash(
        templates_hash, manifest_hash
    )
    assert canonical_hash_dir(Path(__file__).parent) == canonical_hash_dir(
        Path(__file__).parent
    )
    assert hash_file(Path(__file__)) == hash_file(Path(__file__))
