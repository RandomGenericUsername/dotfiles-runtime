"""JSON clipboard history store — atomic, deduped, per-kind trimmed (D4).

History is one small JSON document under ``$XDG_STATE_HOME/hypr-pano/``
(default ``~/.local/state/hypr-pano/history.json``); copied images live under
``~/.cache/hypr-pano/``. The daemon is the single writer, so a whole-document
read-modify-write with an atomic ``os.replace`` is sufficient and crash-safe:
an interrupted write leaves the previous document intact.

Dedupe is by content hash — re-copying an existing item bumps its recency and
preserves its favorite flag rather than appending a duplicate. Eviction applies
the pure ``apply_retention`` policy and removes evicted image files (only ever
inside this store's own image directory).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from runtime.domain.clipboard import (
    ClipboardItem,
    RetentionLimits,
    apply_retention,
)
from runtime.ports.clipboard import IClipboardStore

__all__ = ["JsonClipboardStore", "default_history_path", "default_image_dir"]

logger = logging.getLogger(__name__)

#: History document version (schema marker for future migrations).
HISTORY_VERSION = 1


def _state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return Path(base)


def _cache_home() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base)


def default_history_path() -> Path:
    """``$XDG_STATE_HOME/hypr-pano/history.json``."""
    return _state_home() / "hypr-pano" / "history.json"


def default_image_dir() -> Path:
    """``$XDG_CACHE_HOME/hypr-pano/`` (where image items are cached)."""
    return _cache_home() / "hypr-pano"


class JsonClipboardStore(IClipboardStore):
    """Single-document JSON history with hash dedupe and atomic writes."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        image_dir: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path if path is not None else default_history_path()
        self._image_dir = image_dir if image_dir is not None else default_image_dir()
        self._clock = clock

    # ── Persistence ──────────────────────────────────────────────────

    def load(self) -> list[ClipboardItem]:
        """All stored items, newest first (empty on missing/corrupt file)."""
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("clipboard store: cannot read %s: %s", self._path, exc)
            return []
        try:
            data = json.loads(raw)
        except ValueError as exc:
            logger.warning(
                "clipboard store: malformed %s: %s; treating as empty", self._path, exc
            )
            return []
        if not isinstance(data, dict):
            return []
        records = data.get("items")
        if not isinstance(records, list):
            return []
        items: list[ClipboardItem] = []
        for record in records:
            item = self._decode(record)
            if item is not None:
                items.append(item)
        items.sort(key=lambda item: item.timestamp, reverse=True)
        return items

    def _decode(self, record: object) -> ClipboardItem | None:
        if not isinstance(record, dict):
            return None
        item_hash = record.get("hash")
        kind = record.get("kind")
        timestamp = record.get("timestamp")
        if not isinstance(item_hash, str) or not isinstance(kind, str):
            return None
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            return None
        text = record.get("text")
        path = record.get("path")
        return ClipboardItem(
            hash=item_hash,
            kind=kind,
            timestamp=float(timestamp),
            favorite=bool(record.get("favorite", False)),
            text=text if isinstance(text, str) else None,
            path=path if isinstance(path, str) else None,
        )

    def _write(self, items: list[ClipboardItem]) -> None:
        payload = {
            "version": HISTORY_VERSION,
            "items": [item.to_record() for item in items],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self._path.parent),
            prefix=".history-",
            suffix=".json.tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, self._path)
        except BaseException:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

    # ── Mutation ─────────────────────────────────────────────────────

    def add(self, item: ClipboardItem) -> ClipboardItem:
        """Insert or bump recency by hash; preserve an existing favorite."""
        items = self.load()
        stored: ClipboardItem | None = None
        for existing in items:
            if existing.hash == item.hash:
                stored = ClipboardItem(
                    hash=existing.hash,
                    kind=existing.kind,
                    timestamp=item.timestamp,
                    favorite=existing.favorite,
                    text=item.text if item.text is not None else existing.text,
                    path=item.path if item.path is not None else existing.path,
                )
                items = [stored if other.hash == item.hash else other for other in items]
                break
        if stored is None:
            stored = item
            items.append(item)
        items.sort(key=lambda entry: entry.timestamp, reverse=True)
        self._write(items)
        return stored

    def delete(self, item_hash: str) -> bool:
        """Remove by hash; also unlink its cached image. Returns whether it existed."""
        items = self.load()
        target = next((item for item in items if item.hash == item_hash), None)
        if target is None:
            return False
        items = [item for item in items if item.hash != item_hash]
        self._write(items)
        if target.path is not None:
            self._discard_image(target.path)
        return True

    def set_favorite(self, item_hash: str, favorite: bool) -> bool:
        """Set the favorite flag by hash; returns whether the item existed."""
        items = self.load()
        found = False
        updated: list[ClipboardItem] = []
        for item in items:
            if item.hash == item_hash:
                found = True
                updated.append(
                    ClipboardItem(
                        hash=item.hash,
                        kind=item.kind,
                        timestamp=item.timestamp,
                        favorite=favorite,
                        text=item.text,
                        path=item.path,
                    )
                )
            else:
                updated.append(item)
        if not found:
            return False
        self._write(updated)
        return True

    def evict(self, limits: RetentionLimits) -> list[ClipboardItem]:
        """Drop items over the per-kind limits and delete their image files."""
        items = self.load()
        kept, evicted = apply_retention(items, limits)
        if not evicted:
            return []
        self._write(kept)
        for item in evicted:
            if item.path is not None:
                self._discard_image(item.path)
        logger.info("clipboard store: evicted %d item(s)", len(evicted))
        return evicted

    # ── Image lifecycle ──────────────────────────────────────────────

    def _discard_image(self, path: str) -> None:
        """Unlink ``path`` only when it lives inside this store's image dir."""
        try:
            candidate = Path(path).resolve()
            image_root = self._image_dir.resolve()
            candidate.relative_to(image_root)
        except (OSError, ValueError):
            return
        try:
            candidate.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("clipboard store: cannot remove image %s: %s", candidate, exc)

    def cleanup_orphans(self) -> int:
        """Remove cached images no longer referenced by history. Returns count."""
        referenced = {
            Path(item.path).resolve()
            for item in self.load()
            if item.path is not None
        }
        if not self._image_dir.is_dir():
            return 0
        removed = 0
        for candidate in self._image_dir.iterdir():
            if not candidate.is_file():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if resolved in referenced:
                continue
            try:
                resolved.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("clipboard store: cannot remove orphan %s: %s", resolved, exc)
        return removed
