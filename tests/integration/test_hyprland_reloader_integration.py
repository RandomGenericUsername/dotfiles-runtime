"""Integration test for Hyprland reload adapter (Story 2.3)."""

from __future__ import annotations

import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.hyprland_reloader import HyprlandReloader
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.application.reconcile import ReconcileDesktopStateUseCase
from runtime.domain.models import PaletteArtifacts, PaletteEntry


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeMutex:
    def hold(self, blocking: bool = False) -> object:  # type: ignore[no-untyped-def]
        class _Hold:
            def __enter__(self_inner) -> None:
                return None

            def __exit__(self_inner, *exc: object) -> None:
                pass

        return _Hold()


class _FakeCsg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
        return PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml=hash_file(output_dir / "colors.yaml"),
                colors_conf=hash_file(output_dir / "colors.conf"),
                colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "effect.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        effect_hash = hash_file(output_dir / "effect.png")
        return EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash=output_dir.name,
            source_wallpaper_hash=hash_file(wallpaper_path),
            input_catalog_hash="2" * 64,
            artifact_hashes=EffectsArtifacts(**{"effect.png": effect_hash}),
            generated_at=_now_z(),
        )


class _FakeItr:
    def render(
        self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
    ) -> object:
        from runtime.domain.models import IconsArtifacts, IconsEntry

        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        return IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash=output_dir.name,
            source_palette_hash=palette_hash,
            input_templates_hash="5" * 64,
            input_mappings_hash="6" * 64,
            artifact_hashes=IconsArtifacts(**{"icon.svg": hash_file(output_dir / "icon.svg")}),
            generated_at=_now_z(),
        )


def _setup_spine(install_spine: Path) -> None:
    csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    csg_templates.mkdir(parents=True)
    (csg_templates / "default.yaml").write_text("window: {}\n")
    weg_cfg = install_spine / "config" / "weg"
    weg_cfg.mkdir(parents=True)
    (weg_cfg / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "config" / "icon-templates-renderer" / "templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "config" / "icon-templates-renderer" / "icons.yaml").write_text("icons: {}\n")


def test_hyprland_reloader_integration_with_real_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: seed → apply → reconcile with real HyprlandReloader.

    If ``hyprctl`` is not naturally present, inject a fake shim that
    exits 0 so the adapter's success path is exercised. When a real
    ``hyprctl`` exists but Hyprland is not running, the adapter would
    return False — the fake shim guarantees determinism.
    """
    # Create a fake hyprctl shim that exits 0 and logs calls
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    shim = bin_dir / "hyprctl"
    shim.write_text('#!/bin/sh\nif [ "$1" = "reload" ]; then exit 0; fi\nexit 1\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Verify shim is discoverable
    assert shutil.which("hyprctl") is not None, "fake hyprctl not in PATH"

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"e2e wallpaper for hyprland reload")

    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    ApplyWallpaperUseCase(
        state_repo=repo,
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
    ).run(img)

    reloader = HyprlandReloader()
    use_case = ReconcileDesktopStateUseCase(
        state_repo=repo,
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
        reloaders=[reloader],
    )
    result = use_case.run()
    assert result.reload_failures == []


def test_hyprland_reloader_integration_skip_when_no_binary(tmp_path: Path) -> None:
    """If no hyprctl and no shim, the adapter returns False without crashing."""
    # Ensure PATH does not contain hyprctl for this isolated check
    # Use an explicit None path reloader which always returns False
    reloader = HyprlandReloader(hyprctl_path=None)
    reloader._hyprctl_path = None  # type: ignore[attr-defined]
    assert reloader.reload() is False
