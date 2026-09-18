"""Integration tests for the visible-first swap (openspec D1/D5, task 1.3/1.4).

Composes the REAL use cases on a REAL filesystem (real
``JsonStateRepository``/``CacheSeeder``/spine, contract-honest fake
csg/weg/itr, real flock mutex):

- Crash-safety (1.3): kill between ``visible`` and ``done`` (run ONLY
  the swap phase, then drop it like a dead process) reconverges via the
  existing reconcile — ``current.json`` validates, symlinks repoint, a
  follow-up set derives the missing layers.
- Post-visible failure (1.4): palette failure after the swap keeps the
  new wallpaper live (``current.json`` holds it with a null palette, no
  history line); effects/icons failure degrades to a ``done``-shaped
  full converge with null layers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.flock_seed_mutex import FlockSeedMutex, PassThroughSeedMutex
from runtime.adapters.hashing import hash_file
from runtime.adapters.json_state_repository import JsonStateRepository
from runtime.adapters.seeder import CacheSeeder
from runtime.application.reconcile import ReconcileDesktopStateUseCase


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class _FakeCsg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("csg exploded")
        output_dir.mkdir(parents=True, exist_ok=True)
        for name in (
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "colors.rasi",
            "colors.kitty",
        ):
            (output_dir / name).write_text(f"{name} content")
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
                colors_rasi=hash_file(output_dir / "colors.rasi"),
                colors_kitty=hash_file(output_dir / "colors.kitty"),
            ),
            generated_at=_now_z(),
        )


class _FakeWeg:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:
        from runtime.domain.models import EffectsArtifacts, EffectsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("weg exploded")
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
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> Any:
        from runtime.domain.models import IconsArtifacts, IconsEntry

        self.calls += 1
        if self.fail:
            raise RuntimeError("itr exploded")
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


class _PassingHyprpaper:
    def __init__(self, invoked: list[str] | None = None) -> None:
        self.invoked = invoked if invoked is not None else []

    def reload(self) -> bool:
        self.invoked.append("hyprpaper")
        return True


class _RecordingReloader:
    def __init__(self, name: str, invoked: list[str]) -> None:
        self._name = name
        self._invoked = invoked

    def reload(self) -> bool:
        self._invoked.append(self._name)
        return True


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


def _env(tmp_path: Path) -> tuple[Path, Path]:
    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    return tmp_path / "state", install_spine


def _run_swap(
    state_root: Path,
    install_spine: Path,
    img: Path,
    *,
    csg: Any | None = None,
    hyprpaper: Any | None = None,
) -> Any:
    """Phase 1 only, under the real outer flock (the composition shape)."""
    from runtime.application.swap_visible import SwapVisibleUseCase

    with FlockSeedMutex(state_root / ".seed.lock").hold(blocking=False):
        return SwapVisibleUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=PassThroughSeedMutex(),
            hyprpaper=hyprpaper or _PassingHyprpaper(),
            monitor_source=None,
        ).run(img)


def _run_themed(
    state_root: Path,
    install_spine: Path,
    img: Path,
    wallpaper_hash: str,
    *,
    csg: Any | None = None,
    weg: Any | None = None,
    itr: Any | None = None,
    reloaders: list[Any] | None = None,
) -> tuple[Any, Any]:
    """Phase 2 only, under the real outer flock (the composition shape)."""
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    with FlockSeedMutex(state_root / ".seed.lock").hold(blocking=False):
        apply_result = ApplyWallpaperUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            csg=csg or _FakeCsg(),
            weg=weg or _FakeWeg(),
            itr=itr or _FakeItr(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=PassThroughSeedMutex(),
        ).run(img, wallpaper_hash=wallpaper_hash)
        reconcile_result = ReconcileDesktopStateUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            csg=csg or _FakeCsg(),
            weg=weg or _FakeWeg(),
            itr=itr or _FakeItr(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=PassThroughSeedMutex(),
            reloaders=reloaders if reloaders is not None else [],
        ).run(trigger="set")
    return apply_result, reconcile_result


class TestCrashSafety:
    """Task 1.3: kill between ``visible``/``done`` reconverges."""

    def test_killed_after_swap_reconverges_via_reconcile(self, tmp_path: Path) -> None:
        state_root, install_spine = _env(tmp_path)
        img = tmp_path / "wall.png"
        img.write_bytes(b"crash me bytes")

        # The process dies right after the swap (visible emitted, done never).
        swap = _run_swap(state_root, install_spine, img)

        # A NEW process boots: current.json validates (wallpaper layer
        # present, layers nullable) and reconcile reconverges it.
        loaded = JsonStateRepository(state_root=state_root).load_current()
        assert loaded is not None
        assert loaded.wallpaper.content_hash == swap.wallpaper_hash
        assert loaded.palette is None and loaded.effects is None and loaded.icons is None

        invoked: list[str] = []
        result = ReconcileDesktopStateUseCase(
            state_repo=JsonStateRepository(state_root=state_root),
            csg=_FakeCsg(),
            weg=_FakeWeg(),
            itr=_FakeItr(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=FlockSeedMutex(state_root / ".seed.lock"),
            reloaders=[_RecordingReloader("Reloader0", invoked)],
        ).run(trigger="reconcile")

        assert result.repointed
        assert invoked == ["Reloader0"]
        lines = (state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1

    def test_followup_set_derives_missing_layers(self, tmp_path: Path) -> None:
        state_root, install_spine = _env(tmp_path)
        img = tmp_path / "wall.png"
        img.write_bytes(b"resume bytes")
        swap = _run_swap(state_root, install_spine, img)

        # The next set re-derives the missing layers (cold palette here).
        csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
        apply_result, reconcile_result = _run_themed(
            state_root,
            install_spine,
            img,
            swap.wallpaper_hash,
            csg=csg,
            weg=weg,
            itr=itr,
        )

        assert (csg.calls, weg.calls, itr.calls) == (1, 1, 1)
        assert apply_result.state.palette is not None
        assert reconcile_result.state.palette is not None
        lines = (state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["trigger"] == "set"


class TestPostVisibleFailure:
    """Task 1.4 / D5: post-visible failure keeps the wallpaper."""

    def test_palette_failure_keeps_wallpaper_with_null_palette(self, tmp_path: Path) -> None:
        state_root, install_spine = _env(tmp_path)
        img = tmp_path / "wall.png"
        img.write_bytes(b"palette down bytes")
        swap = _run_swap(state_root, install_spine, img)

        with pytest.raises(RuntimeError, match="palette apply failed"):
            _run_themed(
                state_root, install_spine, img, swap.wallpaper_hash, csg=_FakeCsg(fail=True)
            )

        # The new wallpaper stays live with a null palette; no history line
        # (phase 2 never reached its converge step — never a revert).
        loaded = JsonStateRepository(state_root=state_root).load_current()
        assert loaded is not None
        assert loaded.wallpaper.content_hash == swap.wallpaper_hash
        assert loaded.palette is None
        assert not (state_root / "history.jsonl").exists()
        link = state_root / "current" / "wallpaper-DP-1.png"
        assert link.resolve() == (
            state_root / "cache" / "wallpapers" / swap.wallpaper_hash / "wallpaper.png"
        )

    def test_effects_icons_failure_degrades_to_done(self, tmp_path: Path) -> None:
        state_root, install_spine = _env(tmp_path)
        img = tmp_path / "wall.png"
        img.write_bytes(b"graceful bytes")
        swap = _run_swap(state_root, install_spine, img)

        invoked: list[str] = []
        apply_result, reconcile_result = _run_themed(
            state_root,
            install_spine,
            img,
            swap.wallpaper_hash,
            weg=_FakeWeg(fail=True),
            itr=_FakeItr(fail=True),
            reloaders=[_RecordingReloader("Reloader0", invoked)],
        )

        # Graceful policy: null layers, converge still completes + history.
        assert apply_result.effects is None and apply_result.icons is None
        assert reconcile_result.state.palette is not None
        assert reconcile_result.state.effects is None
        assert invoked == ["Reloader0"]
        lines = (state_root / "history.jsonl").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["trigger"] == "set"
