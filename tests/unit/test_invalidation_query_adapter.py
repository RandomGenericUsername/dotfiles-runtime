"""Unit tests for the Story 1.2 spine input-hash walk adapter.

Zero tools, zero network: `tmp_path` fixture spine (templates dir, catalog
file, icon templates, mappings file) + fixture cache entries with `meta.json`.
Covers Story 1.2 ACs: hash-walk + compare mechanics, canonicalization parity,
pinned conventions (wallpapers exclusion, missing/corrupt meta, boundary).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.adapters.hashing import canonical_hash_dir, hash_file
from runtime.adapters.invalidation import InvalidationQueryAdapter
from runtime.domain.invalidation import DerivationLayer

PALETTE_HASH = "aa" * 32
EFFECTS_HASH = "bb" * 32
ICONS_HASH = "cc" * 32


def _make_spine(base: Path) -> dict[str, Path]:
    """Build a fixture install spine: templates dir, catalog, icon inputs."""
    templates = base / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "a.j2").write_text("template-a", encoding="utf-8")
    (templates / "b.j2").write_text("template-b", encoding="utf-8")
    catalog = base / "effects.yaml"
    catalog.write_text("effects: []", encoding="utf-8")
    icon_templates = base / "icon-templates"
    icon_templates.mkdir(parents=True, exist_ok=True)
    (icon_templates / "icon.svg").write_text("<svg/>", encoding="utf-8")
    icon_mappings = base / "icons.yaml"
    icon_mappings.write_text("mappings: {}", encoding="utf-8")
    return {
        "templates": templates,
        "catalog": catalog,
        "icon_templates": icon_templates,
        "icon_mappings": icon_mappings,
    }


def _write_meta(state_root: Path, layer: str, entry_hash: str, fields: dict[str, str]) -> Path:
    """Write a minimal per-layer meta.json for a fixture cache entry."""
    entry_dir = state_root / "cache" / layer / entry_hash
    entry_dir.mkdir(parents=True, exist_ok=True)
    meta = {"hash_algorithm": "sha256", "kind": layer.rstrip("s"), **fields}
    meta_path = entry_dir / "meta.json"
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return entry_dir


def _make_adapter(
    tmp_path: Path, **overrides: Path | None
) -> tuple[InvalidationQueryAdapter, dict[str, Path]]:
    """Build adapter + spine; pass None for any input to simulate absence."""
    spine = _make_spine(tmp_path / "spine")
    state_root = tmp_path / "state"
    kwargs: dict[str, Path | None] = {
        "templates_dir": spine["templates"],
        "catalog_path": spine["catalog"],
        "icon_templates": spine["icon_templates"],
        "icon_mappings": spine["icon_mappings"],
    }
    kwargs.update(overrides)
    adapter = InvalidationQueryAdapter(state_root=state_root, **kwargs)  # type: ignore[arg-type]
    return adapter, spine


def _seed_warm_entries(
    adapter: InvalidationQueryAdapter, state_root: Path
) -> dict[DerivationLayer, str]:
    """Write fixture entries matching current spine hashes; return recorded map."""
    recomputed = adapter.recompute_input_hashes()
    assert recomputed["palettes"] is not None
    assert recomputed["effects"] is not None
    assert recomputed["icons"] is not None
    templates_hash, mappings_hash = recomputed["icons"].split("\x00", 1)
    assert isinstance(recomputed["palettes"], str)
    assert isinstance(recomputed["effects"], str)
    _write_meta(
        state_root,
        "palettes",
        PALETTE_HASH,
        {"entry_hash": PALETTE_HASH, "input_template_hash": recomputed["palettes"]},
    )
    _write_meta(
        state_root,
        "effects",
        EFFECTS_HASH,
        {"entry_hash": EFFECTS_HASH, "input_catalog_hash": recomputed["effects"]},
    )
    _write_meta(
        state_root,
        "icons",
        ICONS_HASH,
        {
            "entry_hash": ICONS_HASH,
            "input_templates_hash": templates_hash,
            "input_mappings_hash": mappings_hash,
        },
    )
    return {
        "palettes": adapter.recorded_inputs("palettes", PALETTE_HASH),  # type: ignore[dict-item]
        "effects": adapter.recorded_inputs("effects", EFFECTS_HASH),  # type: ignore[dict-item]
        "icons": adapter.recorded_inputs("icons", ICONS_HASH),  # type: ignore[dict-item]
    }


class TestWarmCache:
    def test_matching_inputs_are_fresh(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        recorded = _seed_warm_entries(adapter, adapter._state_root)
        assert (
            adapter.compare_against_meta(recorded, adapter.recompute_input_hashes()) == frozenset()
        )

    def test_canonicalization_parity_with_hashing_helpers(self, tmp_path: Path) -> None:
        adapter, spine = _make_adapter(tmp_path)
        recomputed = adapter.recompute_input_hashes()
        assert recomputed["palettes"] == canonical_hash_dir(spine["templates"])
        assert recomputed["effects"] == hash_file(spine["catalog"])
        assert recomputed["icons"] == (
            f"{canonical_hash_dir(spine['icon_templates'])}\x00{hash_file(spine['icon_mappings'])}"
        )


class TestStaleDetection:
    def test_template_edit_stales_palette(self, tmp_path: Path) -> None:
        adapter, spine = _make_adapter(tmp_path)
        recorded = _seed_warm_entries(adapter, adapter._state_root)
        (spine["templates"] / "c.j2").write_text("template-c", encoding="utf-8")
        stale = adapter.compare_against_meta(recorded, adapter.recompute_input_hashes())
        assert stale == frozenset({"palettes", "icons"})

    def test_catalog_edit_stales_effects_only(self, tmp_path: Path) -> None:
        adapter, spine = _make_adapter(tmp_path)
        recorded = _seed_warm_entries(adapter, adapter._state_root)
        spine["catalog"].write_text("effects: [blur]", encoding="utf-8")
        stale = adapter.compare_against_meta(recorded, adapter.recompute_input_hashes())
        assert stale == frozenset({"effects"})

    def test_mappings_edit_stales_icons_only(self, tmp_path: Path) -> None:
        adapter, spine = _make_adapter(tmp_path)
        recorded = _seed_warm_entries(adapter, adapter._state_root)
        spine["icon_mappings"].write_text("mappings: {a: b}", encoding="utf-8")
        stale = adapter.compare_against_meta(recorded, adapter.recompute_input_hashes())
        assert stale == frozenset({"icons"})

    def test_absent_spine_input_is_stale(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path, templates_dir=None)
        recorded = {
            "palettes": "bb" * 32,
            "effects": "cc" * 32,
            "icons": "dd" * 32,
        }
        stale = adapter.compare_against_meta(recorded, adapter.recompute_input_hashes())  # type: ignore[dict-item]
        assert stale == frozenset({"palettes", "effects", "icons"})

    @pytest.mark.parametrize(
        ("absent_kwarg", "expected_stale"),
        [
            ("templates_dir", frozenset({"palettes", "icons"})),
            ("catalog_path", frozenset({"effects"})),
            ("icon_templates", frozenset({"icons"})),
            ("icon_mappings", frozenset({"icons"})),
        ],
    )
    def test_absent_input_parametrized(
        self, tmp_path: Path, absent_kwarg: str, expected_stale: frozenset[str]
    ) -> None:
        full, _ = _make_adapter(tmp_path)
        recorded = _seed_warm_entries(full, full._state_root)
        adapter, _ = _make_adapter(tmp_path, **{absent_kwarg: None})  # type: ignore[arg-type]
        stale = adapter.compare_against_meta(recorded, adapter.recompute_input_hashes())
        assert stale == expected_stale


class TestRecordedInputs:
    def test_missing_meta_returns_none(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        assert adapter.recorded_inputs("palettes", PALETTE_HASH) is None

    def test_corrupt_meta_raises_loudly(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        entry_dir = adapter._state_root / "cache" / "palettes" / PALETTE_HASH
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / "meta.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match=f"corrupt meta.json for palettes/{PALETTE_HASH}"):
            adapter.recorded_inputs("palettes", PALETTE_HASH)

    def test_unknown_layer_raises(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        with pytest.raises(ValueError, match="unknown derivation layer"):
            adapter.recorded_inputs("bogus", PALETTE_HASH)  # type: ignore[arg-type]

    def test_artifact_mismatch_is_not_staleness(self, tmp_path: Path) -> None:
        """Boundary pin: corrupt artifact_hashes with matching inputs is FRESH here.

        Artifact-hash mismatch is corrupt-by-digest (Story 1.5 / doctor), never
        staleness. recorded_inputs must ignore artifact_hashes entirely.
        """
        adapter, _ = _make_adapter(tmp_path)
        _seed_warm_entries(adapter, adapter._state_root)
        recomputed = adapter.recompute_input_hashes()
        assert recomputed["palettes"] is not None
        _write_meta(
            adapter._state_root,
            "palettes",
            PALETTE_HASH,
            {
                "entry_hash": PALETTE_HASH,
                "input_template_hash": recomputed["palettes"],
                "artifact_hashes": {"colors.yaml": "ff" * 32},
            },
        )
        recorded = {
            "palettes": adapter.recorded_inputs("palettes", PALETTE_HASH),  # type: ignore[dict-item]
            "effects": adapter.recorded_inputs("effects", EFFECTS_HASH),  # type: ignore[dict-item]
            "icons": adapter.recorded_inputs("icons", ICONS_HASH),  # type: ignore[dict-item]
        }
        assert adapter.compare_against_meta(recorded, recomputed) == frozenset()

    def test_wallpapers_diffs_ignored(self, tmp_path: Path) -> None:
        """Pinned convention: content-addressed layer never stale-by-input."""
        adapter, _ = _make_adapter(tmp_path)
        recorded = {"wallpapers": "aa" * 32, "palettes": "bb" * 32}
        recomputed = {"wallpapers": "ff" * 32, "palettes": "bb" * 32}
        assert adapter.compare_against_meta(recorded, recomputed) == frozenset()  # type: ignore[dict-item]
        recomputed2 = {"wallpapers": "ff" * 32, "palettes": "00" * 32}
        assert adapter.compare_against_meta(recorded, recomputed2) == frozenset(  # type: ignore[dict-item]
            {"palettes", "icons"}
        )

    def test_source_hashes_ignored(self, tmp_path: Path) -> None:
        """Convention 4 pin: divergent source_* with matching input_* stays fresh."""
        adapter, _ = _make_adapter(tmp_path)
        recorded = _seed_warm_entries(adapter, adapter._state_root)
        (adapter._state_root / "cache" / "palettes" / PALETTE_HASH / "meta.json").write_text(
            json.dumps(
                {
                    "input_template_hash": recorded["palettes"],
                    "source_wallpaper_hash": "ff" * 32,
                }
            ),
            encoding="utf-8",
        )
        (adapter._state_root / "cache" / "icons" / ICONS_HASH / "meta.json").write_text(
            json.dumps(
                {
                    "input_templates_hash": "ab" * 32,
                    "input_mappings_hash": "cd" * 32,
                    "source_palette_hash": "ff" * 32,
                }
            ),
            encoding="utf-8",
        )
        recorded2 = {
            "palettes": adapter.recorded_inputs("palettes", PALETTE_HASH),  # type: ignore[dict-item]
            "effects": adapter.recorded_inputs("effects", EFFECTS_HASH),  # type: ignore[dict-item]
            "icons": adapter.recorded_inputs("icons", ICONS_HASH),  # type: ignore[dict-item]
        }
        assert adapter.compare_against_meta(recorded2, adapter.recompute_input_hashes()) == (
            frozenset({"icons"})
        )

    def test_icons_half_missing_field_is_none(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        _write_meta(
            adapter._state_root,
            "icons",
            ICONS_HASH,
            {"entry_hash": ICONS_HASH, "input_templates_hash": "ab" * 32},
        )
        assert adapter.recorded_inputs("icons", ICONS_HASH) is None

    def test_non_dict_meta_raises(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        entry_dir = adapter._state_root / "cache" / "palettes" / PALETTE_HASH
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / "meta.json").write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="corrupt meta.json"):
            adapter.recorded_inputs("palettes", PALETTE_HASH)

    def test_null_fields_are_none(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        _write_meta(
            adapter._state_root,
            "effects",
            EFFECTS_HASH,
            {"entry_hash": EFFECTS_HASH, "input_catalog_hash": None},  # type: ignore[dict-value]
        )
        assert adapter.recorded_inputs("effects", EFFECTS_HASH) is None

    def test_missing_field_is_none(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        _write_meta(adapter._state_root, "palettes", PALETTE_HASH, {"entry_hash": PALETTE_HASH})
        assert adapter.recorded_inputs("palettes", PALETTE_HASH) is None

    def test_wrong_type_field_is_none(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        _write_meta(
            adapter._state_root,
            "effects",
            EFFECTS_HASH,
            {"entry_hash": EFFECTS_HASH, "input_catalog_hash": 123},  # type: ignore[dict-value]
        )
        assert adapter.recorded_inputs("effects", EFFECTS_HASH) is None

    def test_neither_dir_nor_file_raises(self, tmp_path: Path) -> None:
        from runtime.adapters.invalidation import _hash_path_input

        with pytest.raises(RuntimeError, match="neither a dir nor a file"):
            _hash_path_input(tmp_path / "absent", "icon mappings")

    def test_wrong_kind_path_raises(self, tmp_path: Path) -> None:
        from runtime.adapters.invalidation import _hash_dir_input, _hash_file_input

        adapter, spine = _make_adapter(tmp_path)
        with pytest.raises(RuntimeError, match="must be a dir"):
            _hash_dir_input(spine["catalog"], "csg templates")
        with pytest.raises(RuntimeError, match="must be a file"):
            _hash_file_input(spine["templates"], "weg catalog")
        _ = adapter

    def test_all_none_spine_recomputes_none(self, tmp_path: Path) -> None:
        adapter = InvalidationQueryAdapter(
            state_root=tmp_path / "state",
            templates_dir=None,
            catalog_path=None,
            icon_templates=None,
            icon_mappings=None,
        )
        assert adapter.recompute_input_hashes() == {
            "wallpapers": None,
            "palettes": None,
            "effects": None,
            "icons": None,
        }

    def test_invalid_entry_hash_raises(self, tmp_path: Path) -> None:
        adapter, _ = _make_adapter(tmp_path)
        with pytest.raises(ValueError, match="64-char"):
            adapter.recorded_inputs("palettes", "zzz")

    def test_icon_templates_dir_edit_stales_icons(self, tmp_path: Path) -> None:
        adapter, spine = _make_adapter(tmp_path)
        recorded = _seed_warm_entries(adapter, adapter._state_root)
        (spine["icon_templates"] / "other.svg").write_text("<svg/>", encoding="utf-8")
        stale = adapter.compare_against_meta(recorded, adapter.recompute_input_hashes())
        assert stale == frozenset({"icons"})
