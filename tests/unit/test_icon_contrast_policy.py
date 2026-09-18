"""Unit tests for the pure icon-contrast preference policy (task 1.1).

Roundtrip, the precedence table (flag > store > default), absent ⇒ None,
and strict rejection of malformed shapes.
"""

from __future__ import annotations

import pytest

from runtime.domain.icon_contrast_policy import (
    describe,
    is_wallpaper_hash,
    lookup,
    parse,
    resolve,
    serialize,
)

H1 = "a" * 64
H2 = "B" * 64  # uppercase accepted, normalized to lowercase


class TestParseSerializeRoundtrip:
    def test_empty_prefs_roundtrip(self) -> None:
        assert parse(serialize({})) == {}

    def test_roundtrip_preserves_choices(self) -> None:
        prefs = {H1: True, "c" * 64: False}
        assert parse(serialize(prefs)) == prefs

    def test_serialize_sorts_keys_and_ends_with_newline(self) -> None:
        text = serialize({"f" * 64: True, "0" * 64: False})
        assert text.endswith("\n")
        assert text.index("0" * 64) < text.index("f" * 64)

    def test_parse_tolerates_whitespace_and_key_order(self) -> None:
        text = f'{{ "prefs" : {{ "{H1}" : false , "{"c" * 64}" : true }} , "version" : 1 }}'
        assert parse(text) == {H1: False, "c" * 64: True}

    def test_parse_absent_version_defaults_to_1(self) -> None:
        assert parse(f'{{"prefs": {{"{H1}": true}}}}') == {H1: True}


class TestParseRejectsMalformed:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "not json",
            "[]",
            '{"version": 1}',  # missing prefs
            '{"version": 2, "prefs": {}}',  # unknown version
            '{"version": 1, "prefs": {"short": true}}',  # bad key
            f'{{"version": 1, "prefs": {{"{"a" * 64}": "yes"}}}}',  # non-bool
            f'{{"version": 1, "prefs": {{"{"a" * 64}": true}}, trailing',
            'garbage {"version": 1, "prefs": {}}',
        ],
    )
    def test_malformed_raises_value_error(self, text: str) -> None:
        with pytest.raises(ValueError):
            parse(text)

    def test_serialize_rejects_bad_key(self) -> None:
        with pytest.raises(ValueError):
            serialize({"nope": True})

    def test_serialize_rejects_non_bool(self) -> None:
        with pytest.raises(ValueError):
            serialize({H1: "true"})  # type: ignore[dict-item]


class TestLookup:
    def test_absent_hash_returns_none(self) -> None:
        assert lookup({}, H1) is None
        assert lookup({H1: True}, "d" * 64) is None

    def test_present_hash_returns_choice(self) -> None:
        assert lookup({H1: False}, H1) is False
        assert lookup({H1: True}, H1) is True


class TestResolvePrecedence:
    def test_flag_on_forces_enabled_from_flag(self) -> None:
        assert resolve(flag="on", stored=False) == (True, "flag")

    def test_flag_off_forces_disabled_from_flag(self) -> None:
        assert resolve(flag="off", stored=True) == (False, "flag")

    def test_auto_follows_store(self) -> None:
        assert resolve(flag="auto", stored=False) == (False, "store")
        assert resolve(flag="auto", stored=True) == (True, "store")

    def test_auto_absent_entry_defaults_on(self) -> None:
        assert resolve(flag="auto", stored=None) == (True, "default")

    def test_auto_absent_entry_honors_explicit_default(self) -> None:
        assert resolve(flag="auto", stored=None, default=False) == (False, "default")

    def test_unknown_flag_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="invalid contrast flag"):
            resolve(flag="sometimes", stored=None)


class TestDescribe:
    def test_store_off_uses_via_wording(self) -> None:
        assert describe(enabled=False, source="store") == (
            "icons rendered with guard OFF via per-wallpaper preference"
        )

    def test_store_on(self) -> None:
        assert describe(enabled=True, source="store") == (
            "icons rendered with guard ON (per-wallpaper preference)"
        )

    def test_flag_and_default(self) -> None:
        assert describe(enabled=True, source="flag") == (
            "icons rendered with guard ON (explicit --contrast flag)"
        )
        assert describe(enabled=False, source="flag") == (
            "icons rendered with guard OFF (explicit --contrast flag)"
        )
        assert describe(enabled=True, source="default") == (
            "icons rendered with guard ON (default)"
        )

    def test_unknown_source_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="invalid policy source"):
            describe(enabled=True, source="registry")


class TestIsWallpaperHash:
    def test_accepts_64_hex(self) -> None:
        assert is_wallpaper_hash(H1) is True
        assert is_wallpaper_hash(H2) is True

    @pytest.mark.parametrize("value", ["", "xyz", "a" * 63, "a" * 65, "g" * 64, None, 42])
    def test_rejects_non_hashes(self, value: object) -> None:
        assert is_wallpaper_hash(value) is False
