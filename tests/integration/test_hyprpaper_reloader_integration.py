"""Integration test for Hyprpaper wallpaper reload adapter (Story 2.5).

The fake ``hyprctl`` shim emulates the VERIFIED hyprpaper v0.8.4 request
contract — ``hyprctl hyprpaper wallpaper <monitor>,<path>`` with a single
space-free comma-delimited segment — but does NOT invoke a live hyprpaper
(a live on-wire change needs a running Hyprland session, which the dev
container does not have; source-level verification of the installed
versions is the evidence, mirroring the AGS story pattern).
"""

from __future__ import annotations

import os
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.hyprpaper_reloader import HyprpaperReloader
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


def _make_hyprpaper_shim(bin_dir: Path, marker: Path, exit_code: int) -> None:
    """Fake ``hyprctl`` shim: records argv and emulates the v0.8.4 contract.

    Exits ``exit_code`` when the request parses as
    ``hyprpaper wallpaper <nonempty-monitor>,<absolute-existing-path>``;
    otherwise exits 1 (invalid monitor/path → failure, like the real
    hyprpaper error contract).
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "hyprctl"
    shim.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> {marker}\n'
        'if [ "$1" = "hyprpaper" ] && [ "$2" = "wallpaper" ]; then\n'
        '  case "$3" in\n'
        '    *,/*)\n'
        '      p="${3#*,}"\n'
        '      if [ -n "${3%,*}" ] && [ -e "$p" ]; then\n'
        f'        exit {exit_code}\n'
        "      fi\n"
        "      ;;\n"
        "  esac\n"
        "fi\n"
        "exit 1\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)


def _make_reconcile(repo: object, state_root: Path, install_spine: Path, reloaders: list) -> object:
    return ReconcileDesktopStateUseCase(
        state_repo=repo,  # type: ignore[arg-type]
        csg=_FakeCsg(),  # type: ignore[arg-type]
        weg=_FakeWeg(),  # type: ignore[arg-type]
        itr=_FakeItr(),  # type: ignore[arg-type]
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=_FakeMutex(),  # type: ignore[arg-type]
        reloaders=reloaders,  # type: ignore[arg-type]
    )


def test_hyprpaper_reloader_integration_with_hyprctl_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E2E: seed → apply → reconcile with HyprpaperReloader via a shim.

    The tmp ``state_root`` is passed EXPLICITLY — the constructor's env
    default would resolve the real ``$XDG_STATE_HOME/dotfiles``, find no
    ``current/`` symlinks, and vacuously succeed without ever invoking
    the shim.
    """
    bin_dir = tmp_path / "fakebin"
    marker = tmp_path / "hyprctl-called"
    _make_hyprpaper_shim(bin_dir, marker, exit_code=0)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert shutil.which("hyprctl") is not None, "fake hyprctl not in PATH"

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"e2e wallpaper for hyprpaper reload")

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

    reloader = HyprpaperReloader(state_root=state_root)
    use_case = _make_reconcile(repo, state_root, install_spine, [reloader])
    result = use_case.run()  # type: ignore[attr-defined]
    assert result.reload_failures == []
    assert marker.is_file(), "hyprctl shim was never invoked"
    calls = marker.read_text().splitlines()
    wallpaper_calls = [
        c for c in calls if len(c.split()) >= 3 and c.split()[0:2] == ["hyprpaper", "wallpaper"]
    ]
    assert wallpaper_calls, "no hyprpaper wallpaper request recorded"
    for call in wallpaper_calls:
        parts = call.split()
        segment = parts[2]
        monitor, _, path = segment.partition(",")
        assert monitor == "DP-1", "seeder default monitor expected"
        assert path.startswith("/"), "path must be absolute"
        assert Path(path).is_file(), "resolved target must be a real cache file"


def test_hyprpaper_reloader_integration_missing_hyprctl_is_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No discoverable ``hyprctl`` → surfaced failure, NOT a skip (R5)."""
    monkeypatch.setattr("runtime.adapters.hyprland_reloader.shutil.which", lambda *_a, **_k: None)
    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"wallpaper for missing-hyprctl case")

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

    reloader = HyprpaperReloader(hyprctl_path=None, state_root=state_root)
    use_case = _make_reconcile(repo, state_root, install_spine, [reloader])
    result = use_case.run()  # type: ignore[attr-defined]
    assert "HyprpaperReloader" in result.reload_failures


def test_hyprpaper_reloader_integration_shim_failure_is_surfaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``hyprctl`` present but the wallpaper request fails (exit 1, e.g.
    "failed to connect to hyprpaper (is it running?)") → surfaced failure."""
    bin_dir = tmp_path / "fakebin"
    marker = tmp_path / "hyprctl-called"
    _make_hyprpaper_shim(bin_dir, marker, exit_code=1)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    install_spine = tmp_path / "install"
    _setup_spine(install_spine)
    state_root = tmp_path / "state"
    repo = JsonStateRepository(state_root=state_root)
    img = tmp_path / "wall.png"
    img.write_bytes(b"wallpaper for shim-failure case")

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

    reloader = HyprpaperReloader(state_root=state_root)
    use_case = _make_reconcile(repo, state_root, install_spine, [reloader])
    result = use_case.run()  # type: ignore[attr-defined]
    assert "HyprpaperReloader" in result.reload_failures
