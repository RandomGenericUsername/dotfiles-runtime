"""Palette producer fingerprint: TPR keys are producer-specific (REQ-007)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

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


def test_engine_and_version_change_the_entry_key(tmp_path: Path) -> None:
    templates = _templates(tmp_path)
    native = TprAdapter(templates_dir=templates, engine="native", version="v0.1.0")
    matugen = TprAdapter(templates_dir=templates, engine="matugen", version="v0.1.0")
    newer = TprAdapter(templates_dir=templates, engine="native", version="v0.2.0")

    keys = {
        native.expected_entry_hash(_WALLPAPER_HASH),
        matugen.expected_entry_hash(_WALLPAPER_HASH),
        newer.expected_entry_hash(_WALLPAPER_HASH),
    }
    assert len(keys) == 3


@pytest.mark.parametrize("producer", ["csg", "bogus"])
def test_unregistered_producer_seed_fails_loud(
    tmp_path: Path, producer: str
) -> None:
    template_hash = canonical_hash_dir(_templates(tmp_path))
    with pytest.raises(ValueError, match="unknown palette producer"):
        palette_producer_seed(producer, template_hash)
