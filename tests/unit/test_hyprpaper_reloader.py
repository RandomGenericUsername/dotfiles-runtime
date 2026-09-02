"""Unit tests for Hyprpaper wallpaper reload adapter (Story 2.5)."""

from __future__ import annotations

import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runtime.adapters.hyprpaper_reloader import HyprpaperReloader
from runtime.ports.desktop_reloader import IDesktopReloader


@pytest.fixture
def hyprctl_bin(tmp_path: Path) -> Path:
    """An executable ``hyprctl`` probe the adapter can resolve explicitly."""
    probe = tmp_path / "hyprctl"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return probe


def _make_current(state_root: Path, monitors: list[str], dangling: list[str] | None = None) -> dict[str, Path]:
    """Seed ``current/wallpaper-<monitor>.png`` symlinks to real cache files."""
    current = state_root / "current"
    current.mkdir(parents=True, exist_ok=True)
    targets: dict[str, Path] = {}
    for monitor in monitors:
        cache_file = state_root / "cache" / "wallpapers" / f"wh-{monitor}" / "wallpaper.png"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(b"png bytes")
        link = current / f"wallpaper-{monitor}.png"
        link.symlink_to(cache_file)
        targets[monitor] = cache_file
    for monitor in dangling or []:
        link = current / f"wallpaper-{monitor}.png"
        link.symlink_to(state_root / "cache" / "wallpapers" / "missing" / "wallpaper.png")
    return targets


def _ok_result() -> MagicMock:
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = ""
    mock.stderr = ""
    return mock


