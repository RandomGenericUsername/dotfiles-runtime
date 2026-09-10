"""Unit tests for the Phase 3 invalidation port + pure stale-set computation.

Zero I/O: all fixtures are in-memory hash strings and fakes. Covers
Story 1.1 ACs: port ABC contract, pure cascade helper, hashing regression pin.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

import pytest

from runtime.adapters.hashing import (
    HASH_ALGORITHM,
    effects_entry_hash,
    icons_entry_hash,
    palette_entry_hash,
)
from runtime.domain.invalidation import (
    CASCADE_DOWNSTREAM,
    LAYER_ORDER,
    DerivationLayer,
    close_stale_set,
    diff_input_hashes,
)
from runtime.ports.invalidation_query import IInvalidationQuery


def _full_warm() -> dict[DerivationLayer, str]:
    """Recorded == recomputed warm-cache input hashes (all fresh)."""
    return {
        "wallpapers": "aa" * 32,
        "palettes": "bb" * 32,
        "effects": "cc" * 32,
        "icons": "dd" * 32,
    }


class TestCloseStaleSet:
    def test_empty_stays_empty(self) -> None:
        assert close_stale_set(frozenset()) == frozenset()

    def test_accepts_mutable_set_and_does_not_mutate_input(self) -> None:
        src = {"wallpapers"}
        assert close_stale_set(src) == frozenset(  # type: ignore[arg-type]
            {"wallpapers", "palettes", "effects", "icons"}
        )
        assert src == {"wallpapers"}

    def test_overlapping_cascade_input(self) -> None:
        assert close_stale_set({"wallpapers", "palettes"}) == frozenset(  # type: ignore[arg-type]
            {"wallpapers", "palettes", "effects", "icons"}
        )

    def test_wallpaper_cascades_to_palette_effects_and_icons(self) -> None:
        assert close_stale_set(frozenset({"wallpapers"})) == frozenset(
            {"wallpapers", "palettes", "effects", "icons"}
        )

    def test_palette_cascades_to_icons_only(self) -> None:
        assert close_stale_set(frozenset({"palettes"})) == frozenset({"palettes", "icons"})

    def test_effects_has_no_downstream(self) -> None:
        assert close_stale_set(frozenset({"effects"})) == frozenset({"effects"})

    def test_icons_has_no_downstream(self) -> None:
        assert close_stale_set(frozenset({"icons"})) == frozenset({"icons"})

    def test_union_of_disjoint_inputs(self) -> None:
        assert close_stale_set(frozenset({"effects", "icons"})) == frozenset({"effects", "icons"})

    def test_unknown_layer_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="unknown derivation layer"):
            close_stale_set(frozenset({"bogus"}))  # type: ignore[arg-type]

    def test_case_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown derivation layer"):
            close_stale_set({"Wallpapers"})  # type: ignore[arg-type]


class TestDiffInputHashes:
    def test_identical_sets_are_fresh(self) -> None:
        warm = _full_warm()
        assert diff_input_hashes(warm, dict(warm)) == frozenset()

    def test_palette_input_change_is_directly_stale(self) -> None:
        recorded = _full_warm()
        recomputed = dict(recorded, palettes="ff" * 32)
        assert diff_input_hashes(recorded, recomputed) == frozenset({"palettes"})

    def test_catalog_change_stales_effects_only(self) -> None:
        recorded = _full_warm()
        recomputed = dict(recorded, effects="ff" * 32)
        assert diff_input_hashes(recorded, recomputed) == frozenset({"effects"})

    def test_mappings_change_stales_icons_only(self) -> None:
        recorded = _full_warm()
        recomputed = dict(recorded, icons="ff" * 32)
        assert diff_input_hashes(recorded, recomputed) == frozenset({"icons"})

    def test_missing_on_either_side_is_stale_not_fresh(self) -> None:
        recorded = _full_warm()
        recomputed = {k: v for k, v in recorded.items() if k != "palettes"}
        assert diff_input_hashes(recorded, recomputed) == frozenset({"palettes"})
        assert diff_input_hashes(recomputed, recorded) == frozenset({"palettes"})

    def test_none_hash_is_stale(self) -> None:
        recorded = _full_warm()
        recomputed: dict[DerivationLayer, str | None] = dict(recorded, effects=None)
        assert diff_input_hashes(recorded, recomputed) == frozenset({"effects"})

    def test_recorded_none_is_stale(self) -> None:
        recorded: dict[DerivationLayer, str | None] = dict(_full_warm(), palettes=None)
        assert diff_input_hashes(recorded, _full_warm()) == frozenset({"palettes"})

    def test_both_none_is_stale(self) -> None:
        assert diff_input_hashes({"effects": None}, {"effects": None}) == frozenset({"effects"})

    def test_empty_string_is_stale(self) -> None:
        recorded = _full_warm()
        assert diff_input_hashes(recorded, dict(recorded, effects="")) == frozenset({"effects"})
        assert diff_input_hashes(dict(recorded, effects=""), recorded) == frozenset({"effects"})

    def test_empty_compare_is_fresh(self) -> None:
        # Vacuous compare: nothing to compare, nothing stale (pinned; 1.2 always passes 4 layers).
        assert diff_input_hashes({}, {}) == frozenset()

    def test_non_string_hash_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid derivation input hash"):
            diff_input_hashes({"effects": 123}, {"effects": "cc" * 32})  # type: ignore[dict-item]

    def test_unknown_layer_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="unknown derivation layer"):
            diff_input_hashes({"bogus": "aa" * 32}, {"bogus": "aa" * 32})  # type: ignore[dict-item]

    def test_one_sided_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown derivation layer"):
            diff_input_hashes(_full_warm(), {"bogus": "aa" * 32})  # type: ignore[dict-item]


class TestStaleSetEndToEnd:
    """diff -> close pipeline: the exact behavior Story 1.3's report relies on."""

    def test_template_edit_stales_palette_plus_icons_cascade(self) -> None:
        recorded = _full_warm()
        recomputed = dict(recorded, palettes="ff" * 32)
        assert close_stale_set(diff_input_hashes(recorded, recomputed)) == frozenset(
            {"palettes", "icons"}
        )

    def test_wallpaper_change_stales_everything(self) -> None:
        recorded = _full_warm()
        recomputed = dict(recorded, wallpapers="ff" * 32)
        assert close_stale_set(diff_input_hashes(recorded, recomputed)) == frozenset(
            {"wallpapers", "palettes", "effects", "icons"}
        )


