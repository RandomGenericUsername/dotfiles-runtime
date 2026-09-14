"""Clipboard domain — pure classification, items, and retention (no I/O).

The clipboard watcher captures a ``ClipboardReading`` (raw selected
representation plus MIME types) and derives a typed, hashable
``ClipboardItem``. Everything here is deterministic and side-effect free so
the classification precedence and eviction policy are unit-testable without
a clipboard, a compositor, or a store.

Classification precedence (design D9): image → link → color → emoji → code →
text. Image wins when any ``image/*`` representation is present, matching the
browser case where a copy offers both ``image/png`` and ``text/plain``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

__all__ = [
    "ITEM_TYPES",
    "PREVIEW_LIMIT",
    "ClipboardItem",
    "ClipboardReading",
    "RetentionLimits",
    "apply_retention",
    "classify",
    "preview_for",
    "summarize_kinds",
    "with_favorite",
]

#: Item kinds — the `clipboard.update` payload `type` enum, exactly.
ITEM_TYPES: tuple[str, ...] = ("text", "image", "link", "code", "color", "emoji")

#: Maximum preview characters carried on the wire (bounded payload).
PREVIEW_LIMIT = 280

_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_URL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://\S+$")
#: A pragmatic emoji test: every non-space character is pictographic/symbol
#: or a variation-selector/join/skin-tone modifier. Deliberately conservative
#: so mixed text never reads as emoji.
_EMOJI = re.compile(
    "^["
    "\U0001f000-\U0001faff"
    "\u2600-\u27bf"
    "\u2b00-\u2bff"
    "\ufe0e\ufe0f\u200d\u20e3"
    "\U0001f3fb-\U0001f3ff"
    "\u2190-\u21ff"
    "\\s"
    "]+$"
)
_CODE_LINE = re.compile(
    r"^\s*(?:"
    r"def |class |function |const |let |var |import |from |#include|package |"
    r"public |private |protected |return |if \(|for \(|while \(|switch \(|"
    r"</?[a-zA-Z][\w-]*[ />]|\)\s*\{|\}\s*;?\s*$"
    r")",
    re.MULTILINE,
)
_CODE_MARKERS = ("=>", "::", "->", "!==", "===", ";\n", "{\n", "}\n")


@dataclass(frozen=True, slots=True)
class ClipboardReading:
    """One observed clipboard selection (raw, pre-classification).

    ``text`` is the decoded plain-text representation when the clipboard
    offers one; ``image_bytes``/``image_mime`` carry the selected image
    representation. At least one of the two is present on a valid reading.
    """

    mimetypes: tuple[str, ...]
    text: str | None = None
    image_bytes: bytes | None = None
    image_mime: str | None = None
    image_path: str | None = None

    @property
    def has_image(self) -> bool:
        return any(mime.startswith("image/") for mime in self.mimetypes) or (
            self.image_bytes is not None
        )

    def canonical_bytes(self) -> bytes:
        """Stable bytes to hash: image bytes win, else the text encoding."""
        if self.image_bytes is not None:
            return self.image_bytes
        return (self.text or "").encode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class ClipboardItem:
    """A stored history entry.

    ``kind`` is one of :data:`ITEM_TYPES`; ``text`` is set for text-like
    kinds and ``path`` for images. ``favorite`` exempts the item from
    automatic eviction.
    """

    hash: str
    kind: str
    timestamp: float
    favorite: bool = False
    text: str | None = None
    path: str | None = None

    def to_payload(self) -> dict[str, str]:
        """The `clipboard.update` payload (no binary; empty sentinels)."""
        return {
            "type": self.kind,
            "hash": self.hash,
            "path": self.path or "",
            "preview": preview_for(self.text, self.kind),
        }

    def to_record(self) -> dict[str, object]:
        """The JSON history record shape."""
        record: dict[str, object] = {
            "hash": self.hash,
            "kind": self.kind,
            "timestamp": self.timestamp,
            "favorite": self.favorite,
        }
        if self.text is not None:
            record["text"] = self.text
        if self.path is not None:
            record["path"] = self.path
        return record


@dataclass(frozen=True, slots=True)
class RetentionLimits:
    """Per-kind maximum retained non-favorite items."""

    text: int = 200
    image: int = 50
    link: int = 100
    code: int = 100
    color: int = 50
    emoji: int = 50

    def for_kind(self, kind: str) -> int:
        """Limit for ``kind``; unknown kinds fall back to the text limit."""
        return int(getattr(self, kind, self.text))


def classify(mimetypes: Sequence[str], text: str | None) -> str:
    """Return the item kind for a reading (design D9 precedence)."""
    if any(mime.startswith("image/") for mime in mimetypes):
        return "image"
    if "text/uri-list" in mimetypes:
        return "link"
    if text is None:
        return "text"
    stripped = text.strip()
    if not stripped:
        return "text"
    if _URL.match(stripped):
        return "link"
    if _HEX_COLOR.match(stripped):
        return "color"
    if _EMOJI.match(stripped):
        return "emoji"
    if _looks_like_code(text):
        return "code"
    return "text"


def _looks_like_code(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return False
    marked = sum(1 for line in lines if _CODE_LINE.match(line))
    if marked >= 2:
        return True
    return marked >= 1 and any(marker in text for marker in _CODE_MARKERS)


def preview_for(text: str | None, kind: str) -> str:
    """Bounded single-line preview for a text-like item (empty for images)."""
    if kind == "image" or not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= PREVIEW_LIMIT:
        return collapsed
    return collapsed[: PREVIEW_LIMIT - 1] + "\u2026"


def apply_retention(
    items: Iterable[ClipboardItem], limits: RetentionLimits
) -> tuple[list[ClipboardItem], list[ClipboardItem]]:
    """Split ``items`` into (kept, evicted) under per-kind limits.

    Favorites are never evicted; a kind whose favorites alone exceed the
    limit keeps all of them (the limit bounds non-favorites). Order is
    preserved for the kept items.
    """
    ordered = sorted(items, key=lambda item: item.timestamp, reverse=True)
    kept: list[ClipboardItem] = []
    evicted: list[ClipboardItem] = []
    counts: dict[str, int] = {}
    for item in ordered:
        if item.favorite:
            kept.append(item)
            continue
        used = counts.get(item.kind, 0)
        if used < limits.for_kind(item.kind):
            counts[item.kind] = used + 1
            kept.append(item)
        else:
            evicted.append(item)
    return kept, evicted


def with_favorite(item: ClipboardItem, favorite: bool) -> ClipboardItem:
    """Return ``item`` with its favorite flag set (pure)."""
    return replace(item, favorite=favorite)


def summarize_kinds(items: Iterable[ClipboardItem]) -> Mapping[str, int]:
    """Count retained items per kind (observability/tests)."""
    counts: dict[str, int] = {}
    for item in items:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    return counts
