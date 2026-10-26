"""Required-palette integration proof (BUG-001).

Composes the REAL CLI composition root on a REAL filesystem (real
``JsonStateRepository``/``CacheSeeder``/spine, contract-honest fake
csg/weg/itr, fake wallpaper applier/reloaders) and proves the new
contract:

- a palette failure aborts BEFORE the visible swap: non-zero exit,
  ``current.json`` byte-identical, ``current/`` and spine consumer links
  untouched, no wallpaper apply, a traceable error event + failure record;
- a successful set still swaps and persists a real (non-null) palette.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from runtime.adapters.hashing import hash_file, palette_entry_hash
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.cli.main import app
from runtime.domain.models import (
    BackendType,
    DesktopState,
    FitMode,
    MonitorWallpaperConfig,
    PaletteArtifacts,
    PaletteEntry,
    WallpaperEntry,
)

runner = CliRunner()

CONSUMER_RELS = (
    "config/ags/colors.css",
    "config/gtk-3.0/colors.css",
    "config/gtk-4.0/colors.css",
    "config/rofi/colors.rasi",
    "config/btop/themes/colors.btop.theme",
)

PALETTE_ARTIFACTS = (
    "colors.yaml",
    "colors.conf",
    "colors.gtk.css",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
    "colors.kitty",
    "colors.btop.theme",
)


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _setup_spine(install_spine: Path) -> None:
    templates = install_spine / "config" / "color-scheme-generator" / "templates"
    templates.mkdir(parents=True)
    (templates / "default.yaml").write_text("window: {}\n")
    weg = install_spine / "config" / "weg"
    weg.mkdir(parents=True)
    (weg / "effects.yaml").write_text("effects: []\n")
    (install_spine / "icon-templates").mkdir(parents=True)
    (install_spine / "icon-templates" / "terminal.svg").write_text("<svg/>")
    (install_spine / "icon-mappings").mkdir(parents=True)
    (install_spine / "icon-mappings" / "icons.yaml").write_text("icons: {}\n")
    for rel in CONSUMER_RELS:
        (install_spine / rel).parent.mkdir(parents=True, exist_ok=True)


def _palette_entry(
    output_dir: Path, wallpaper_hash: str, *, templates_hash: str = "c" * 64
) -> PaletteEntry:
    return PaletteEntry(
        hash_algorithm="sha256",
        kind="palette",
        entry_hash=output_dir.name,
        source_wallpaper_hash=wallpaper_hash,
        input_template_hash=templates_hash,
        artifact_hashes=PaletteArtifacts(
            colors_yaml=hash_file(output_dir / "colors.yaml"),
            colors_conf=hash_file(output_dir / "colors.conf"),
            colors_gtk_css=hash_file(output_dir / "colors.gtk.css"),
            colors_adw_css=hash_file(output_dir / "colors.adw.css"),
            colors_sequences=hash_file(output_dir / "colors.sequences"),
            colors_rasi=hash_file(output_dir / "colors.rasi"),
            colors_kitty=hash_file(output_dir / "colors.kitty"),
            colors_btop=hash_file(output_dir / "colors.btop.theme"),
        ),
        generated_at=_now_z(),
    )


def _write_palette_artifacts(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in PALETTE_ARTIFACTS:
        (output_dir / name).write_text(f"{name} content")


class _FakeCsg:
    """Contract-honest csg fake: echoes the output dir name as entry hash."""

    def __init__(self, **kwargs: Any) -> None:
        pass

    def generate(self, wallpaper_path: Path, output_dir: Path) -> PaletteEntry:
        _write_palette_artifacts(output_dir)
        return _palette_entry(output_dir, hash_file(wallpaper_path))


class _FailingCsg:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def generate(self, wallpaper_path: Path, output_dir: Path) -> PaletteEntry:
        from runtime.domain.models import PaletteUnavailableError

        raise PaletteUnavailableError(
            "csg generate failed (exit 7): csg exploded",
            wallpaper_path=str(wallpaper_path),
            wallpaper_hash=hash_file(wallpaper_path),
            returncode=7,
            stderr="csg exploded",
        )


class _FakeWeg:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
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
    def __init__(self, **kwargs: Any) -> None:
        pass

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> Any:
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


class _RecordingApplier:
    def __init__(self) -> None:
        self.calls = 0

    def apply(self) -> bool:
        self.calls += 1
        return True

    def reload(self) -> bool:
        return self.apply()


class _RecordingClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, object]]] = []

    def publish(self, topic: str, payload: Any) -> None:
        self.published.append((topic, dict(payload)))


def _env(tmp_path: Path) -> tuple[Path, Path, Path]:
    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state-home" / "dotfiles"
    state_root.mkdir(parents=True, exist_ok=True)
    return state_root, install_spine, tmp_path / "state-home"


def _write_last_good_state(
    state_root: Path, install_spine: Path, old_img: Path
) -> tuple[str, dict[str, str], dict[str, str]]:
    """Write a real last-good state + current/ + spine consumer links."""
    old_hash = hash_file(old_img)
    old_wallpaper_dir = state_root / "cache" / "wallpapers" / old_hash
    old_wallpaper_dir.mkdir(parents=True)
    (old_wallpaper_dir / "wallpaper.png").write_bytes(old_img.read_bytes())

    palette_hash = "a" * 64
    palette_dir = state_root / "cache" / "palettes" / palette_hash
    _write_palette_artifacts(palette_dir)
    palette = _palette_entry(palette_dir, old_hash)

    now = _now_z()
    state = DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=old_hash,
            source_path=str(old_img),
            imported_at=now,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=old_hash,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=palette,
        effects=None,
        icons=None,
        applied_at=now,
    )
    JsonStateRepository(state_root=state_root).save(state)

    seeder = CacheSeeder(state_root)
    seeder.repoint_current_symlinks(
        wallpaper_target=old_wallpaper_dir / "wallpaper.png",
        monitor_names=["DP-1"],
        palette_entry_hash=palette_hash,
    )
    seeder.repoint_consumer_symlinks(install_spine, palette_hash)

    before_links = {
        path.name: os.readlink(path)
        for path in sorted((state_root / "current").iterdir())
        if path.is_symlink()
    }
    before_consumers = {
        rel: os.readlink(install_spine / rel)
        for rel in CONSUMER_RELS
        if (install_spine / rel).is_symlink()
    }
    return (state_root / "current.json").read_text(encoding="utf-8"), before_links, before_consumers


def _error_output(result: Any) -> str:
    return result.stderr if getattr(result, "stderr", "") else result.output


class TestPaletteFailureAbortsBeforeSwap:
    def test_palette_failure_leaves_last_good_state_and_links_untouched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root, install_spine, state_home = _env(tmp_path)
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install_spine))
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

        old_img = tmp_path / "old.png"
        old_img.write_bytes(b"last good wallpaper")
        before_state, before_links, before_consumers = _write_last_good_state(
            state_root, install_spine, old_img
        )

        img = tmp_path / "new.png"
        img.write_bytes(b"new wallpaper whose palette fails")
        expected_hash = hash_file(img)

        applier = _RecordingApplier()
        client = _RecordingClient()
        monkeypatch.setattr("runtime.adapters.csg_adapter.CsgAdapter", _FailingCsg)
        monkeypatch.setattr("runtime.adapters.weg_adapter.WegAdapter", _FakeWeg)
        monkeypatch.setattr("runtime.adapters.itr_adapter.ItrAdapter", _FakeItr)
        monkeypatch.setattr("runtime.cli.main._build_wallpaper_applier", lambda root: applier)
        monkeypatch.setattr("runtime.cli.main._build_reloaders", lambda *a, **k: [])
        monkeypatch.setattr("runtime.cli.main._build_wallpaper_client", lambda: client)

        result = runner.invoke(app, ["wallpaper", "set", str(img)])

        assert result.exit_code != 0
        assert (state_root / "current.json").read_text(encoding="utf-8") == before_state
        assert {
            path.name: os.readlink(path)
            for path in sorted((state_root / "current").iterdir())
            if path.is_symlink()
        } == before_links
        assert {
            rel: os.readlink(install_spine / rel)
            for rel in CONSUMER_RELS
            if (install_spine / rel).is_symlink()
        } == before_consumers
        assert applier.calls == 0
        assert not (state_root / "history.jsonl").exists()

        # Traceable error event: reason + path/hash/stage/tool/rc/stderr, no visible.
        assert [payload["state"] for _, payload in client.published] == ["applying", "error"]
        first = client.published[0][1]
        last = client.published[-1][1]
        assert first["stage"] == "generating_palette"
        assert last["stage"] == "generating_palette"
        assert last["wallpaper_hash"] == expected_hash
        assert "csg exploded" in str(last["error"])
        assert "exit 7" in str(last["error"])

        # Traceable failure record (history schema rejects extra fields, so
        # the runtime-owned failures.jsonl carries them).
        failures = (state_root / "failures.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(failures) == 1
        record = json.loads(failures[0])
        assert record["trigger"] == "set-failed"
        assert record["wallpaper_path"] == str(img.resolve())
        assert record["wallpaper_hash"] == expected_hash
        assert record["stage"] == "palette"
        assert record["tool"] == "csg"
        assert record["returncode"] == 7
        assert record["stderr"] == "csg exploded"

        # The CLI error is structured with the same traceability fields.
        text = _error_output(result) + result.output
        for needle in (
            "PaletteUnavailableError",
            str(img.resolve()),
            expected_hash,
            "palette",
            "csg",
            "7",
            "csg exploded",
        ):
            assert needle in text


class TestSuccessStillPersistsRealPalette:
    def test_success_swaps_and_persists_non_null_palette(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        state_root, install_spine, state_home = _env(tmp_path)
        monkeypatch.setenv("DOTFILES_INSTALL_SPINE", str(install_spine))
        monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

        img = tmp_path / "wall.png"
        img.write_bytes(b"successful wallpaper bytes")
        applier = _RecordingApplier()
        client = _RecordingClient()
        monkeypatch.setattr("runtime.adapters.csg_adapter.CsgAdapter", _FakeCsg)
        monkeypatch.setattr("runtime.adapters.weg_adapter.WegAdapter", _FakeWeg)
        monkeypatch.setattr("runtime.adapters.itr_adapter.ItrAdapter", _FakeItr)
        monkeypatch.setattr("runtime.cli.main._build_wallpaper_applier", lambda root: applier)
        monkeypatch.setattr("runtime.cli.main._build_reloaders", lambda *a, **k: [])
        monkeypatch.setattr("runtime.cli.main._build_wallpaper_client", lambda: client)

        result = runner.invoke(app, ["wallpaper", "set", str(img)])

        assert result.exit_code == 0, _error_output(result)
        loaded = JsonStateRepository(state_root=state_root).load_current()
        assert loaded is not None
        assert loaded.wallpaper.content_hash == hash_file(img)
        assert loaded.palette is not None
        expected_ph = palette_entry_hash(
            hash_file(img),
            _canonical_hash_dir(install_spine / "config" / "color-scheme-generator" / "templates"),
        )
        assert loaded.palette.entry_hash == expected_ph
        palette_dir = state_root / "cache" / "palettes" / expected_ph
        assert all((palette_dir / name).is_file() for name in PALETTE_ARTIFACTS)
        assert applier.calls == 1
        assert (state_root / "current" / "colors.gtk.css").resolve() == (
            palette_dir / "colors.gtk.css"
        )
        lines = (state_root / "history.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["trigger"] == "set"
        assert json.loads(lines[0])["palette"] == expected_ph


def _canonical_hash_dir(path: Path) -> str:
    from runtime.adapters.hashing import canonical_hash_dir

    return canonical_hash_dir(path)
