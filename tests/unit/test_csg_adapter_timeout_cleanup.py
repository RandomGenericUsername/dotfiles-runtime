"""BUG-002 regression tests: a csg timeout is a bounded, clean failure.

The forced-timeout defect was: the adapter killed ``csg`` at the timeout,
raised ``PaletteUnavailableError`` even when all eight artifacts were already
written, never cleaned up the container, and the staging cleanup then deleted
the complete palette.

These tests use a real child process (a fake ``csg`` executable) rather than a
mocked ``subprocess.run`` so the timeout/termination path is exercised for real:
the child writes artifacts, hangs, and records the graceful signal it receives.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.hashing import canonical_hash_dir, hash_file, palette_entry_hash
from runtime.application.derive import PALETTE_ARTIFACT_NAMES, DerivationPipeline
from runtime.domain.models import PaletteUnavailableError

ALL_ARTIFACTS: dict[str, str] = {
    "colors.yaml": '{"background":"#000"}\n',
    "colors.conf": "$color0 = #000\n",
    "colors.gtk.css": "@define-color bg #000;\n",
    "colors.adw.css": "@define-color window_bg_bg #000;\n",
    "colors.sequences": "\x1b]4;0;#000\x1b\\",
    "colors.rasi": "* { background: #000; }\n",
    "colors.kitty": "* { background: #000; }",
    "colors.btop.theme": "theme[colors.main_bg]=#000000\n",
}


def _make_templates_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "templates"
    tdir.mkdir(parents=True)
    (tdir / "colors.yaml.j2").write_text("background: {{ bg }}\n")
    return tdir


def _copy_wallpaper_fixture(tmp_path: Path) -> Path:
    fixture = Path(__file__).parent.parent / "fixtures" / "wallpaper.png"
    assert fixture.is_file(), f"wallpaper fixture not found: {fixture}"
    dest = tmp_path / "wallpaper.png"
    dest.write_bytes(fixture.read_bytes())
    return dest


def _write_fake_csg(
    tmp_path: Path,
    *,
    artifacts: dict[str, str],
    ignore_sigint: bool = False,
    stderr_text: str = "",
) -> tuple[Path, Path]:
    """Create an executable fake csg that writes artifacts, then hangs.

    The fake records the first SIGINT/SIGTERM it receives in ``signaled`` so a
    test can prove the adapter attempted a graceful cleanup before SIGKILL.
    Returns ``(script, signaled_marker)``.
    """
    marker = tmp_path / "signaled.txt"
    script = tmp_path / "fake-csg"
    handler = ""
    if not ignore_sigint:
        handler = f"""
def _on_signal(signum, frame):
    Path({str(marker)!r}).write_text(str(signum), encoding="utf-8")
    raise SystemExit(128 + signum)
signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)
"""
    else:
        handler = "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
    script.write_text(
        f"""#!{sys.executable}
import os
import signal
import sys
import time
from pathlib import Path

out = Path(os.environ["COLORSCHEME__OUTPUT__DIRECTORY"])
out.mkdir(parents=True, exist_ok=True)
for name, text in {artifacts!r}.items():
    (out / name).write_text(text, encoding="utf-8")
if {stderr_text!r}:
    print({stderr_text!r}, file=sys.stderr, flush=True)
{handler}
while True:
    time.sleep(0.05)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script, marker


