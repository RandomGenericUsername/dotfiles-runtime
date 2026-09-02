"""Unit tests for AGS restart reload adapter (Story 2.4)."""

from __future__ import annotations

import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from runtime.adapters.ags_reloader import AgsReloader
from runtime.ports.desktop_reloader import IDesktopReloader


@pytest.fixture
def ags_bin(tmp_path: Path) -> Path:
    """An executable ``ags`` probe the adapter can resolve explicitly."""
    probe = tmp_path / "ags"
    probe.write_text("#!/bin/sh\nexit 0\n")
    probe.chmod(probe.stat().st_mode | stat.S_IEXEC)
    return probe


class _FakeProcess:
    """Fake ``Popen`` result: ``poll()`` yields ``code`` (None = alive)."""

    def __init__(self, code: int | None) -> None:
        self.returncode = code
        self._poll_result = code

    def poll(self) -> int | None:
        return self._poll_result


class _SequenceProcess:
    """Fake ``Popen`` result whose ``poll()`` yields codes in sequence."""

    def __init__(self, codes: list[int | None]) -> None:
        self._codes = list(codes)
        self.returncode = next((code for code in self._codes if code is not None), 0)

    def poll(self) -> int | None:
        return self._codes.pop(0) if self._codes else self.returncode


class TestAgsReloaderSuccess:
    def test_reload_success_returns_true(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        fake = _FakeProcess(code=None)
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run"),
            patch("runtime.adapters.ags_reloader.subprocess.Popen", return_value=fake),
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            assert reloader.reload() is True

    def test_quit_invoked_with_ags_quit(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        fake = _FakeProcess(code=None)
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run") as mock_run,
            patch("runtime.adapters.ags_reloader.subprocess.Popen", return_value=fake),
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            reloader.reload()
        mock_run.assert_called_once_with(
            [str(ags_bin), "quit"],
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_run_spawned_detached(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        fake = _FakeProcess(code=None)
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run"),
            patch("runtime.adapters.ags_reloader.subprocess.Popen") as mock_popen,
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            reloader.reload()
        mock_popen.assert_called_once_with(
            [str(ags_bin), "run"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


class TestAgsReloaderFailure:
    def test_run_process_dies_within_window_returns_false(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        fake = _FakeProcess(code=1)
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run"),
            patch("runtime.adapters.ags_reloader.subprocess.Popen", return_value=fake),
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            assert reloader.reload() is False

    def test_quit_failure_is_tolerated(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "no instance running"
        fake = _FakeProcess(code=None)
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run", return_value=mock_result),
            patch(
                "runtime.adapters.ags_reloader.subprocess.Popen",
                return_value=_FakeProcess(code=None),
            ) as mock_popen,
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            assert reloader.reload() is True
            mock_popen.assert_called_once()

    def test_quit_exception_is_tolerated(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        with (
            patch(
                "runtime.adapters.ags_reloader.subprocess.run",
                side_effect=FileNotFoundError("ags quit"),
            ),
            patch(
                "runtime.adapters.ags_reloader.subprocess.Popen",
                return_value=_FakeProcess(code=None),
            ) as mock_popen,
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            assert reloader.reload() is True
            mock_popen.assert_called_once()

    @pytest.mark.parametrize(
        "exc",
        [
            FileNotFoundError("ags run"),
            PermissionError("permission denied"),
            OSError("generic os error"),
            subprocess.TimeoutExpired(cmd="ags run", timeout=1),
            ValueError("embedded null byte"),
        ],
    )
    def test_popen_exception_returns_false(self, ags_bin: Path, exc: Exception) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run"),
            patch("runtime.adapters.ags_reloader.subprocess.Popen", side_effect=exc),
        ):
            assert reloader.reload() is False


class TestAgsReloaderLiveness:
    def test_liveness_window_contract_is_pinned(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        fake = MagicMock()
        fake.poll.return_value = None
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run"),
            patch("runtime.adapters.ags_reloader.subprocess.Popen", return_value=fake),
            patch("runtime.adapters.ags_reloader.time.sleep") as mock_sleep,
        ):
            assert reloader.reload() is True
        assert mock_sleep.call_count == 7
        mock_sleep.assert_called_with(0.25)
        assert fake.poll.call_count == 8

    def test_death_mid_window_returns_false(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        fake = _SequenceProcess([None, None, None, None, 1])
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run"),
            patch("runtime.adapters.ags_reloader.subprocess.Popen", return_value=fake),
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            assert reloader.reload() is False

    def test_death_on_final_poll_returns_false(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        fake = _SequenceProcess([None] * 7 + [1])
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run"),
            patch("runtime.adapters.ags_reloader.subprocess.Popen", return_value=fake),
            patch("runtime.adapters.ags_reloader.time.sleep"),
        ):
            assert reloader.reload() is False


class TestAgsReloaderMissing:
    def test_missing_ags_returns_false(self) -> None:
        with patch(
            "runtime.adapters.hyprland_reloader.shutil.which", return_value=None
        ) as mock_which:
            reloader = AgsReloader(ags_path=None)
        mock_which.assert_called_once_with("ags")
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run") as mock_run,
            patch("runtime.adapters.ags_reloader.subprocess.Popen") as mock_popen,
        ):
            assert reloader.reload() is False
            mock_run.assert_not_called()
            mock_popen.assert_not_called()

    def test_non_executable_ags_returns_false(self, tmp_path: Path) -> None:
        non_exec = tmp_path / "ags"
        non_exec.write_text("#!/bin/sh\nexit 0\n")
        non_exec.chmod(0o644)
        with patch("runtime.adapters.hyprland_reloader.shutil.which", return_value=str(non_exec)):
            reloader = AgsReloader(ags_path=None)
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run") as mock_run,
            patch("runtime.adapters.ags_reloader.subprocess.Popen") as mock_popen,
        ):
            assert reloader.reload() is False
            mock_run.assert_not_called()
            mock_popen.assert_not_called()

    def test_resolve_explicit_missing_path_fails_later(self, tmp_path: Path) -> None:
        reloader = AgsReloader(ags_path=tmp_path / "does-not-exist")
        with (
            patch("runtime.adapters.ags_reloader.subprocess.run") as mock_run,
            patch("runtime.adapters.ags_reloader.subprocess.Popen") as mock_popen,
        ):
            assert reloader.reload() is False
            mock_run.assert_not_called()
            mock_popen.assert_not_called()

    def test_which_called_with_ags(self, ags_bin: Path) -> None:
        with patch(
            "runtime.adapters.hyprland_reloader.shutil.which", return_value=str(ags_bin)
        ) as mock_which:
            AgsReloader(ags_path=None)
        mock_which.assert_called_once_with("ags")


class TestAgsReloaderInterface:
    def test_implements_port(self, ags_bin: Path) -> None:
        reloader = AgsReloader(ags_path=ags_bin)
        assert isinstance(reloader, IDesktopReloader)


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


def _make_applied(
    tmp_path: Path,
) -> tuple[object, Path, Path]:
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.seeder import CacheSeeder
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"ags reloader unit wallpaper")
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


class TestAgsReconcileReloadIntegration:
    def test_ags_reload_failure_populates_reload_failures(self, tmp_path: Path) -> None:
        repo, state_root, install_spine = _make_applied(tmp_path)
        failing = _FailingReloader()
        passing = _PassingReloader()
        use_case = _make_reconcile_with_reloaders(
            repo, state_root, install_spine, [passing, failing]
        )
        result = use_case.run()  # type: ignore[attr-defined]
        assert "_FailingReloader" in result.reload_failures
        assert "_PassingReloader" not in result.reload_failures

    def test_both_reloaders_invoked_once(self, tmp_path: Path) -> None:
        repo, state_root, install_spine = _make_applied(tmp_path)
        hypr = _PassingReloader()
        ags = _PassingReloader()
        use_case = _make_reconcile_with_reloaders(repo, state_root, install_spine, [hypr, ags])
        use_case.run()  # type: ignore[attr-defined]
        assert hypr.calls == 1
        assert ags.calls == 1
