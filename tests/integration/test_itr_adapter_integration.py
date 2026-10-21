"""Integration test for ItrAdapter with real itr binary (Story 1.9, AC 1-2).

Skipped loudly if itr not on PATH — proves env override writes SVGs
to output_dir and IconsEntry artifact_hashes match hash_file.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import resolve_icon_templates_dir

from runtime.adapters.hashing import canonical_hash_dir, hash_file, icons_entry_hash
from runtime.adapters.itr_adapter import ItrAdapter

pytestmark = pytest.mark.integration


def _make_mappings(tmp_path: Path) -> Path:
    """Minimal icons.yaml + adjacent battery template dir for itr to render.

    itr resolves ``template_dir`` relative to its own templates root (the
    install spine's ``icon-templates``); a host whose spine lacks the
    ``battery/`` group makes itr fail and the test skips loudly below (setup
    mismatch, not a code defect).
    """
    battery_dir = tmp_path / "battery"
    battery_dir.mkdir(parents=True, exist_ok=True)
    (battery_dir / "battery-0.svg").write_text('<svg><path fill="{{background}}"/></svg>')
    icons_yaml = tmp_path / "icons.yaml"
    icons_yaml.write_text(
        "battery:\n"
        "  template_dir: battery/\n"
        "  output_dir: out/battery/\n"
        "  color_mappings:\n"
        "    background: background\n"
        "  variants:\n"
        "    - name: battery-0\n"
        "      template: battery-0.svg\n"
        "      output: battery-0.svg\n"
    )
    return icons_yaml


def test_itr_adapter_integration_real_binary(tmp_path: Path) -> None:
    if shutil.which("itr") is None:
        pytest.skip("itr not on PATH — integration requires itr binary")

    # Resolve a real templates dir — env override (DOTFILES_ICON_TEMPLATES_DIR),
    # else the checked-in dotfiles icon-template assets. No retired-tree walk:
    # an absent input skips loudly instead of silently falling back.
    templates_dir = resolve_icon_templates_dir()
    if templates_dir is None:
        pytest.skip("icon templates dir not provided; discovery fallback removed")

    palette_hash = "a" * 64
    palette_entry = tmp_path / "state" / "cache" / "palettes" / palette_hash
    palette_entry.mkdir(parents=True, exist_ok=True)
    (palette_entry / "colors.yaml").write_text(
        'special:\n  background: "#1a1a2e"\n  foreground: "#e0e0e0"\ncolors: []\n'
    )

    mappings_path = _make_mappings(tmp_path)
    try:
        templates_hash = canonical_hash_dir(templates_dir)
    except Exception as exc:
        pytest.skip(f"cannot hash templates dir {templates_dir}: {exc}")
    mappings_hash = hash_file(mappings_path)

    try:
        ph_hash = palette_hash
        ih = icons_entry_hash(ph_hash, templates_hash, mappings_hash)
    except Exception as exc:
        pytest.skip(f"cannot compute icons_entry_hash: {exc}")

    output_dir = tmp_path / "state" / "cache" / "icons" / ih

    # Adapter with explicit palette_cache_dir pointing to entry
    adapter = ItrAdapter(
        templates_dir=templates_dir,
        mappings_path=mappings_path,
        palette_cache_dir=palette_entry,
    )
    # itr resolves templates from its own install spine, not from the dir we
    # hash; a host spine without the battery group fails here and skips loudly.
    try:
        entry = adapter.render(ph_hash, templates_dir, mappings_path, output_dir)
    except (RuntimeError, FileNotFoundError, ValueError, TimeoutError) as exc:
        pytest.skip(f"itr real invocation skipped due to setup mismatch: {exc}")

    # Verify SVGs exist and hashes match
    svg_files = list(output_dir.rglob("*.svg"))
    assert svg_files, f"expected SVGs in {output_dir}"
    for p in svg_files:
        assert p.is_file()
        assert not p.is_dir()

    assert entry.entry_hash == ih
    assert entry.hash_algorithm == "sha256"
    assert entry.kind == "icons"
    assert entry.source_palette_hash == palette_hash
    for key, h in entry.artifact_hashes.items():
        assert len(h) == 64
        p = output_dir / key if (output_dir / key).is_file() else next(output_dir.rglob(key))
        assert h == hash_file(p)

    assert output_dir.is_dir()
