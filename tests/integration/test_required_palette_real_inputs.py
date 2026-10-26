"""Real-input palette proof for BUG-001 (developer integration, not a gate).

Runs the REAL ``csg`` on the two fixtures that produced null-palette
reconciles on 2026-10-26, inside a SCRATCH state root (never the live
``$XDG_STATE_HOME``). For each fixture it asserts the runtime either
persists a complete palette or hard-fails with a clean
``PaletteUnavailableError`` carrying the csg rc/stderr, and prints the
observed rc/stderr so the ticket evidence is traceable.

Requires ``DOTFILES_REAL_WALLPAPERS`` to name the directory holding
``yasuke-dark-7680x4320-21997.jpg`` and
``windows-95-dark-3840x2160-27097.jpg``. Skips loudly when absent.

Re-run::

    DOTFILES_REAL_WALLPAPERS=$HOME/.local/share/dotfiles/wallpapers \
        uv run pytest -q -s tests/integration/test_required_palette_real_inputs.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from conftest import resolve_csg_templates_dir

from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.application.derive import DerivationPipeline, PALETTE_ARTIFACT_NAMES
from runtime.domain.models import PaletteUnavailableError

pytestmark = pytest.mark.integration

FIXTURES = (
    "yasuke-dark-7680x4320-21997.jpg",
    "windows-95-dark-3840x2160-27097.jpg",
)


class _UnusedWeg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:  # pragma: no cover
        raise AssertionError("real-input palette proof must not invoke WEG")


class _UnusedItr:
    def render(self, *args: object, **kwargs: object) -> Any:  # pragma: no cover
        raise AssertionError("real-input palette proof must not invoke ITR")


def _real_wallpapers_dir() -> Path | None:
    raw = os.environ.get("DOTFILES_REAL_WALLPAPERS")
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    return candidate if candidate.is_dir() else None


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_real_csg_palette_or_clean_hard_error(
    fixture_name: str, tmp_path: Path
) -> None:
    fixture_dir = _real_wallpapers_dir()
    if fixture_dir is None:
        pytest.skip(
            "DOTFILES_REAL_WALLPAPERS is not set to the directory holding the "
            f"fixtures; cannot prove {fixture_name}"
        )
    fixture = fixture_dir / fixture_name
    if not fixture.is_file():
        pytest.skip(f"real fixture missing: {fixture}")

    csg_bin = shutil.which("csg")
    if csg_bin is None:
        pytest.skip("csg not on PATH — real-input proof unavailable")

    templates = resolve_csg_templates_dir(csg_bin, tmp_path)
    if templates is None:
        pytest.skip("csg templates dir could not be resolved/dumped")

    # DerivationPipeline resolves the csg templates from the install spine;
    # give the scratch run a private spine pointing at the resolved templates.
    install_spine = tmp_path / "install"
    spine_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    spine_templates.parent.mkdir(parents=True)
    spine_templates.symlink_to(templates, target_is_directory=True)

    state_root = tmp_path / "scratch-state"
    seeder = CacheSeeder(state_root)
    pipeline = DerivationPipeline(
        state_root=state_root,
        seeder=seeder,
        csg=CsgAdapter(templates_dir=templates),
        weg=_UnusedWeg(),
        itr=_UnusedItr(),
        install_spine=install_spine,
    )

    wallpaper_hash = hash_file(fixture)
    try:
        entry, cache_hit = pipeline.ensure_palette(fixture, wallpaper_hash)
    except PaletteUnavailableError as exc:
        # A csg failure is a SEPARATE issue; the runtime contract is that it
        # fails cleanly with traceable rc/stderr and never persists a palette.
        print(
            f"REAL-INPUT csg HARD FAIL fixture={fixture_name} rc={exc.returncode!r} "
            f"stderr={exc.stderr!r} path={exc.wallpaper_path} hash={exc.wallpaper_hash}"
        )
        assert exc.tool == "csg"
        assert exc.stage == "palette"
        assert exc.wallpaper_path == str(fixture)
        assert exc.wallpaper_hash == wallpaper_hash
        assert exc.stderr, "a palette failure must carry a stderr tail"
        assert exc.returncode is not None or "not on PATH" in exc.stderr
        return

    palette_dir = state_root / "cache" / "palettes" / entry.entry_hash
    print(
        f"REAL-INPUT csg OK fixture={fixture_name} rc=0 stderr='' "
        f"entry={entry.entry_hash} cache_hit={cache_hit} path={palette_dir}"
    )
    assert entry.source_wallpaper_hash == wallpaper_hash
    assert palette_dir.is_dir()
    for name in PALETTE_ARTIFACT_NAMES:
        assert (palette_dir / name).is_file(), f"missing palette artifact: {name}"
