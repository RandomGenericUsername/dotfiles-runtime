"""Integration test for CsgAdapter with real csg binary (Story 1.7, AC 1-2).

Skipped loudly if csg not on PATH — proves env override writes three artifacts
to output_dir and PaletteEntry artifact_hashes match hash_file, even when
csg config says runtime.mode==container (container forwarding).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.hashing import canonical_hash_dir, hash_file, palette_entry_hash

pytestmark = pytest.mark.integration


def _find_templates_dir() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / "src"
            / "cli-tools"
            / "color-scheme-generator"
            / "src"
            / "color_scheme_generator"
            / "defaults"
            / "templates"
        )
        if candidate.is_dir():
            return candidate
        alt = parent / "src" / "cli-tools" / "color-scheme-generator" / "defaults" / "templates"
        if alt.is_dir():
            return alt
        # Walk from this file's ancestors up to repo root
        # Also try relative to runtime src
    # Fallback: search from repo root derived via runtime package location
    for parent in Path(__file__).resolve().parents:
        cand = (
            parent
            / "src"
            / "cli-tools"
            / "color-scheme-generator"
            / "src"
            / "color_scheme_generator"
            / "defaults"
            / "templates"
        )
        if cand.is_dir():
            return cand
    return None


def test_csg_adapter_integration_real_binary(tmp_path: Path) -> None:
    if shutil.which("csg") is None:
        pytest.skip("csg not on PATH — integration requires csg binary")

    # Reuse fixture wallpaper.png or create minimal one
    fixture = Path(__file__).parent.parent / "fixtures" / "wallpaper.png"
    if fixture.is_file():
        wallpaper = tmp_path / "wallpaper.png"
        wallpaper.write_bytes(fixture.read_bytes())
    else:
        # fallback minimal image via PIL-free helper not needed — pytest skip?
        pytest.skip("wallpaper fixture missing")

    templates_dir = _find_templates_dir()
    if templates_dir is None or not templates_dir.is_dir():
        # Use default discovery via adapter; let adapter find it
        templates_dir_for_hash = None
        # Compute hash via adapter-discovered dir for ph precalc
        # Instead, rely on adapter's internal discovery: we need ph for output_dir
        # So we must find templates_dir to compute ph; skip if not found
        pytest.skip(f"CSG templates dir not found: {templates_dir}")
    else:
        templates_dir_for_hash = templates_dir

    # Compute expected ph for output_dir validation
    if templates_dir_for_hash is not None:
        template_hash = canonical_hash_dir(templates_dir_for_hash)
        wallpaper_hash = hash_file(wallpaper)
        ph = palette_entry_hash(wallpaper_hash, template_hash)
        output_dir = tmp_path / "state" / "cache" / "palettes" / ph
        adapter = CsgAdapter(templates_dir=templates_dir_for_hash)
    else:
        # Let adapter discover — need to compute ph via adapter's discovered hash
        # We instantiate adapter without templates_dir and compute via its helper
        from runtime.adapters.csg_adapter import _find_default_templates_dir  # noqa: PLC0415

        discovered = _find_default_templates_dir()
        if discovered is None:
            pytest.skip("no default templates dir discovered")
        template_hash = canonical_hash_dir(discovered)
        wallpaper_hash = hash_file(wallpaper)
        ph = palette_entry_hash(wallpaper_hash, template_hash)
        output_dir = tmp_path / "state" / "cache" / "palettes" / ph
        adapter = CsgAdapter()

    entry = adapter.generate(wallpaper, output_dir)

    # Verify three files exist and hashes match
    for name in ("colors.yaml", "colors.conf", "colors.gtk.css"):
        p = output_dir / name
        assert p.is_file(), f"expected {name} in {output_dir}"
        assert not p.is_dir()

    assert entry.entry_hash == ph
    assert entry.hash_algorithm == "sha256"
    assert entry.kind == "palette"
    assert entry.source_wallpaper_hash == hash_file(wallpaper)
    # Artifact hashes match binary hash_file
    assert entry.artifact_hashes["colors_yaml"] == hash_file(output_dir / "colors.yaml")
    assert entry.artifact_hashes["colors_conf"] == hash_file(output_dir / "colors.conf")
    assert entry.artifact_hashes["colors_gtk_css"] == hash_file(output_dir / "colors.gtk.css")
    # Output dir under state_root/cache/palettes
    assert output_dir.is_dir()
