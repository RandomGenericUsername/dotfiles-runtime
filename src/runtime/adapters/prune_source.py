"""Prune sources + removal seam (Stories 3.2/3.3).

Supplies ``PruneUseCase`` with per-layer entries + recency and seed pins
(read-only), plus :func:`remove_entry` — the validated deletion seam used by
the explicit ``inspect cache prune`` command. All filesystem access lives
here (AD-25).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

from runtime.domain.models import CacheEntryRef

logger = logging.getLogger(__name__)

_LAYERS: frozenset[str] = frozenset({"wallpapers", "palettes", "effects", "icons"})
_HEX = frozenset("0123456789abcdef")
_SEED_FIELD_TO_LAYER = {
    "wallpaper": "wallpapers",
    "palette": "palettes",
    "effects": "effects",
    "icons": "icons",
}


def _is_entry_name(name: str) -> bool:
    return len(name) == 64 and all(c in _HEX for c in name)


def _timestamp_key(layer: str) -> str:
    return "imported_at" if layer == "wallpapers" else "generated_at"


def _read_timestamp(meta_path: Path, layer: str) -> str | None:
    """Return the entry's ISO timestamp, canonicalized, or ``None`` when undated.

    Canonicalized to fixed-width UTC (``YYYY-MM-DDTHH:MM:SS.ffffffZ``) so the
    use case's lexicographic recency sort is chronologically correct regardless
    of the recorded precision/offset. A symlinked meta is refused (undated →
    protected).
    """
    if meta_path.is_symlink():
        return None
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    value = meta.get(_timestamp_key(layer))
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def entries_for(state_root: Path, layer: str) -> list[CacheEntryRef]:
    """List one layer's valid entries with recency (undated -> ``None``)."""
    if layer not in _LAYERS:
        raise ValueError(f"unknown cache layer {layer!r}")
    cache_root = state_root / "cache"
    if cache_root.is_symlink():
        return []  # refuse a symlinked cache root (mirror InspectCacheUseCase)
    layer_dir = cache_root / layer
    if layer_dir.is_symlink() or not layer_dir.is_dir():
        return []
    refs: list[CacheEntryRef] = []
    try:
        children = sorted(layer_dir.iterdir())
    except OSError:
        return []
    for path in children:
        if path.is_symlink() or not path.is_dir():
            continue
        if not _is_entry_name(path.name):
            continue
        refs.append(CacheEntryRef(path.name, _read_timestamp(path / "meta.json", layer)))
    return refs


def seed_pins(state_root: Path) -> dict[str, set[str]]:
    """Derive seed-pinned ``(layer, hash)`` sets from the OLDEST seed history line.

    Reads ``history.jsonl``; absent/unreadable/empty → no pins. Torn trailing
    lines are skipped (never a crash). Read-only.
    """
    pins: dict[str, set[str]] = {layer: set() for layer in _LAYERS}
    history_path = state_root / "history.jsonl"
    try:
        fd = os.open(history_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return pins  # absent/symlinked/unreadable -> no pins
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError, UnicodeDecodeError:
        return pins
    lines = raw.splitlines()
    last_index = max((i for i, line in enumerate(lines) if line.strip()), default=-1)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            # Only a torn TRAILING line is tolerated (mirror the reader policy);
            # mid-file corruption means the oldest seed line is unknowable — fail
            # CLOSED (raise) rather than return no pins and let prune evict the
            # seed-pinned defaults.
            if i == last_index and not raw.endswith("\n"):
                break
            raise ValueError(
                f"history.jsonl line {i + 1}: not valid JSON; refusing to compute seed pins"
            ) from None
        if not isinstance(obj, dict) or obj.get("trigger") != "seed":
            continue
        for field, layer in _SEED_FIELD_TO_LAYER.items():
            value = obj.get(field)
            if isinstance(value, str) and value:
                pins[layer].add(value)
        break  # oldest seed line pins the first-run defaults
    return pins


def remove_entry(state_root: Path, layer: str, entry_hash: str) -> bool:
    """Delete one cache entry dir during an explicit prune (Story 3.3).

    Strictly validated: known layer, 64-char lowercase hex, no symlinked
    cache/layer/entry, and the resolved target must sit directly under
    ``cache/<layer>`` (no escape). Absent target → ``False`` (idempotent
    no-op); a present real dir → ``shutil.rmtree`` + one log line → ``True``.

    Raises ``ValueError`` on any validation failure (never a silent delete).
    """
    if layer not in _LAYERS:
        raise ValueError(f"unknown cache layer {layer!r}")
    if not _is_entry_name(entry_hash):
        raise ValueError(f"entry_hash must be 64-char lowercase hex, got {entry_hash!r}")
    cache_root = state_root / "cache"
    if cache_root.is_symlink():
        raise ValueError(f"cache dir is a symlink (refusing): {cache_root}")
    layer_dir = cache_root / layer
    if layer_dir.is_symlink():
        raise ValueError(f"layer dir is a symlink (refusing): {layer_dir}")
    target = layer_dir / entry_hash
    if target.is_symlink():
        raise ValueError(f"entry is a symlink (refusing): {target}")
    if not target.exists():
        return False
    try:
        resolved = target.resolve()
        cache_resolved = cache_root.resolve()
        layer_resolved = layer_dir.resolve()
    except OSError as exc:
        raise ValueError(f"cannot resolve entry {target}: {exc}") from exc
    if not resolved.is_relative_to(cache_resolved) or resolved.parent != layer_resolved:
        raise ValueError(f"entry escapes cache layer: {target}")
    if resolved.name != entry_hash:
        raise ValueError(f"entry resolves to a different name: {target} -> {resolved}")
    if not resolved.is_dir():
        return False  # non-dir squatter is not a prunable entry
    if target.is_symlink():
        return False  # swapped to a symlink mid-race: refuse
    try:
        shutil.rmtree(target)  # delete the NAMED inode, not a resolved target
    except FileNotFoundError, NotADirectoryError:
        return False  # vanished/replaced mid-race: idempotent no-op
    logger.info("prune: removed cache/%s/%s", layer, entry_hash)
    return True