class TestHyprpaperReloaderSuccess:
    def test_reload_success_returns_true(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _make_current(state_root, ["DP-1", "DP-2"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        with patch(
            "runtime.adapters.hyprpaper_reloader.subprocess.run", return_value=_ok_result()
        ) as mock_run:
            assert reloader.reload() is True
        assert mock_run.call_count == 2

    def test_invokes_wallpaper_ipc_per_monitor(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        targets = _make_current(state_root, ["DP-1"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        with patch(
            "runtime.adapters.hyprpaper_reloader.subprocess.run", return_value=_ok_result()
        ) as mock_run:
            reloader.reload()
        mock_run.assert_called_once_with(
            [str(hyprctl_bin), "hyprpaper", "wallpaper", f"DP-1,{targets['DP-1']}"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_uses_resolved_symlink_target(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        targets = _make_current(state_root, ["HDMI-A-1"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        with patch(
            "runtime.adapters.hyprpaper_reloader.subprocess.run", return_value=_ok_result()
        ) as mock_run:
            reloader.reload()
        argv = mock_run.call_args.args[0]
        assert argv[3] == f"HDMI-A-1,{targets['HDMI-A-1'].resolve()}"

    def test_zero_monitors_returns_true(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        state_root.mkdir(parents=True)
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        with patch("runtime.adapters.hyprpaper_reloader.subprocess.run") as mock_run:
            assert reloader.reload() is True
            mock_run.assert_not_called()

    def test_no_current_dir_returns_true(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=tmp_path / "absent")
        with patch("runtime.adapters.hyprpaper_reloader.subprocess.run") as mock_run:
            assert reloader.reload() is True
            mock_run.assert_not_called()

    def test_monitor_order_sorted_deterministic(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        targets = _make_current(state_root, ["eDP-1", "HDMI-A-1", "DP-1"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        with patch(
            "runtime.adapters.hyprpaper_reloader.subprocess.run", return_value=_ok_result()
        ) as mock_run:
            assert reloader.reload() is True
        called_monitors = [call.args[0][3].split(",")[0] for call in mock_run.call_args_list]
        assert called_monitors == sorted(targets.keys())


class TestHyprpaperReloaderFailure:
    def test_monitor_failure_returns_false(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _make_current(state_root, ["DP-1", "DP-2"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        ok, bad = _ok_result(), _ok_result()
        bad.returncode = 1
        bad.stdout = "error: failed to set wallpaper: Invalid monitor"
        with patch(
            "runtime.adapters.hyprpaper_reloader.subprocess.run", side_effect=[ok, bad]
        ):
            assert reloader.reload() is False

    def test_dangling_symlink_is_failure(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _make_current(state_root, [], dangling=["DP-1"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        with patch("runtime.adapters.hyprpaper_reloader.subprocess.run") as mock_run:
            assert reloader.reload() is False
            mock_run.assert_not_called()

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("gone"),
            PermissionError("denied"),
            subprocess.TimeoutExpired(cmd="hyprctl hyprpaper wallpaper", timeout=10),
            OSError("generic os error"),
            ValueError("embedded null byte"),
        ],
    )
    def test_subprocess_exception_returns_false(
        self, hyprctl_bin: Path, tmp_path: Path, exc: Exception
    ) -> None:
        state_root = tmp_path / "state"
        _make_current(state_root, ["DP-1"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        with patch(
            "runtime.adapters.hyprpaper_reloader.subprocess.run", side_effect=exc
        ):
            assert reloader.reload() is False

    def test_timeout_returns_false(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _make_current(state_root, ["DP-1"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        with patch(
            "runtime.adapters.hyprpaper_reloader.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="hyprctl hyprpaper wallpaper", timeout=10),
        ):
            assert reloader.reload() is False

    def test_one_failure_does_not_stop_other_monitors(
        self, hyprctl_bin: Path, tmp_path: Path
    ) -> None:
        state_root = tmp_path / "state"
        _make_current(state_root, ["DP-1", "DP-2"])
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=state_root)
        bad, ok = _ok_result(), _ok_result()
        bad.returncode = 1
        with patch(
            "runtime.adapters.hyprpaper_reloader.subprocess.run", side_effect=[bad, ok]
        ) as mock_run:
            assert reloader.reload() is False
        assert mock_run.call_count == 2


class TestHyprpaperReloaderMissing:
    def test_missing_hyprctl_returns_false(self, tmp_path: Path) -> None:
        state_root = tmp_path / "state"
        _make_current(state_root, ["DP-1"])
        with patch(
            "runtime.adapters.hyprland_reloader.shutil.which", return_value=None
        ) as mock_which:
            reloader = HyprpaperReloader(hyprctl_path=None, state_root=state_root)
        mock_which.assert_called_once_with("hyprctl")
        with patch("runtime.adapters.hyprpaper_reloader.subprocess.run") as mock_run:
            assert reloader.reload() is False
            mock_run.assert_not_called()

    def test_non_executable_hyprctl_returns_false(self, tmp_path: Path) -> None:
        non_exec = tmp_path / "hyprctl"
        non_exec.write_text("#!/bin/sh\nexit 0\n")
        non_exec.chmod(0o644)
        with patch(
            "runtime.adapters.hyprland_reloader.shutil.which", return_value=str(non_exec)
        ):
            reloader = HyprpaperReloader(hyprctl_path=None, state_root=tmp_path / "state")
        with patch("runtime.adapters.hyprpaper_reloader.subprocess.run") as mock_run:
            assert reloader.reload() is False
            mock_run.assert_not_called()

    def test_resolve_explicit_missing_path_fails_later(self, tmp_path: Path) -> None:
        reloader = HyprpaperReloader(
            hyprctl_path=tmp_path / "does-not-exist", state_root=tmp_path / "state"
        )
        with patch("runtime.adapters.hyprpaper_reloader.subprocess.run") as mock_run:
            assert reloader.reload() is False
            mock_run.assert_not_called()

    def test_which_called_with_hyprctl(self, tmp_path: Path) -> None:
        with patch(
            "runtime.adapters.hyprland_reloader.shutil.which", return_value=None
        ) as mock_which:
            HyprpaperReloader(hyprctl_path=None, state_root=tmp_path / "state")
        mock_which.assert_called_once_with("hyprctl")


class TestHyprpaperReloaderInterface:
    def test_implements_desktop_reloader_port(self, hyprctl_bin: Path, tmp_path: Path) -> None:
        reloader = HyprpaperReloader(hyprctl_path=hyprctl_bin, state_root=tmp_path / "state")
        assert isinstance(reloader, IDesktopReloader)


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
        from runtime.adapters.hashing import hash_file
        from runtime.domain.models import PaletteArtifacts, PaletteEntry

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
    def render(
        self,
        palette_hash: str,
        templates_dir: Path,
        mappings_path: Path,
        output_dir: Path,
    ) -> object:
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
            artifact_hashes=IconsArtifacts(
                **{"icon.svg": hash_file(output_dir / "icon.svg")}
            ),
            generated_at=_now_z(),
        )


def _now_z() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


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


def _make_applied(tmp_path: Path) -> tuple[object, Path, Path]:
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.seeder import CacheSeeder
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"hyprpaper reloader unit wallpaper")
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


def _make_reconcile_with_reloaders(
    repo: object,
    state_root: Path,
    install_spine: Path,
    reloaders: list[IDesktopReloader],
) -> object:
    from runtime.adapters.seeder import CacheSeeder
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    return ReconcileDesktopStateUseCase(
        state_repo=repo,  # type: ignore[arg-type]
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
        reloaders=reloaders,
    )


class _FailingReloader(IDesktopReloader):
    def __init__(self) -> None:
        self.calls = 0

    def reload(self) -> bool:
        self.calls += 1
        return False


class _PassingReloader(IDesktopReloader):
    def __init__(self) -> None:
        self.calls = 0

    def reload(self) -> bool:
        self.calls += 1
        return True


class TestHyprpaperReconcileReloadIntegration:
    def test_hyprpaper_failure_populates_reload_failures(self, tmp_path: Path) -> None:
        repo, state_root, install_spine = _make_applied(tmp_path)
        failing = _FailingReloader()
        passing = _PassingReloader()
        use_case = _make_reconcile_with_reloaders(
            repo, state_root, install_spine, [passing, failing]
        )
        result = use_case.run()  # type: ignore[attr-defined]
        assert "_FailingReloader" in result.reload_failures
        assert "_PassingReloader" not in result.reload_failures

    def test_all_reloaders_invoked_once(self, tmp_path: Path) -> None:
        repo, state_root, install_spine = _make_applied(tmp_path)
        hyprland = _PassingReloader()
        ags = _PassingReloader()
        hyprpaper = _PassingReloader()
        use_case = _make_reconcile_with_reloaders(
            repo, state_root, install_spine, [hyprland, ags, hyprpaper]
        )
        use_case.run()  # type: ignore[attr-defined]
        assert hyprland.calls == 1
        assert ags.calls == 1
        assert hyprpaper.calls == 1
