"""Integration test for AGS restart reload adapter (Story 2.4)."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runtime.adapters.ags_reloader import AgsReloader
from runtime.adapters.hashing import hash_file
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
    itr_templates = install_spine / "icon-templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")


def _make_applied(
    tmp_path: Path,
) -> tuple[JsonStateRepository, Path, Path]:
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"e2e wallpaper for ags restart")
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
    return repo, state_root, install_spine


def _make_ags_shim(tmp_path: Path, marker: Path) -> Path:
    """A fake ``ags`` that records its argv to ``marker``.

    ``quit`` exits 0 immediately; ``run`` sleeps LONGER than the adapter's
    liveness window (3s > 2s) so the liveness poll sees a live process.
    """
    shim = tmp_path / "ags"
    shim.write_text(
        "#!/bin/sh\n"
        f'echo "$1" >> "{marker}"\n'
        'case "$1" in\n'
        "  quit) exit 0 ;;\n"
        "  run) sleep 3; exit 0 ;;\n"
        "esac\n"
        "exit 1\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


def test_ags_reloader_integration_restart_with_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: seed → apply → reconcile with an ``ags`` shim, restart recorded.

    A fake ``ags`` shim records every invocation to a marker file and is
    injected via PATH. A real ``AgsReloader(ags_path=<shim>)`` drives the
    full swap sequence; the marker proves both ``quit`` and detached
    ``run`` were invoked and no reload failure was surfaced.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    marker = tmp_path / "ags-called"
    shim = _make_ags_shim(bin_dir, marker)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    repo, state_root, install_spine = _make_applied(tmp_path)
    reloader = AgsReloader(ags_path=shim)
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
    assert marker.is_file(), "ags shim was never invoked"
    assert marker.read_text().splitlines() == ["quit", "run"], (
        "expected quit then a detached run from the shim"
    )


def test_ags_reloader_integration_missing_binary_populates_reload_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No discoverable ``ags`` → surfaced failure, not a skip (R5).

    With ``shutil.which`` resolving to ``None``, ``AgsReloader()`` stores
    ``None``, ``reload()`` returns ``False``, and ``reconcile`` collects
    ``"AgsReloader"`` into ``reload_failures`` (surfaced failure — the CLI
    exits non-zero on this, matching the Hyprland decision).
    """
    monkeypatch.setattr("runtime.adapters.hyprland_reloader.shutil.which", lambda *_a, **_k: None)
    repo, state_root, install_spine = _make_applied(tmp_path)
    use_case = ReconcileDesktopStateUseCase(
        state_repo=repo,
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
        reloaders=[AgsReloader()],
    )
    result = use_case.run()
    assert "AgsReloader" in result.reload_failures
