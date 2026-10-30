"""REQ-005 developer proof: TprAdapter generates via the container runtime.

This is an integration proof (real ``tpr`` + a built engine image), not a
stock-runner gate. It is skipped honestly when the CLI, container engine,
image, or provisioned spine templates are absent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from runtime.adapters.hashing import hash_file
from runtime.adapters.tpr_adapter import TprAdapter
from runtime.domain.models import PALETTE_ARTIFACT_NAMES

pytestmark = pytest.mark.integration


def _engine() -> str | None:
    for candidate in ("podman", "docker"):
        if shutil.which(candidate):
            return candidate
    return None


def _image_present(engine: str, image: str) -> bool:
    result = subprocess.run(
        [engine, "image", "exists", image],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return True
    listed = subprocess.run(
        [engine, "images", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return listed.returncode == 0 and image in listed.stdout.splitlines()


def _templates_dir() -> Path | None:
    candidates = [
        Path(os.environ.get("DOTFILES_INSTALL_SPINE", "")) / "config/theme-producer/templates",
        Path(
            os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
        )
        / "dotfiles/config/theme-producer/templates",
    ]
    return next((path for path in candidates if path.is_dir() and any(path.iterdir())), None)


def test_tpr_container_generates_eight_artifacts(tmp_path: Path) -> None:
    tpr_bin = shutil.which("tpr")
    if tpr_bin is None:
        pytest.skip("tpr is not installed")
    engine = _engine()
    if engine is None:
        pytest.skip("no podman/docker container engine")
    # The provisioned settings pin the tag; accept the common installed tag.
    if not any(_image_present(engine, f"localhost/tpr-native-{engine}:{tag}") for tag in ("v0.4.0",)):
        pytest.skip("tpr-native container image is not built at the pinned tag")
    templates = _templates_dir()
    if templates is None:
        pytest.skip("provisioned tpr templates dir is absent")

    settings = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    ) / "theme-producer/settings.toml"
    adapter = TprAdapter(
        tpr_bin=tpr_bin,
        templates_dir=templates,
        engine="native",
        runtime="container",
        settings_path=settings if settings.is_file() else None,
    )
    wallpaper = Path(__file__).resolve().parents[1] / "fixtures" / "wallpaper.png"
    wallpaper_hash = hash_file(wallpaper)
    output_dir = (
        tmp_path
        / "state"
        / "cache"
        / "palettes"
        / adapter.expected_entry_hash(wallpaper_hash)
    )

    entry = adapter.generate(wallpaper, output_dir)

    assert entry.entry_hash == output_dir.name
    assert entry.source_wallpaper_hash == wallpaper_hash
    assert sorted(path.name for path in output_dir.iterdir()) == sorted(PALETTE_ARTIFACT_NAMES)
    assert not (output_dir / "theme.json").exists()
    for name in PALETTE_ARTIFACT_NAMES:
        assert (output_dir / name).is_file(), f"missing artifact {name}"
