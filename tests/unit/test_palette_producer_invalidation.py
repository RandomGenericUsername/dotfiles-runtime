"""Invalidation recomputes the active producer's palette input hash."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.adapters.csg_adapter import CsgAdapter
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


def test_csg_recomputed_matches_adapter(tmp_path: Path) -> None:
    templates = _templates(tmp_path, "csg")
    csg = CsgAdapter(templates_dir=templates)
    adapter = InvalidationQueryAdapter(
        state_root=tmp_path / "state", palette_producer=csg
    )

    recomputed = adapter.recompute_input_hashes()

    assert recomputed["palettes"] == csg.template_set_hash()


def test_tpr_recomputed_matches_adapter(tmp_path: Path) -> None:
    templates = _templates(tmp_path, "tpr")
    tpr = TprAdapter(templates_dir=templates, engine="native", version="v0.1.0")
    adapter = InvalidationQueryAdapter(
        state_root=tmp_path / "state", palette_producer=tpr
    )

    recomputed = adapter.recompute_input_hashes()

    assert recomputed["palettes"] == tpr.template_set_hash()


def test_active_producer_controls_staleness(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    csg_templates = _templates(tmp_path, "csg")
    tpr_templates = _templates(tmp_path, "tpr")
    csg = CsgAdapter(templates_dir=csg_templates)
    tpr = TprAdapter(templates_dir=tpr_templates, engine="native", version="v0.1.0")
    _write_palette_meta(state_root, "a" * 64, csg.template_set_hash())

    csg_check = InvalidationQueryAdapter(state_root=state_root, palette_producer=csg)
    tpr_check = InvalidationQueryAdapter(state_root=state_root, palette_producer=tpr)

    csg_stale = csg_check.compare_against_meta(
        {"palettes": csg_check.recorded_inputs("palettes", "a" * 64)},
        csg_check.recompute_input_hashes(),
    )
    tpr_stale = tpr_check.compare_against_meta(
        {"palettes": tpr_check.recorded_inputs("palettes", "a" * 64)},
        tpr_check.recompute_input_hashes(),
    )

    assert "palettes" not in csg_stale
    assert "palettes" in tpr_stale


def test_absent_producer_templates_is_none(tmp_path: Path) -> None:
    tpr = TprAdapter(templates_dir=None, engine="native", version="v0.1.0")
    adapter = InvalidationQueryAdapter(
        state_root=tmp_path / "state", palette_producer=tpr
    )
    assert adapter.recompute_input_hashes()["palettes"] is None
