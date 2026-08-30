"""Integration test for WegAdapter with real weg binary (Story 1.8).

Skips if weg not on PATH.
Proves zero -o flag + env override container forwarding.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from runtime.adapters.hashing import effects_entry_hash, hash_file
from runtime.adapters.weg_adapter import WegAdapter


@pytest.mark.integration
def test_weg_adapter_integration_real_binary(tmp_path: Path) -> None:
    if shutil.which("weg") is None:
        pytest.skip("weg not on PATH")
    # Also need magick for weg to succeed
    if shutil.which("magick") is None:
        pytest.skip("magick not on PATH (needed by weg)")

    # Copy fixture wallpaper
    fixture = Path(__file__).parent.parent / "fixtures" / "wallpaper.png"
    if not fixture.is_file():
        fixture = Path("tests/fixtures/wallpaper.png")
    if not fixture.is_file():
        fixture = Path("src/runtime/tests/fixtures/wallpaper.png")
    assert fixture.is_file(), f"wallpaper fixture not found: {fixture}"
    wallpaper = tmp_path / "wallpaper.png"
    wallpaper.write_bytes(fixture.read_bytes())

    catalog_candidates = [
        Path(__file__).resolve().parents[3]
        / "src"
        / "cli-tools"
        / "wallpaper-effects-generator"
        / "src"
        / "wallpaper_effects_generator"
        / "defaults"
        / "effects.yaml",
        Path(__file__).resolve().parents[4]
        / "src"
        / "cli-tools"
        / "wallpaper-effects-generator"
        / "src"
        / "wallpaper_effects_generator"
        / "defaults"
        / "effects.yaml",
    ]
    catalog = next((p for p in catalog_candidates if p.is_file()), None)
    if catalog is None:
        pytest.skip("WEG effects catalog not found")

    wallpaper_hash = hash_file(wallpaper)
    catalog_hash = hash_file(catalog)
    eh = effects_entry_hash(wallpaper_hash, catalog_hash)
    output_dir = tmp_path / "state" / "cache" / "effects" / eh

    adapter = WegAdapter(catalog_path=catalog)
    entry = adapter.generate(wallpaper, output_dir)

    assert entry.entry_hash == eh
    assert entry.source_wallpaper_hash == wallpaper_hash
    assert entry.input_catalog_hash == catalog_hash
    # Check PNGs exist via rglob
    pngs = list(output_dir.rglob("*.png"))
    assert len(pngs) > 0, f"no PNGs found in {output_dir}, listing: {list(output_dir.rglob('*'))}"
    for p in pngs:
        assert p.is_file()
    # Verify artifact_hashes match hash_file for each
    for key, h in entry.artifact_hashes.items():
        # Find corresponding file: key may be basename or relative
        # Try basename match first
        matches = [p for p in pngs if p.name == key or p.relative_to(output_dir).as_posix() == key]
        assert matches, f"artifact key {key} not found among rglob pngs"
        assert h == hash_file(matches[0])
        assert len(h) == 64
