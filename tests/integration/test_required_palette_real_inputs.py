"""Real-input palette proof for BUG-001 / BUG-002 (developer integration, not a gate).

Runs the REAL ``csg`` on the two fixtures that produced null-palette
reconciles on 2026-10-26, inside SCRATCH state roots (never the live
``$XDG_STATE_HOME``).

BUG-002 makes the matrix success-required: 8K and 4K, cold (fresh state root)
and warm (a second fresh root immediately after the cold generation, so the
container image/page cache is warm), must all publish a complete palette.
A hard ``PaletteUnavailableError`` is a test failure, not an accepted row.

The opt-in forced-timeout regression runs the 8K fixture through
``CsgAdapter(timeout=2)``: the adapter must terminate the child gracefully,
verify the complete 8-artifact set, publish it, and leave no new container
(running or stopped) in the pre/post ``podman ps -a`` diff.

Requires ``DOTFILES_REAL_WALLPAPERS`` to name the directory holding
``yasuke-dark-7680x4320-21997.jpg`` and
``windows-95-dark-3840x2160-27097.jpg``. Skips loudly when absent.
The forced-timeout proof additionally requires
``DOTFILES_REAL_TIMEOUT_PROOF=1`` and a container-mode csg.

Re-run::

    DOTFILES_REAL_WALLPAPERS=$HOME/.local/share/dotfiles/wallpapers \
        uv run pytest -q -s tests/integration/test_required_palette_real_inputs.py
    DOTFILES_REAL_WALLPAPERS=$HOME/.local/share/dotfiles/wallpapers \
        DOTFILES_REAL_TIMEOUT_PROOF=1 \
        uv run pytest -q -s tests/integration/test_required_palette_real_inputs.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
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
FORCED_TIMEOUT_FIXTURE = "yasuke-dark-7680x4320-21997.jpg"


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


def _resolve_inputs(tmp_path: Path) -> tuple[Path, str, Path]:
    """Resolve (wallpaper dir, csg binary, templates dir) or skip loudly."""
    fixture_dir = _real_wallpapers_dir()
    if fixture_dir is None:
        pytest.skip(
            "DOTFILES_REAL_WALLPAPERS is not set to the directory holding the fixtures"
        )
    csg_bin = shutil.which("csg")
    if csg_bin is None:
        pytest.skip("csg not on PATH — real-input proof unavailable")
    templates = resolve_csg_templates_dir(csg_bin, tmp_path)
    if templates is None:
        pytest.skip("csg templates dir could not be resolved/dumped")
    return fixture_dir, csg_bin, templates


def _pipeline(
    tmp_path: Path,
    *,
    run_name: str,
    templates: Path,
    timeout: int | None = None,
) -> tuple[DerivationPipeline, Path]:
    install_spine = tmp_path / "install"
    spine_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    if not spine_templates.exists():
        spine_templates.parent.mkdir(parents=True)
        spine_templates.symlink_to(templates, target_is_directory=True)
    state_root = tmp_path / f"scratch-state-{run_name}"
    adapter = (
        CsgAdapter(templates_dir=templates)
        if timeout is None
        else CsgAdapter(timeout=timeout, templates_dir=templates)
    )
    pipeline = DerivationPipeline(
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        csg=adapter,
        weg=_UnusedWeg(),
        itr=_UnusedItr(),
        install_spine=install_spine,
    )
    return pipeline, state_root


def _assert_complete(entry: Any, state_root: Path) -> Path:
    palette_dir = state_root / "cache" / "palettes" / entry.entry_hash
    assert palette_dir.is_dir()
    for name in PALETTE_ARTIFACT_NAMES:
        assert (palette_dir / name).is_file(), f"missing palette artifact: {name}"
    return palette_dir


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_real_csg_palette_matrix_cold_warm(fixture_name: str, tmp_path: Path) -> None:
    """8K/4K x cold/warm are required successes (BUG-002 acceptance)."""
    fixture_dir, _csg_bin, templates = _resolve_inputs(tmp_path)
    fixture = fixture_dir / fixture_name
    if not fixture.is_file():
        pytest.skip(f"real fixture missing: {fixture}")

    wallpaper_hash = hash_file(fixture)
    observed: dict[str, tuple[Any, float]] = {}
    for mode in ("cold", "warm"):
        pipeline, state_root = _pipeline(
            tmp_path, run_name=f"{fixture_name}-{mode}", templates=templates
        )
        started = time.monotonic()
        try:
            entry, cache_hit = pipeline.ensure_palette(fixture, wallpaper_hash)
        except PaletteUnavailableError as exc:
            pytest.fail(
                f"REAL-INPUT matrix HARD FAIL fixture={fixture_name} mode={mode} "
                f"rc={exc.returncode!r} stderr={exc.stderr!r} error={exc}",
                pytrace=False,
            )
        elapsed = time.monotonic() - started
        palette_dir = _assert_complete(entry, state_root)
        assert cache_hit is False, f"{mode} row must generate, not hit the cache"
        assert entry.source_wallpaper_hash == wallpaper_hash
        observed[mode] = (entry, elapsed)
        print(
            f"REAL-INPUT matrix OK fixture={fixture_name} mode={mode} "
            f"entry={entry.entry_hash} elapsed={elapsed:.2f}s path={palette_dir}"
        )

    assert observed["cold"][0].entry_hash == observed["warm"][0].entry_hash


def _container_ids(engine: str) -> set[str]:
    result = subprocess.run(
        [engine, "ps", "-a", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{engine} ps -a failed: {result.stderr}"
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _csg_container_mode(csg_bin: str) -> tuple[bool, str | None]:
    result = subprocess.run(
        [csg_bin, "info"], capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        return False, None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False, None
    settings = payload.get("settings", {})
    mode = settings.get("runtime", {}).get("mode")
    engine = settings.get("container", {}).get("engine")
    return mode == "container", engine if isinstance(engine, str) else None


@pytest.mark.skipif(
    os.environ.get("DOTFILES_REAL_TIMEOUT_PROOF") != "1",
    reason="set DOTFILES_REAL_TIMEOUT_PROOF=1 to run the real-container forced-timeout proof",
)
def test_real_csg_forced_timeout_publishes_complete_palette_no_container_leak(
    tmp_path: Path,
) -> None:
    """A forced 2 s timeout keeps the complete palette and leaves no container."""
    fixture_dir, csg_bin, templates = _resolve_inputs(tmp_path)
    fixture = fixture_dir / FORCED_TIMEOUT_FIXTURE
    if not fixture.is_file():
        pytest.skip(f"real fixture missing: {fixture}")

    container_mode, engine = _csg_container_mode(csg_bin)
    if not container_mode:
        pytest.skip("csg is not in container mode on this host; container proof not applicable")
    assert engine is not None
    if shutil.which(engine) is None:
        pytest.skip(f"container engine not on PATH: {engine}")

    wallpaper_hash = hash_file(fixture)
    before = _container_ids(engine)
    pipeline, state_root = _pipeline(
        tmp_path, run_name="forced-timeout", templates=templates, timeout=2
    )

    error: BaseException | None = None
    entry: Any = None
    elapsed = 0.0
    leaked: list[str] = []
    started = time.monotonic()
    try:
        entry, cache_hit = pipeline.ensure_palette(fixture, wallpaper_hash)
    except BaseException as exc:  # noqa: BLE001 - re-raised after evidence capture
        error = exc
    finally:
        elapsed = time.monotonic() - started
        after = _container_ids(engine)
        leaked = sorted(after - before)
        print(
            f"REAL-INPUT timeout proof fixture={FORCED_TIMEOUT_FIXTURE} "
            f"timeout=2s elapsed={elapsed:.2f}s leaked_containers={leaked} "
            f"before={sorted(before)} after={sorted(after)}"
        )
        if leaked:
            # Clean up only this test's containers; the pre-existing evidence
            # container is never touched.
            subprocess.run(
                [engine, "rm", "-f", *leaked],
                capture_output=True,
                text=True,
                timeout=120,
            )

    if error is not None:
        pytest.fail(
            f"forced timeout did not publish the complete palette: {error!r} "
            f"(elapsed={elapsed:.2f}s)",
            pytrace=False,
        )
    assert cache_hit is False
    palette_dir = _assert_complete(entry, state_root)
    assert entry is not None and entry.source_wallpaper_hash == wallpaper_hash
    assert not leaked, f"forced timeout leaked containers: {leaked}"
    print(f"REAL-INPUT timeout proof OK entry={entry.entry_hash} path={palette_dir}")
