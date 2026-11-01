"""Invalidation recomputes the active producer's palette input hash."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.adapters.invalidation import InvalidationQueryAdapter
from runtime.adapters.tpr_adapter import TprAdapter


def _templates(root: Path, marker: str) -> Path:
    tdir = root / marker
    tdir.mkdir(parents=True)
    (tdir / "a.j2").write_text(marker, encoding="utf-8")
    return tdir


def _write_palette_meta(state_root: Path, entry_hash: str, template_hash: str) -> None:
    entry = state_root / "cache" / "palettes" / entry_hash
    entry.mkdir(parents=True)
    (entry / "meta.json").write_text(
        json.dumps(
            {
                "hash_algorithm": "sha256",
                "kind": "palette",
                "entry_hash": entry_hash,
                "input_template_hash": template_hash,
            }
        ),
        encoding="utf-8",
    )


def test_recomputed_matches_active_producer(tmp_path: Path) -> None:
    templates = _templates(tmp_path, "tpr")
    tpr = TprAdapter(templates_dir=templates, engine="native", version="v0.1.0")
    adapter = InvalidationQueryAdapter(
        state_root=tmp_path / "state", palette_producer=tpr
    )

    recomputed = adapter.recompute_input_hashes()

    assert recomputed["palettes"] == tpr.template_set_hash()


def test_template_change_stales_the_palette(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    templates = _templates(tmp_path, "tpr")
    tpr = TprAdapter(templates_dir=templates, engine="native", version="v0.1.0")
    _write_palette_meta(state_root, "a" * 64, tpr.template_set_hash())

    adapter = InvalidationQueryAdapter(state_root=state_root, palette_producer=tpr)

    def _palette_stale() -> bool:
        recorded = {"palettes": adapter.recorded_inputs("palettes", "a" * 64)}
        return "palettes" in adapter.compare_against_meta(
            recorded, adapter.recompute_input_hashes()
        )

    assert not _palette_stale()
    (templates / "b.j2").write_text("changed", encoding="utf-8")
    assert _palette_stale()


def test_absent_producer_templates_is_none(tmp_path: Path) -> None:
    tpr = TprAdapter(templates_dir=None, engine="native", version="v0.1.0")
    adapter = InvalidationQueryAdapter(
        state_root=tmp_path / "state", palette_producer=tpr
    )
    assert adapter.recompute_input_hashes()["palettes"] is None
