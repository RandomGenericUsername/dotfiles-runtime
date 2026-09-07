"""Unit tests for InspectStateUseCase (Story 3.2, rt-3-2).

Covers: status projection from current.json (AC 1, FR-7, CAP-7), live
``current/`` symlink reflection with ok/missing/diverged/dangling statuses
(AC 2, NFR-3 filesystem authority), absent-state loud failure (AC 3), and
the read-only invariant (AC 4 — no mutation of any kind).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.cache import cache_entry_path
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
from runtime.ports.state_repository import IStateRepository


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _make_state(
    *, with_palette: bool = True, with_effects: bool = True, with_icons: bool = True
) -> DesktopState:
    """Deterministic seeded-style state with 'a'*64-style hashes."""
    wh = "a" * 64
    now = _now_z()
    palette = (
        PaletteEntry(
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
        )
        if with_palette
        else None
    )
    effects = (
        EffectsEntry(
            hash_algorithm="sha256",
            kind="effects",
            entry_hash="1" * 64,
            source_wallpaper_hash=wh,
            input_catalog_hash="2" * 64,
            artifact_hashes={},
            generated_at=now,
        )
        if with_effects
        else None
    )
    icons = (
        IconsEntry(
            hash_algorithm="sha256",
            kind="icons",
            entry_hash="3" * 64,
            source_palette_hash="b" * 64,
            input_templates_hash="4" * 64,
            input_mappings_hash="5" * 64,
            artifact_hashes={},
            generated_at=now,
        )
        if with_icons
        else None
    )
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
        palette=palette,
        effects=effects,
        icons=icons,
        applied_at=now,
    )


class _FakeStateRepo(IStateRepository):
    """In-memory repo; save() is a tripwire for the read-only invariant."""

    def __init__(self, state: DesktopState | None) -> None:
        self._state = state

    def load_current(self) -> DesktopState | None:
        return self._state

    def save(self, state: DesktopState) -> None:
        raise AssertionError("inspect must never save (AC 4 read-only)")


def _expected_targets(state_root: Path, state: DesktopState) -> dict[str, Path]:
    """Build the expected current/ symlink target map for the state."""
    targets: dict[str, Path] = {}
    for monitor in state.monitors:
        targets[f"wallpaper-{monitor}.png"] = (
            cache_entry_path(state_root, "wallpapers", state.wallpaper.content_hash)
            / "wallpaper.png"
        )
    if state.palette is not None:
        pal_dir = cache_entry_path(state_root, "palettes", state.palette.entry_hash)
        for artifact in (
            "colors.conf",
            "colors.gtk.css",
            "colors.yaml",
            "colors.adw.css",
            "colors.sequences",
        ):
            targets[artifact] = pal_dir / artifact
    if state.effects is not None:
        targets["effects"] = cache_entry_path(state_root, "effects", state.effects.entry_hash)
    if state.icons is not None:
        targets["icons"] = cache_entry_path(state_root, "icons", state.icons.entry_hash)
    return targets


def _make_current_tree(state_root: Path, state: DesktopState) -> dict[str, Path]:
    """Create real cache-entry files/dirs and current/ symlinks pointing at them."""
    targets = _expected_targets(state_root, state)
    current_dir = state_root / "current"
    current_dir.mkdir(parents=True)
    for name, target in targets.items():
        if name in ("effects", "icons"):
            target.mkdir(parents=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"data")
        (current_dir / name).symlink_to(target)
    return targets


def _snapshot(root: Path) -> dict[str, str]:
    """Full tree snapshot: path -> kind(+symlink target or file bytes)."""
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


class TestInspectStateUseCaseAbsentState:
    """AC 3 — absent state must be loud, never fabricated."""

    def test_absent_state_raises_runtime_error_with_seed_hint(self) -> None:
        use_case = InspectStateUseCase(state_repo=_FakeStateRepo(None), state_root=Path("/s"))
        with pytest.raises(RuntimeError) as exc_info:
            use_case.run()
        assert "no state recorded" in str(exc_info.value)
        assert "wallpaper set" in str(exc_info.value)


class TestInspectStateUseCaseProjection:
    """AC 1 — project wallpaper/monitors/palette/effects/icons from DesktopState."""

    def test_happy_path_projects_all_layers(self, tmp_path: Path) -> None:
        state = _make_state()
        result = InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()

        assert result.wallpaper == "a" * 64
        assert result.wallpaper_source_path == "/img/wall.png"
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

    def test_degraded_layers_render_absent_not_crash(self, tmp_path: Path) -> None:
        """palette/effects/icons are None when never derived (degraded apply)."""
        state = _make_state(with_palette=False, with_effects=False, with_icons=False)
        result = InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()

        assert result.wallpaper == "a" * 64
        assert result.palette is None
        assert result.effects is None
        assert result.icons is None
        # Only the per-monitor wallpaper symlink is expected when no layers derived
        assert set(result.current_symlinks) == {"wallpaper-DP-1.png"}


class TestInspectSymlinkReflection:
    """AC 2 / NFR-3 — live current/ symlink targets are the filesystem authority."""

    def test_all_ok_when_current_tree_matches(self, tmp_path: Path) -> None:
        state = _make_state()
        targets = _make_current_tree(tmp_path, state)

        result = InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()

        assert set(result.current_symlinks) == set(targets)
        for name, status in result.current_symlinks.items():
            assert status.status == "ok", f"{name}: {status}"
            assert status.target == str(targets[name].resolve())

    def test_missing_when_current_dir_absent(self, tmp_path: Path) -> None:
        state = _make_state()
        result = InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()

        expected_names = {
            "wallpaper-DP-1.png",
            "colors.yaml",
            "colors.conf",
            "colors.gtk.css",
            "colors.adw.css",
            "colors.sequences",
            "effects",
            "icons",
        }
        assert set(result.current_symlinks) == expected_names
        for name, status in result.current_symlinks.items():
            assert status.status == "missing", f"{name}: {status}"
            assert status.target is None

    def test_diverged_flagged_with_actual_target(self, tmp_path: Path) -> None:
        state = _make_state()
        targets = _make_current_tree(tmp_path, state)
        wrong = tmp_path / "elsewhere"
        wrong.mkdir()
        link = tmp_path / "current" / "colors.yaml"
        link.unlink()
        link.symlink_to(wrong)

        result = InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()

        assert result.current_symlinks["colors.yaml"].status == "diverged"
        assert result.current_symlinks["colors.yaml"].target == str(wrong.resolve())
        # Other symlinks unaffected
        assert result.current_symlinks["colors.conf"].status == "ok"
        assert result.current_symlinks["wallpaper-DP-1.png"].target == str(
            targets["wallpaper-DP-1.png"].resolve()
        )

    def test_dangling_flagged_never_crashes(self, tmp_path: Path) -> None:
        """Dangling symlink is a distinct status (mirror reconcile.py:543-547)."""
        state = _make_state()
        _make_current_tree(tmp_path, state)
        link = tmp_path / "current" / "effects"
        link.unlink()
        link.symlink_to(tmp_path / "cache" / "effects" / "missing")

        result = InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()

        assert result.current_symlinks["effects"].status == "dangling"
        assert result.current_symlinks["effects"].target == str(
            tmp_path / "cache" / "effects" / "missing"
        )

    def test_non_symlink_path_reports_missing(self, tmp_path: Path) -> None:
        """A regular file squatting on a consumer name has no symlink target."""
        state = _make_state()
        _make_current_tree(tmp_path, state)
        link = tmp_path / "current" / "icons"
        link.unlink()
        link.write_text("not a symlink")

        result = InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()

        assert result.current_symlinks["icons"].status == "missing"


class TestInspectConsumerPointerProjection:
    """gt-2-2 — spec-driven spine-pointer projection (additive, read-only)."""

    AGS = "config/ags/colors.css"
    GTK3 = "config/gtk-3.0/colors.css"
    GTK4 = "config/gtk-4.0/colors.css"

    @staticmethod
    def _make_use_case(tmp_path: Path, state: DesktopState, *, with_spec: bool = True) -> Any:
        from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
        from runtime.application.inspect import InspectStateUseCase

        install_spine = tmp_path / "install"
        (install_spine / "config" / "ags").mkdir(parents=True)
        return (
            InspectStateUseCase(
                _FakeStateRepo(state),
                tmp_path,
                install_spine=install_spine,
                consumer_spec=StaticConsumerPathSpec() if with_spec else None,
            ),
            install_spine,
        )

    def test_ok_when_pointer_resolves_to_expected_target(self, tmp_path: Path) -> None:
        state = _make_state()
        _make_current_tree(tmp_path, state)
        use_case, install_spine = self._make_use_case(tmp_path, state)
        (install_spine / "config" / "gtk-3.0").mkdir()
        (install_spine / "config" / "gtk-4.0").mkdir()
        (install_spine / self.AGS).symlink_to(tmp_path / "current" / "colors.gtk.css")
        (install_spine / self.GTK3).symlink_to(tmp_path / "current" / "colors.gtk.css")
        (install_spine / self.GTK4).symlink_to(tmp_path / "current" / "colors.adw.css")

        result = use_case.run()

        assert set(result.consumer_pointers) == {self.AGS, self.GTK3, self.GTK4}
        expected_targets = {
            self.AGS: "colors.gtk.css",
            self.GTK3: "colors.gtk.css",
            self.GTK4: "colors.adw.css",
        }
        for path, status in result.consumer_pointers.items():
            assert status.status == "ok"
            assert status.target == str((tmp_path / "current" / expected_targets[path]).resolve())

    def test_missing_when_dest_or_parent_absent(self, tmp_path: Path) -> None:
        """Absent dest AND absent parent both report missing (pre-gt-3-1
        spine) — expected state, never a crash."""
        state = _make_state()
        _make_current_tree(tmp_path, state)
        use_case, _spine = self._make_use_case(tmp_path, state)

        result = use_case.run()

        assert result.consumer_pointers[self.AGS].status == "missing"
        assert result.consumer_pointers[self.GTK3].status == "missing"
        assert result.consumer_pointers[self.GTK4].status == "missing"
        assert all(s.target is None for s in result.consumer_pointers.values())

    def test_diverged_flagged_with_actual_target(self, tmp_path: Path) -> None:
        state = _make_state()
        _make_current_tree(tmp_path, state)
        use_case, install_spine = self._make_use_case(tmp_path, state)
        wrong = tmp_path / "elsewhere.css"
        wrong.write_text("/* wrong */")
        (install_spine / self.AGS).symlink_to(wrong)

        result = use_case.run()

        status = result.consumer_pointers[self.AGS]
        assert status.status == "diverged"
        assert status.target == str(wrong.resolve())

    def test_dangling_flagged_never_crashes(self, tmp_path: Path) -> None:
        state = _make_state()
        _make_current_tree(tmp_path, state)
        use_case, install_spine = self._make_use_case(tmp_path, state)
        (install_spine / self.AGS).symlink_to(tmp_path / "current" / "colors.gtk.css")
        (tmp_path / "current" / "colors.gtk.css").unlink()

        result = use_case.run()

        status = result.consumer_pointers[self.AGS]
        assert status.status == "dangling"
        assert status.target == str(tmp_path / "current" / "colors.gtk.css")

    def test_null_palette_omits_pointers_entirely(self, tmp_path: Path) -> None:
        state = _make_state(with_palette=False)
        use_case, _spine = self._make_use_case(tmp_path, state)

        result = use_case.run()

        assert result.consumer_pointers == {}

    def test_none_spec_leaves_field_empty_backward_compat(self, tmp_path: Path) -> None:
        state = _make_state()
        _make_current_tree(tmp_path, state)
        use_case, _spine = self._make_use_case(tmp_path, state, with_spec=False)

        result = use_case.run()

        assert result.consumer_pointers == {}

    def test_projection_is_read_only(self, tmp_path: Path) -> None:
        state = _make_state()
        _make_current_tree(tmp_path, state)
        use_case, install_spine = self._make_use_case(tmp_path, state)
        before = _snapshot(install_spine)

        use_case.run()

        assert _snapshot(install_spine) == before


class TestInspectReadOnlyInvariant:
    """AC 4 — inspect mutates NOTHING: negative side-effect assertions."""

    def test_run_mutates_nothing_on_diverged_tree(self, tmp_path: Path) -> None:
        """A mutating use case would 'fix' the diverged symlink; inspect must not."""
        state = _make_state()
        _make_current_tree(tmp_path, state)
        wrong = tmp_path / "elsewhere"
        wrong.mkdir()
        link = tmp_path / "current" / "colors.conf"
        link.unlink()
        link.symlink_to(wrong)
        before = _snapshot(tmp_path)

        InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()

        assert _snapshot(tmp_path) == before
        # No history append, no seed lock, no staging dirs, no cache writes
        assert not (tmp_path / "history.jsonl").exists()
        assert not (tmp_path / ".seed.lock").exists()
        assert not list(tmp_path.glob(".staging-*"))
        assert os.readlink(link) == str(wrong)

    def test_run_never_touches_repo_save(self, tmp_path: Path) -> None:
        state = _make_state()
        # _FakeStateRepo.save raises AssertionError — run() must not trip it
        result = InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()
        assert result.wallpaper == "a" * 64

    def test_run_does_not_create_current_dir_when_absent(self, tmp_path: Path) -> None:
        state = _make_state()
        InspectStateUseCase(_FakeStateRepo(state), tmp_path).run()
        assert not (tmp_path / "current").exists()
