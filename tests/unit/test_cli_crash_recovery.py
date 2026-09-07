"""CLI tests for crash recovery logging and reload failure placeholder (Story 2.2)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder, _repoint_symlink
from runtime.cli.main import app

runner = CliRunner()


def _now_z() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeMutex:
    def hold(self, blocking: bool = False) -> object:
        class _Hold:
            def __enter__(self) -> None:
                return None

            def __exit__(self, *exc: object) -> None:
                pass

        return _Hold()


class _FakeCsg:
    def __init__(self) -> None:
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "colors.yaml").write_text("colors: []")
        (output_dir / "colors.conf").write_text("colors {}")
        (output_dir / "colors.gtk.css").write_text("colors {}")
        (output_dir / "colors.adw.css").write_text("colors {}")
        (output_dir / "colors.sequences").write_bytes(b"\x1b]4;0;#000\x1b\\")
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
                colors_adw_css=hash_file(output_dir / "colors.adw.css"),
                colors_sequences=hash_file(output_dir / "colors.sequences"),
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
        from runtime.adapters.hashing import hash_file
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
    def render(self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path) -> object:
        from runtime.adapters.hashing import hash_file
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
    weg_config = install_spine / "config" / "weg"
    weg_config.mkdir(parents=True)
    (weg_config / "effects.yaml").write_text("effects: []\n")
    itr_templates = install_spine / "icon-templates"
    itr_templates.mkdir(parents=True)
    (itr_templates / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True, exist_ok=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")


def _prepare_state(tmp_path: Path, state_root: Path) -> Path:
    """Create install spine and state_root, apply wallpaper & reconcile so current/ exists."""
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    repo = JsonStateRepository(state_root=state_root)
    csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
    img = tmp_path / "wall.png"
    img.write_bytes(b"user wallpaper bytes")
    ApplyWallpaperUseCase(
        state_repo=repo,
        csg=csg,
        weg=weg,
        itr=itr,
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),
    ).run(img)
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    ReconcileDesktopStateUseCase(
        state_repo=repo,
        csg=csg,
        weg=weg,
        itr=itr,
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),
    ).run()
    return install_spine


class _PassingHyprpaperReloader:
    """Isolates the CLI from the real hyprctl/hyprpaper session on this host.

    The composition root wires a real ``HyprpaperReloader``, whose IPC
    invocation would reach the live desktop hyprpaper when a session is
    running; the crash-recovery tests must not touch it. The terminal
    palette applier (``_PassingTerminalColorApplier`` below) is isolated
    for the same reason: the real adapter would write OSC bytes to the
    dev host's live ``/dev/tty`` (recoloring the developer's actual
    terminal) or fail surfaced and flip ``reload_failures``.
    """

    def __init__(self, **_kwargs: object) -> None:
        pass

    def reload(self) -> bool:
        return True


class _PassingTerminalColorApplier:
    """Isolates the CLI from the real ``/dev/tty`` on this host."""

    def __init__(self, **_kwargs: object) -> None:
        pass

    def reload(self) -> bool:
        return True


class TestCliCrashRecoveryLogging:
    def test_reconcile_recovery_logs_stray_reverts(self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        state_root = tmp_path / "dotfiles"
        install_spine = _prepare_state(tmp_path, state_root)
        # corrupt one symlink
        repo = JsonStateRepository(state_root=state_root)
        loaded = repo.load_current()
        assert loaded is not None
        stale = loaded.wallpaper.content_hash[:-1] + ("0" if loaded.wallpaper.content_hash[-1] != "0" else "1")
        stale_entry = state_root / "cache" / "wallpapers" / stale
        stale_entry.mkdir(parents=True, exist_ok=True)
        (stale_entry / "wallpaper.png").write_bytes(b"stale")
        (stale_entry / "meta.json").write_text(json.dumps({"hash_algorithm": "sha256"}))
        _repoint_symlink(state_root / "current" / "wallpaper-DP-1.png", stale_entry / "wallpaper.png")

        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install_spine))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setattr(
            "runtime.adapters.hyprpaper_reloader.HyprpaperReloader",
            _PassingHyprpaperReloader,
        )
        monkeypatch.setattr(
            "runtime.adapters.terminal_color_applier.TerminalColorApplier",
            _PassingTerminalColorApplier,
        )

        with caplog.at_level(logging.INFO, logger="runtime.application.reconcile"):
            result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 0
        assert any("recovery: reverted" in r.message for r in caplog.records)

    def test_reconcile_no_stray_logs_debug(self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        state_root = tmp_path / "dotfiles"
        _prepare_state(tmp_path, state_root)
        # Ensure install spine env for second prepare
        install_spine = tmp_path / "install"
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install_spine))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.setattr(
            "runtime.adapters.hyprpaper_reloader.HyprpaperReloader",
            _PassingHyprpaperReloader,
        )
        monkeypatch.setattr(
            "runtime.adapters.terminal_color_applier.TerminalColorApplier",
            _PassingTerminalColorApplier,
        )

        with caplog.at_level(logging.DEBUG, logger="runtime.application.reconcile"):
            result = runner.invoke(app, ["reconcile"])

        assert result.exit_code == 0
        assert any("no stray symlinks detected" in r.message for r in caplog.records)

    @pytest.mark.skip(reason="reload adapters not yet implemented")
    def test_reconcile_reload_failure_exits_nonzero(self) -> None:
        pytest.skip("reload adapters not yet implemented")
