"""Clipboard config reader: defaults, partial overrides, tolerant errors."""

from __future__ import annotations

import json
from pathlib import Path

from runtime.adapters.clipboard_config import JsonClipboardConfigReader
from runtime.domain.clipboard import RetentionLimits


def _write(path: Path, payload: object) -> Path:
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


class TestJsonClipboardConfigReader:
    def test_missing_file_uses_defaults(self, tmp_path: Path) -> None:
        reader = JsonClipboardConfigReader(tmp_path / "nope.json")
        assert reader.read() == RetentionLimits()

    def test_partial_override_keeps_other_defaults(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "config.json", {"limits": {"image": 7}})
        limits = JsonClipboardConfigReader(path).read()
        assert limits.image == 7
        assert limits.text == RetentionLimits().text

    def test_full_override(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "config.json",
            {"limits": {"text": 1, "image": 2, "link": 3, "code": 4, "color": 5, "emoji": 6}},
        )
        assert JsonClipboardConfigReader(path).read() == RetentionLimits(1, 2, 3, 4, 5, 6)

    def test_malformed_json_uses_defaults(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "config.json", "{not json")
        assert JsonClipboardConfigReader(path).read() == RetentionLimits()

    def test_non_object_limits_uses_defaults(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "config.json", {"limits": [1, 2, 3]})
        assert JsonClipboardConfigReader(path).read() == RetentionLimits()

    def test_invalid_values_fall_back_per_kind(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path / "config.json",
            {"limits": {"text": "lots", "image": -1, "link": True, "code": 10}},
        )
        limits = JsonClipboardConfigReader(path).read()
        defaults = RetentionLimits()
        assert limits.text == defaults.text
        assert limits.image == defaults.image
        assert limits.link == defaults.link
        assert limits.code == 10

    def test_unknown_keys_are_ignored(self, tmp_path: Path) -> None:
        path = _write(tmp_path / "config.json", {"limits": {"bogus": 9, "image": 3}})
        assert JsonClipboardConfigReader(path).read().image == 3
