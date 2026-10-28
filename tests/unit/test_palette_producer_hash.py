"""Palette producer fingerprint: CSG key frozen, TPR key producer-specific."""

from __future__ import annotations

import hashlib
from pathlib import Path

from runtime.adapters.csg_adapter import CsgAdapter
from runtime.adapters.hashing import (
    canonical_hash_dir,
    palette_entry_hash,
    palette_entry_hash_for_producer,
    palette_producer_seed,
)
from runtime.adapters.tpr_adapter import TprAdapter

_WALLPAPER_HASH = "a" * 64


def _templates(tmp_path: Path) -> Path:
    tdir = tmp_path / "templates"
    tdir.mkdir(parents=True)
    (tdir / "a.j2").write_text("a\n")
    return tdir


def test_csg_key_is_byte_identical_to_legacy_formula(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    template_hash = canonical_hash_dir(templates)
    adapter = CsgAdapter(templates_dir=templates)

    legacy = hashlib.sha256((_WALLPAPER_HASH + template_hash).encode("utf-8")).hexdigest()
    assert adapter.expected_entry_hash(_WALLPAPER_HASH) == legacy
    assert adapter.expected_entry_hash(_WALLPAPER_HASH) == palette_entry_hash(
        _WALLPAPER_HASH, template_hash
    )
    assert palette_producer_seed("csg", template_hash) is None


def test_tpr_key_includes_engine_version_and_templates(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    template_hash = canonical_hash_dir(templates)
    adapter = TprAdapter(
        templates_dir=templates, engine="native", version="v0.1.0"
    )

    seed = hashlib.sha256(
        f"tpr:native:v0.1.0:{template_hash}".encode("utf-8")
    ).hexdigest()
    assert palette_producer_seed(
        "tpr", template_hash, engine="native", version="v0.1.0"
    ) == seed
    assert adapter.expected_entry_hash(_WALLPAPER_HASH) == palette_entry_hash(
        _WALLPAPER_HASH, template_hash, seed=seed
    )
    assert adapter.expected_entry_hash(_WALLPAPER_HASH) == palette_entry_hash_for_producer(
        "tpr", _WALLPAPER_HASH, template_hash, engine="native", version="v0.1.0"
    )


def test_producer_switch_cannot_hit_the_other_entry(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    csg = CsgAdapter(templates_dir=templates)
    tpr = TprAdapter(templates_dir=templates, engine="native", version="v0.1.0")

    assert csg.expected_entry_hash(_WALLPAPER_HASH) != tpr.expected_entry_hash(
        _WALLPAPER_HASH
    )