def _paths(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    wallpaper = _copy_wallpaper_fixture(tmp_path)
    templates_dir = _make_templates_dir(tmp_path)
    ph = palette_entry_hash(hash_file(wallpaper), canonical_hash_dir(templates_dir))
    output_dir = tmp_path / "state" / "cache" / "palettes" / ph
    return wallpaper, templates_dir, output_dir, ph


class _UnusedWeg:
    def generate(self, wallpaper_path: Path, output_dir: Path) -> Any:  # pragma: no cover
        raise AssertionError("palette timeout proof must not invoke WEG")


class _UnusedItr:
    def render(self, *args: object, **kwargs: object) -> Any:  # pragma: no cover
        raise AssertionError("palette timeout proof must not invoke ITR")


def _pipeline(tmp_path: Path, templates_dir: Path, csg: CsgAdapter) -> tuple[DerivationPipeline, Path]:
    from runtime.adapters.seeder import CacheSeeder

    state_root = tmp_path / "scratch-state"
    install_spine = tmp_path / "install"
    spine_templates = install_spine / "config" / "color-scheme-generator" / "templates"
    spine_templates.parent.mkdir(parents=True)
    spine_templates.symlink_to(templates_dir, target_is_directory=True)
    return (
        DerivationPipeline(
            state_root=state_root,
            seeder=CacheSeeder(state_root),
            csg=csg,
            weg=_UnusedWeg(),
            itr=_UnusedItr(),
            install_spine=install_spine,
        ),
        state_root,
    )


# ---------------------------------------------------------------------------
# Timeout ordering: the adapter must outlive csg's own 300 s cleanup window.
# ---------------------------------------------------------------------------


def test_default_timeout_is_above_csg_internal_timeout() -> None:
    from runtime.adapters.csg_adapter import CSG_INTERNAL_TIMEOUT_SECONDS

    assert CSG_INTERNAL_TIMEOUT_SECONDS == 300
    assert CsgAdapter.DEFAULT_TIMEOUT > CSG_INTERNAL_TIMEOUT_SECONDS
    assert CsgAdapter.DEFAULT_TIMEOUT >= CSG_INTERNAL_TIMEOUT_SECONDS + 30
    assert CsgAdapter()._timeout == CsgAdapter.DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# A complete palette written before the timeout is usable, never discarded.
# ---------------------------------------------------------------------------


def test_timeout_with_complete_artifacts_returns_palette_entry(tmp_path: Path) -> None:
    wallpaper, templates_dir, output_dir, ph = _paths(tmp_path)
    script, marker = _write_fake_csg(tmp_path, artifacts=ALL_ARTIFACTS)

    adapter = CsgAdapter(timeout=1, csg_bin=str(script), templates_dir=templates_dir)
    entry = adapter.generate(wallpaper, output_dir)

    assert entry.entry_hash == ph
    assert entry.source_wallpaper_hash == hash_file(wallpaper)
    artifact_keys = {
        "colors.yaml": "colors_yaml",
        "colors.conf": "colors_conf",
        "colors.gtk.css": "colors_gtk_css",
        "colors.adw.css": "colors_adw_css",
        "colors.sequences": "colors_sequences",
        "colors.rasi": "colors_rasi",
        "colors.kitty": "colors_kitty",
        "colors.btop.theme": "colors_btop",
    }
    for name in PALETTE_ARTIFACT_NAMES:
        assert (output_dir / name).is_file(), f"lost complete artifact: {name}"
        key = artifact_keys[name]
        assert entry.artifact_hashes[key] == hash_file(output_dir / name)  # type: ignore[literal-required]
    # The adapter attempted cleanup gracefully (SIGINT) before reaping; the
    # fake csg's handler simulates csg's own container-removal finally block.
    assert marker.is_file(), "timeout path did not signal the child for cleanup"
    assert marker.read_text(encoding="utf-8") == "2"


def test_timeout_with_complete_artifacts_publishes_through_staging(tmp_path: Path) -> None:
    wallpaper, templates_dir, _output_dir, _ph = _paths(tmp_path)
    script, marker = _write_fake_csg(tmp_path, artifacts=ALL_ARTIFACTS)
    pipeline, state_root = _pipeline(
        tmp_path,
        templates_dir,
        CsgAdapter(timeout=1, csg_bin=str(script), templates_dir=templates_dir),
    )

    entry, cache_hit = pipeline.ensure_palette(wallpaper, hash_file(wallpaper))

    assert cache_hit is False
    palette_dir = state_root / "cache" / "palettes" / entry.entry_hash
    for name in PALETTE_ARTIFACT_NAMES:
        assert (palette_dir / name).is_file(), f"published palette missing {name}"
    assert list((state_root / "cache").glob(".staging-*")) == []
    assert marker.is_file()


# ---------------------------------------------------------------------------
# An incomplete palette fails precisely and leaves no staging/partial entry.
# ---------------------------------------------------------------------------


def test_timeout_with_missing_artifact_raises_precise_error(tmp_path: Path) -> None:
    wallpaper, templates_dir, output_dir, _ph = _paths(tmp_path)
    incomplete = dict(ALL_ARTIFACTS)
    incomplete.pop("colors.kitty")
    script, _marker = _write_fake_csg(
        tmp_path,
        artifacts=incomplete,
        stderr_text="fake-csg partial output",
    )

    adapter = CsgAdapter(timeout=1, csg_bin=str(script), templates_dir=templates_dir)
    with pytest.raises(PaletteUnavailableError) as excinfo:
        adapter.generate(wallpaper, output_dir)

    exc = excinfo.value
    message = str(exc)
    assert "timed out" in message
    assert "incomplete" in message
    assert "colors.kitty" in message
    assert "fake-csg partial output" in exc.stderr
    assert exc.returncode is not None, "a timeout kill has a concrete termination status"
    assert exc.wallpaper_path == str(wallpaper)
    assert exc.wallpaper_hash == hash_file(wallpaper)
    assert exc.stage == "palette"
    assert exc.tool == "csg"


def test_timeout_ignored_sigint_escalates_and_still_fails_precisely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("runtime.adapters.csg_adapter._TERMINATION_GRACE_SECONDS", 0.2)
    wallpaper, templates_dir, output_dir, _ph = _paths(tmp_path)
    incomplete = dict(ALL_ARTIFACTS)
    incomplete.pop("colors.rasi")
    script, marker = _write_fake_csg(
        tmp_path,
        artifacts=incomplete,
        ignore_sigint=True,
    )

    adapter = CsgAdapter(timeout=1, csg_bin=str(script), templates_dir=templates_dir)
    with pytest.raises(PaletteUnavailableError) as excinfo:
        adapter.generate(wallpaper, output_dir)

    assert "SIGKILL" in str(excinfo.value)
    assert "colors.rasi" in str(excinfo.value)
    assert not marker.exists(), "the fake ignored SIGINT, so no handler ran"


def test_timeout_incomplete_leaves_no_staging_or_partial_palette(tmp_path: Path) -> None:
    wallpaper, templates_dir, _output_dir, _ph = _paths(tmp_path)
    incomplete = dict(ALL_ARTIFACTS)
    incomplete.pop("colors.btop.theme")
    script, _marker = _write_fake_csg(tmp_path, artifacts=incomplete)
    pipeline, state_root = _pipeline(
        tmp_path,
        templates_dir,
        CsgAdapter(timeout=1, csg_bin=str(script), templates_dir=templates_dir),
    )

    with pytest.raises(PaletteUnavailableError) as excinfo:
        pipeline.ensure_palette(wallpaper, hash_file(wallpaper))

    assert "colors.btop.theme" in str(excinfo.value)
    assert list((state_root / "cache").glob(".staging-*")) == []
    assert list((state_root / "cache" / "palettes").glob("*")) == []