class _FakeInvalidationQuery(IInvalidationQuery):
    """Test double wiring the port contract to the domain-pure helpers."""

    def __init__(self, recorded: Mapping[DerivationLayer, str | None]) -> None:
        self._recorded = dict(recorded)

    def recompute_input_hashes(self) -> Mapping[DerivationLayer, str | None]:
        return dict(self._recorded)

    def compare_against_meta(
        self,
        recorded: Mapping[DerivationLayer, str | None],
        recomputed: Mapping[DerivationLayer, str | None],
    ) -> frozenset[DerivationLayer]:
        return close_stale_set(diff_input_hashes(recorded, recomputed))

    def stale_set_with_cascade(
        self,
        directly_stale: frozenset[DerivationLayer]
        | set[DerivationLayer]
        | Collection[DerivationLayer],
    ) -> frozenset[DerivationLayer]:
        return close_stale_set(directly_stale)


class TestPortContract:
    def test_abc_cannot_be_instantiated(self) -> None:
        with pytest.raises(TypeError):
            IInvalidationQuery()  # type: ignore[abstract]

    def test_fake_satisfies_contract_end_to_end(self) -> None:
        recorded = _full_warm()
        query = _FakeInvalidationQuery(recorded)
        assert query.recompute_input_hashes() == recorded
        recomputed = dict(recorded, palettes="ff" * 32)
        assert query.compare_against_meta(recorded, recomputed) == frozenset({"palettes", "icons"})
        assert query.stale_set_with_cascade({"effects"}) == frozenset({"effects"})

    def test_cascade_rule_matches_ad21(self) -> None:
        assert CASCADE_DOWNSTREAM["wallpapers"] == frozenset({"palettes", "effects"})
        assert CASCADE_DOWNSTREAM["palettes"] == frozenset({"icons"})
        assert CASCADE_DOWNSTREAM["effects"] == frozenset()
        assert CASCADE_DOWNSTREAM["icons"] == frozenset()

    def test_cascade_covers_every_layer(self) -> None:
        assert set(CASCADE_DOWNSTREAM) == set(LAYER_ORDER)
        assert dict(CASCADE_DOWNSTREAM) == {
            "wallpapers": frozenset({"palettes", "effects"}),
            "palettes": frozenset({"icons"}),
            "effects": frozenset(),
            "icons": frozenset(),
        }


class TestHashingRegressionPin:
    """AC 4: no hash formula or cache-layout constant altered by this story."""

    def test_algorithm_still_sha256(self) -> None:
        assert HASH_ALGORITHM == "sha256"

    def test_entry_hash_vectors_unchanged(self) -> None:
        assert (
            palette_entry_hash("ab" * 32, "cd" * 32)
            == "dcd7d37b4a82f5161c1ad6968b78c29b905c3af1fd251401ab26d45b6a9c9977"
        )
        assert (
            effects_entry_hash("ab" * 32, "ef" * 32)
            == "294630b1beb3e8021a638a3f8df7a1a416add4acc48427a496930f4abcee927a"
        )
        assert (
            icons_entry_hash("ab" * 32, "cd" * 32, "ef" * 32)
            == "1f4117ab2f1ebb3353562bc198ccd4311524328f648e27e331711a3aff50e4d3"
        )

    def test_palette_arg_order_tripwire(self) -> None:
        assert (
            palette_entry_hash("cd" * 32, "ab" * 32)
            != "dcd7d37b4a82f5161c1ad6968b78c29b905c3af1fd251401ab26d45b6a9c9977"
        )
