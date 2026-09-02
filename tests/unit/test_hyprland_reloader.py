"""Unit tests for Hyprland reload adapter (Story 2.3)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from runtime.adapters.hyprland_reloader import HyprlandReloader
from runtime.ports.desktop_reloader import IDesktopReloader


class TestHyprlandReloaderSuccess:
    def test_reload_success_returns_true(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=Path("/usr/bin/hyprctl"))
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        with patch("runtime.adapters.hyprland_reloader.subprocess.run", return_value=mock_result):
            assert reloader.reload() is True

    def test_reload_invokes_hyprctl_reload(self) -> None:
        hyprctl = Path("/usr/bin/hyprctl")
        reloader = HyprlandReloader(hyprctl_path=hyprctl)
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stderr = ""
        with patch(
            "runtime.adapters.hyprland_reloader.subprocess.run", return_value=mock_result
        ) as mock_run:
            reloader.reload()
            mock_run.assert_called_once_with(
                [str(hyprctl), "reload"],
                capture_output=True,
                text=True,
                timeout=10,
            )


class TestHyprlandReloaderFailure:
    def test_reload_nonzero_exit_returns_false(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=Path("/usr/bin/hyprctl"))
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "error output"
        with patch("runtime.adapters.hyprland_reloader.subprocess.run", return_value=mock_result):
            assert reloader.reload() is False

    def test_reload_timeout_returns_false(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=Path("/usr/bin/hyprctl"))
        with patch(
            "runtime.adapters.hyprland_reloader.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="hyprctl reload", timeout=10),
        ):
            assert reloader.reload() is False

    def test_reload_command_not_found_returns_false(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=Path("/usr/bin/hyprctl"))
        with patch(
            "runtime.adapters.hyprland_reloader.subprocess.run",
            side_effect=FileNotFoundError("hyprctl not found"),
        ):
            assert reloader.reload() is False

    def test_reload_permission_denied_returns_false(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=Path("/usr/bin/hyprctl"))
        with patch(
            "runtime.adapters.hyprland_reloader.subprocess.run",
            side_effect=PermissionError("permission denied"),
        ):
            assert reloader.reload() is False

    def test_reload_oserror_returns_false(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=Path("/usr/bin/hyprctl"))
        with patch(
            "runtime.adapters.hyprland_reloader.subprocess.run",
            side_effect=OSError("generic os error"),
        ):
            assert reloader.reload() is False


class TestHyprlandReloaderMissing:
    def test_reload_no_hyprctl_returns_false(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=None)
        # Force None even if hyprctl is on PATH in CI
        reloader._hyprctl_path = None  # type: ignore[attr-defined]
        with patch("runtime.adapters.hyprland_reloader.subprocess.run") as mock_run:
            assert reloader.reload() is False
            mock_run.assert_not_called()

    def test_reload_none_path_does_not_invoke_subprocess(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=None)
        reloader._hyprctl_path = None  # type: ignore[attr-defined]
        with patch("runtime.adapters.hyprland_reloader.subprocess.run") as mock_run:
            result = reloader.reload()
            assert result is False
            assert mock_run.call_count == 0


class TestHyprlandReloaderInterface:
    def test_implements_port(self) -> None:
        reloader = HyprlandReloader(hyprctl_path=Path("/usr/bin/hyprctl"))
        assert isinstance(reloader, IDesktopReloader)


class TestReconcileReloadIntegration:
    def test_reload_failure_populates_reload_failures(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

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
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
                self.calls += 1
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
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, wallpaper_path: Path, output_dir: Path) -> object:
                from runtime.domain.models import EffectsArtifacts, EffectsEntry

                self.calls += 1
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
            def __init__(self) -> None:
                self.calls = 0

            def render(
                self, palette_hash: str, templates_dir: Path, mappings_path: Path, output_dir: Path
            ) -> object:
                from runtime.domain.models import IconsArtifacts, IconsEntry

                self.calls += 1
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

        class _FailingReloader(IDesktopReloader):
            def reload(self) -> bool:
                return False

        install_spine = tmp_path / "install"
        csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
        csg_templates.mkdir(parents=True)
        (csg_templates / "default.yaml").write_text("window: {}\n")
        weg_cfg = install_spine / "config" / "weg"
        weg_cfg.mkdir(parents=True)
        (weg_cfg / "effects.yaml").write_text("effects: []\n")
        itr_templates = install_spine / "config" / "icon-templates-renderer" / "templates"
        itr_templates.mkdir(parents=True)
        (itr_templates / "terminal.svg").write_text("<svg/>")
        (install_spine / "config" / "icon-templates-renderer" / "icons.yaml").write_text(
            "icons: {}\n"
        )

        state_root = tmp_path / "state"
        repo = JsonStateRepository(state_root=state_root)
        csg, weg, itr = _FakeCsg(), _FakeWeg(), _FakeItr()
        img = tmp_path / "wall.png"
        img.write_bytes(b"integration wallpaper bytes")

        from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

        ApplyWallpaperUseCase(
            state_repo=repo,
            csg=csg,  # type: ignore[arg-type]
            weg=weg,  # type: ignore[arg-type]
            itr=itr,  # type: ignore[arg-type]
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),  # type: ignore[arg-type]
        ).run(img)

        # Reconcile with failing reloader
        failing = _FailingReloader()
        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=csg,  # type: ignore[arg-type]
            weg=weg,  # type: ignore[arg-type]
            itr=itr,  # type: ignore[arg-type]
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),  # type: ignore[arg-type]
            reloaders=[failing],
        )
        result = use_case.run()
        assert "_FailingReloader" in result.reload_failures

    def test_reload_success_empty_failures(self, tmp_path: Path) -> None:
        from datetime import UTC, datetime

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
                (output_dir / "icon.svg").write_text("<svg/>")
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

        class _PassingReloader(IDesktopReloader):
            def reload(self) -> bool:
                return True

        install_spine = tmp_path / "install2"
        csg_templates = install_spine / "config" / "color-scheme-generator" / "templates"
        csg_templates.mkdir(parents=True)
        (csg_templates / "default.yaml").write_text("window: {}\n")
        weg_cfg = install_spine / "config" / "weg"
        weg_cfg.mkdir(parents=True)
        (weg_cfg / "effects.yaml").write_text("effects: []\n")
        itr_templates = install_spine / "config" / "icon-templates-renderer" / "templates"
        itr_templates.mkdir(parents=True)
        (itr_templates / "terminal.svg").write_text("<svg/>")
        (install_spine / "config" / "icon-templates-renderer" / "icons.yaml").write_text(
            "icons: {}\n"
        )
        state_root = tmp_path / "state2"
        repo = JsonStateRepository(state_root=state_root)
        img = tmp_path / "wall2.png"
        img.write_bytes(b"wallpaper 2")
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
        use_case = ReconcileDesktopStateUseCase(
            state_repo=repo,
            csg=_FakeCsg(),  # type: ignore[arg-type]
            weg=_FakeWeg(),  # type: ignore[arg-type]
            itr=_FakeItr(),  # type: ignore[arg-type]
            install_spine=install_spine,
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            mutex=_FakeMutex(),  # type: ignore[arg-type]
            reloaders=[_PassingReloader()],
        )
        result = use_case.run()
        assert result.reload_failures == []
