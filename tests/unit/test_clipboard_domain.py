"""Clipboard domain: classification precedence, previews, retention (no I/O)."""

from __future__ import annotations

from runtime.domain.clipboard import (
    PREVIEW_LIMIT,
    ClipboardItem,
    ClipboardReading,
    RetentionLimits,
    apply_retention,
    classify,
    preview_for,
    summarize_kinds,
    with_favorite,
)


class TestClassify:
    def test_plain_text(self) -> None:
        assert classify(("text/plain", "UTF8_STRING"), "hello world") == "text"

    def test_image_beats_dual_representation(self) -> None:
        # A browser copy offering both image/png and text/plain is an image.
        assert classify(("text/plain", "image/png"), "https://example.com") == "image"

    def test_uri_list_is_a_link(self) -> None:
        assert classify(("text/uri-list",), "https://example.com") == "link"

    def test_bare_url_is_a_link(self) -> None:
        assert classify(("text/plain",), "https://example.com/a?b=1") == "link"

    def test_hex_color(self) -> None:
        assert classify(("text/plain",), "#1e1e2e") == "color"
        assert classify(("text/plain",), "#fff") == "color"
        assert classify(("text/plain",), "#1e1e2e80") == "color"

    def test_emoji(self) -> None:
        assert classify(("text/plain",), "\U0001f600\U0001f389") == "emoji"

    def test_emoji_does_not_swallow_mixed_text(self) -> None:
        assert classify(("text/plain",), "hello \U0001f600") == "text"

    def test_code_snippet(self) -> None:
        snippet = "const x = 1;\nfunction f() {\n  return x;\n}"
        assert classify(("text/plain",), snippet) == "code"

    def test_python_dict_assignment_is_code(self) -> None:
        assert classify(("text/plain",), "dict_a = {'apple': 1, 'banana': 2}") == "code"

    def test_assignment_is_code(self) -> None:
        assert classify(("text/plain",), "total = price * quantity") == "code"

    def test_empty_is_text(self) -> None:
        assert classify(("text/plain",), "") == "text"
        assert classify(("text/plain",), None) == "text"


class TestReading:
    def test_image_reading_hashes_bytes(self) -> None:
        reading = ClipboardReading(mimetypes=("image/png",), image_bytes=b"\x89PNG")
        assert reading.has_image is True
        assert reading.canonical_bytes() == b"\x89PNG"

    def test_text_reading_hashes_utf8(self) -> None:
        reading = ClipboardReading(mimetypes=("text/plain",), text="a\u00e9")
        assert reading.has_image is False
        assert reading.canonical_bytes() == "a\u00e9".encode()


class TestPreview:
    def test_text_is_single_line_and_bounded(self) -> None:
        preview = preview_for("a\n\n  b\tc   d", "text")
        assert preview == "a b c d"

    def test_long_text_is_truncated_with_ellipsis(self) -> None:
        preview = preview_for("x" * (PREVIEW_LIMIT * 2), "text")
        assert len(preview) == PREVIEW_LIMIT
        assert preview.endswith("\u2026")

    def test_image_preview_is_empty(self) -> None:
        assert preview_for("ignored", "image") == ""

    def test_payload_uses_empty_sentinels(self) -> None:
        item = ClipboardItem(hash="h", kind="image", timestamp=1.0, path="/tmp/i.png")
        assert item.to_payload() == {
            "type": "image",
            "hash": "h",
            "path": "/tmp/i.png",
            "preview": "",
        }


class TestRetention:
    def test_evicts_oldest_non_favorite_first(self) -> None:
        items = [
            ClipboardItem(hash=f"t{i}", kind="text", timestamp=float(i))
            for i in range(5)
        ]
        kept, evicted = apply_retention(items, RetentionLimits(text=2))
        assert [item.hash for item in kept] == ["t4", "t3"]
        assert [item.hash for item in evicted] == ["t2", "t1", "t0"]

    def test_favorites_are_retained(self) -> None:
        items = [
            ClipboardItem(hash="old-fav", kind="text", timestamp=1.0, favorite=True),
            ClipboardItem(hash="old", kind="text", timestamp=2.0),
            ClipboardItem(hash="new", kind="text", timestamp=3.0),
        ]
        kept, evicted = apply_retention(items, RetentionLimits(text=1))
        assert {item.hash for item in kept} == {"old-fav", "new"}
        assert [item.hash for item in evicted] == ["old"]

    def test_limits_are_per_kind(self) -> None:
        items = [
            ClipboardItem(hash="a", kind="text", timestamp=3.0),
            ClipboardItem(hash="b", kind="text", timestamp=2.0),
            ClipboardItem(hash="c", kind="image", timestamp=1.0, path="/i.png"),
        ]
        kept, evicted = apply_retention(items, RetentionLimits(text=1, image=1))
        assert {item.hash for item in kept} == {"a", "c"}
        assert [item.hash for item in evicted] == ["b"]

    def test_unknown_kind_falls_back_to_text_limit(self) -> None:
        assert RetentionLimits(text=7).for_kind("mystery") == 7

    def test_summarize_and_favorite_helper(self) -> None:
        item = ClipboardItem(hash="h", kind="text", timestamp=1.0)
        assert summarize_kinds([item]) == {"text": 1}
        assert with_favorite(item, True).favorite is True
