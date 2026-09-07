"""Integration tests for ``inspect status`` — real repository + real symlinks
(Story 3.2).

rt-3.1 lesson: integration realism — a FRESH ``InspectStateUseCase`` reads a
tree built through the real ``JsonStateRepository.save`` + ``os.symlink``, so
the restart-survival path is exercised, not a fake. Covers the end-to-end
status projection (AC 1), live symlink reflection incl. divergence (AC 2),
absent-state failure (AC 3), and the read-only invariant (AC 4).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.application.inspect import InspectStateUseCase
from runtime.domain.models import (
    BackendType,
    DesktopState,
    EffectsEntry,
    FitMode,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteArtifacts,
    PaletteEntry,
    WallpaperEntry,
)


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _make_state() -> DesktopState:
    """Seeded-style full state with deterministic 'a'*64-style hashes."""
    wh = "a" * 64
    now = _now_z()
    return DesktopState(
        schema_version=2,
        wallpaper=WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wh,
            source_path="/img/wall.png",
            imported_at=now,
        ),
        monitors={
            "DP-1": MonitorWallpaperConfig(
                backend=BackendType.hyprpaper,
                source_hash=wh,
                fit_mode=FitMode.cover,
                mpv_options=None,
                ipc_socket=None,
            )
        },
        palette=PaletteEntry(
            hash_algorithm="sha256",
            kind="palette",
            entry_hash="b" * 64,
            source_wallpaper_hash=wh,
            input_template_hash="c" * 64,
            artifact_hashes=PaletteArtifacts(
                colors_yaml="d" * 64,
                colors_conf="e" * 64,
                colors_gtk_css="f" * 64,
                colors_adw_css="1" * 64,
                colors_sequences="2" * 64,
            ),
            generated_at=now,
        ),
        effects=EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash="1" * 64,
            source_wallpaper_hash=wh,
            input_catalog_hash="2" * 64,
            artifact_hashes={},
            generated_at=now,
        ),
        icons=IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash="3" * 64,
            source_palette_hash="b" * 64,
            input_templates_hash="4" * 64,
            input_mappings_hash="5" * 64,
            artifact_hashes={},
            generated_at=now,
        ),
        applied_at=now,
    )


def _build_live_tree(state_root: Path, state: DesktopState) -> dict[str, Path]:
    """Persist current.json via the real repo, create real cache-entry
    files/dirs, and wire real current/ symlinks (seed-style layout)."""
    JsonStateRepository(state_root=state_root).save(state)

    targets: dict[str, Path] = {
        f"wallpaper-{m}.png": (
            cache_entry_path(state_root, "wallpapers", state.wallpaper.content_hash)
            / "wallpaper.png"
        )
        for m in state.monitors
    }
    assert state.palette is not None
    pal_dir = cache_entry_path(state_root, "palettes", state.palette.entry_hash)
    for artifact in (
        "colors.conf",
        "colors.gtk.css",
        "colors.yaml",
        "colors.adw.css",
        "colors.sequences",
    ):
        targets[artifact] = pal_dir / artifact
    assert state.effects is not None
    targets["effects"] = cache_entry_path(state_root, "effects", state.effects.entry_hash)
    assert state.icons is not None
    targets["icons"] = cache_entry_path(state_root, "icons", state.icons.entry_hash)

    current_dir = state_root / "current"
    current_dir.mkdir(parents=True)
    for name, target in targets.items():
        if name in ("effects", "icons"):
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"data")
        os.symlink(target, current_dir / name)
    return targets


def _snapshot(root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            snap[rel] = f"symlink:{os.readlink(path)}"
        elif path.is_dir():
            snap[rel] = "dir"
        else:
            snap[rel] = f"file:{path.read_bytes()!r}"
    return snap


class TestInspectStatusIntegration:
    def test_status_reflects_recorded_state_end_to_end(self, tmp_path: Path) -> None:
        """AC 1 + AC 2 — a fresh use case reads what the real repo persisted."""
        state = _make_state()
        targets = _build_live_tree(tmp_path, state)

        result = InspectStateUseCase(
            state_repo=JsonStateRepository(state_root=tmp_path),
            state_root=tmp_path,
        ).run()

        assert result.wallpaper == "a" * 64
        assert result.palette == "b" * 64
        assert result.effects == "1" * 64
        assert result.icons == "3" * 64
        assert result.applied_at == state.applied_at
        assert result.monitors == {
            "DP-1": {
                "backend": "hyprpaper",
                "source_hash": "a" * 64,
                "fit_mode": "cover",
                "mpv_options": None,
                "ipc_socket": None,
            }
        }
        assert set(result.current_symlinks) == set(targets)
        for name, status in result.current_symlinks.items():
            assert status.status == "ok", f"{name}: {status}"

    def test_divergence_detected_after_manual_repoint(self, tmp_path: Path) -> None:
        """AC 2 / NFR-3 — the filesystem, not the index, is the authority."""
        state = _make_state()
        _build_live_tree(tmp_path, state)
        decoy = tmp_path / "decoy-palette.yaml"
        decoy.write_text("decoy: true\n")
        link = tmp_path / "current" / "colors.yaml"
        link.unlink()
        os.symlink(decoy, link)

        result = InspectStateUseCase(
            state_repo=JsonStateRepository(state_root=tmp_path),
            state_root=tmp_path,
        ).run()

        assert result.current_symlinks["colors.yaml"].status == "diverged"
        assert result.current_symlinks["colors.yaml"].target == str(decoy)
        assert result.current_symlinks["colors.conf"].status == "ok"

    def test_missing_and_dangling_flagged(self, tmp_path: Path) -> None:
        """AC 2 — missing names and dangling targets are reported distinctly."""
        state = _make_state()
        _build_live_tree(tmp_path, state)
        (tmp_path / "current" / "colors.gtk.css").unlink()
        icons_link = tmp_path / "current" / "icons"
        icons_link.unlink()
        os.symlink(tmp_path / "cache" / "icons" / "gone", icons_link)

        result = InspectStateUseCase(
            state_repo=JsonStateRepository(state_root=tmp_path),
            state_root=tmp_path,
        ).run()

        assert result.current_symlinks["colors.gtk.css"].status == "missing"
        assert result.current_symlinks["colors.gtk.css"].target is None
        assert result.current_symlinks["icons"].status == "dangling"
        assert result.current_symlinks["icons"].target == str(
            tmp_path / "cache" / "icons" / "gone"
        )

    def test_absent_state_raises_on_empty_root(self, tmp_path: Path) -> None:
        """AC 3 — fresh machine: loud RuntimeError, no fabricated state."""
        use_case = InspectStateUseCase(
            state_repo=JsonStateRepository(state_root=tmp_path),
            state_root=tmp_path,
        )
        with pytest.raises(RuntimeError, match="no state recorded"):
            use_case.run()

    def test_inspection_is_read_only_on_diverged_tree(self, tmp_path: Path) -> None:
        """AC 4 — full state_root tree is byte-identical before/after a run
        on a diverged tree; no history.jsonl, no .seed.lock, no staging dirs."""
        state = _make_state()
        _build_live_tree(tmp_path, state)
        decoy = tmp_path / "decoy.conf"
        decoy.write_text("decoy\n")
        link = tmp_path / "current" / "colors.conf"
        link.unlink()
        os.symlink(decoy, link)
        before = _snapshot(tmp_path)

        InspectStateUseCase(
            state_repo=JsonStateRepository(state_root=tmp_path),
            state_root=tmp_path,
        ).run()

        assert _snapshot(tmp_path) == before
        assert not (tmp_path / "history.jsonl").exists()
        assert not (tmp_path / ".seed.lock").exists()
        assert not list(tmp_path.glob(".staging-*"))
        assert os.readlink(link) == str(decoy)


class TestInspectConsumerPointersIntegration:
    """gt-2-2 — spec-driven spine-pointer statuses end-to-end with a real
    spine layout (real JsonStateRepository, real filesystem)."""

    AGS = "config/ags/colors.css"
    GTK3 = "config/gtk-3.0/colors.css"
    GTK4 = "config/gtk-4.0/colors.css"

    def _make_use_case(
        self, tmp_path: Path, *, with_spec: bool = True
    ) -> tuple[Any, Path]:
        from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
        from runtime.application.inspect import InspectStateUseCase

        install_spine = tmp_path / "install"
        (install_spine / "config" / "ags").mkdir(parents=True)
        return (
            InspectStateUseCase(
                state_repo=JsonStateRepository(state_root=tmp_path),
                state_root=tmp_path,
                install_spine=install_spine,
                consumer_spec=StaticConsumerPathSpec() if with_spec else None,
            ),
            install_spine,
        )

    def test_pointer_statuses_end_to_end(self, tmp_path: Path) -> None:
        """Real spine layout: ok (ags), missing (absent gtk parents), and a
        diverged gtk-3.0 pointer are all classified through the spec."""
        state = _make_state()
        _build_live_tree(tmp_path, state)
        use_case, install_spine = self._make_use_case(tmp_path)
        gtk3_dir = install_spine / "config" / "gtk-3.0"
        gtk3_dir.mkdir()
        os.symlink(tmp_path / "current" / "colors.gtk.css", install_spine / self.AGS)
        decoy = tmp_path / "decoy-gtk3.css"
        decoy.write_text("/* decoy */\n")
        os.symlink(decoy, gtk3_dir / "colors.css")

        result = use_case.run()

        assert result.consumer_pointers[self.AGS].status == "ok"
        assert result.consumer_pointers[self.GTK3].status == "diverged"
        assert result.consumer_pointers[self.GTK3].target == str(decoy)
        assert result.consumer_pointers[self.GTK4].status == "missing"

    def test_pointer_projection_omitted_without_spec(self, tmp_path: Path) -> None:
        state = _make_state()
        _build_live_tree(tmp_path, state)
        use_case, _spine = self._make_use_case(tmp_path, with_spec=False)

        result = use_case.run()

        assert result.consumer_pointers == {}

    def test_pointer_projection_is_read_only(self, tmp_path: Path) -> None:
        state = _make_state()
        _build_live_tree(tmp_path, state)
        use_case, install_spine = self._make_use_case(tmp_path)
        before = _snapshot(install_spine)

        use_case.run()

        assert _snapshot(install_spine) == before
