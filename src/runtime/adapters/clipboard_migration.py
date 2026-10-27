"""One-time clipboard data migration from ``hypr-pano`` to ``clipboard`` (AD-35).

The runtime clipboard daemon keeps its command name but its writable data now
lives in the ``clipboard`` namespace:

- config: ``$XDG_CONFIG_HOME/clipboard/config.json``
- history: ``$XDG_STATE_HOME/clipboard/history.json``
- images: ``$XDG_CACHE_HOME/clipboard/``

The old ``hypr-pano`` paths are migration sources only. Migration is idempotent
(marker under the new state namespace), never overwrites newer data, merges
history by content hash (favorites/recency survive), and removes the old paths
only after the new copies are verified. The pilot/consumer readiness is a
release-sequence constraint around this code, not a per-run switch: by the time
the runtime reads the new namespace, migration must have completed.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "ClipboardMigrationResult",
    "LEGACY_NAMESPACE",
    "NAMESPACE",
    "default_paths",
    "migrate_clipboard_paths",
]

NAMESPACE = "clipboard"
LEGACY_NAMESPACE = "hypr-pano"
MARKER_NAME = ".migrated-from-hypr-pano.json"


def _home(env_name: str, fallback: Path) -> Path:
    raw = os.environ.get(env_name)
    return Path(raw).expanduser() if raw else fallback


def default_paths() -> tuple[Path, Path, Path]:
    """Return ``(config_home, state_home, cache_home)`` for this process."""
    return (
        _home("XDG_CONFIG_HOME", Path.home() / ".config"),
        _home("XDG_STATE_HOME", Path.home() / ".local" / "state"),
        _home("XDG_CACHE_HOME", Path.home() / ".cache"),
    )


@dataclass(frozen=True, slots=True)
class ClipboardMigrationResult:
    """Outcome of one migration attempt."""

    migrated: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    skipped: bool = False
    errors: tuple[str, ...] = ()


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=".clipboard-migration-",
        suffix=".json.tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _move_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    try:
        os.replace(source, target)
    except OSError:
        shutil.copy2(source, target)
        source.unlink()


def _rewrite_item_paths(data: object, old_cache: Path, new_cache: Path) -> object:
    if not isinstance(data, dict):
        return data
    old_prefix = str(old_cache)
    new_prefix = str(new_cache)
    items = data.get("items")
    if not isinstance(items, list):
        return data
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            item["path"] = item["path"].replace(old_prefix, new_prefix)
    return data


def _load_history(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    return data if isinstance(data, dict) else None


def _merge_history(
    old_path: Path,
    new_path: Path,
    old_cache: Path,
    new_cache: Path,
    *,
    remove_legacy: bool,
) -> bool:
    """Merge old history into the new document without losing items/favorites."""
    old_data = _load_history(old_path)
    if old_data is None:
        # Preserve malformed bytes rather than deleting the only copy.
        new_path.parent.mkdir(parents=True, exist_ok=True)
        if not new_path.exists():
            shutil.copy2(old_path, new_path)
        if remove_legacy:
            old_path.unlink(missing_ok=True)
        return True
    old_data = _rewrite_item_paths(old_data, old_cache, new_cache)
    old_items = old_data.get("items")
    if not isinstance(old_items, list):
        old_items = []

    new_data = _load_history(new_path)
    if new_data is None:
        _atomic_write_json(new_path, old_data)
        if remove_legacy:
            old_path.unlink(missing_ok=True)
        return True
    new_items = new_data.get("items")
    if not isinstance(new_items, list):
        new_items = []

    merged: dict[str, dict[str, object]] = {}
    for item in [*old_items, *new_items]:
        if not isinstance(item, dict) or not isinstance(item.get("hash"), str):
            continue
        key = item["hash"]
        previous = merged.get(key)
        if previous is None:
            merged[key] = dict(item)
            continue
        previous["favorite"] = bool(previous.get("favorite")) or bool(item.get("favorite"))
        try:
            previous["timestamp"] = max(
                float(previous.get("timestamp", 0)), float(item.get("timestamp", 0))
            )
        except TypeError, ValueError:
            pass
        for field_name in ("text", "path", "kind"):
            if not previous.get(field_name) and item.get(field_name):
                previous[field_name] = item[field_name]
    new_data["version"] = new_data.get("version", 1)
    new_data["items"] = list(merged.values())
    _atomic_write_json(new_path, new_data)
    if remove_legacy:
        old_path.unlink(missing_ok=True)
    return True


def _merge_cache_dir(old_dir: Path, new_dir: Path, *, remove_legacy: bool) -> None:
    new_dir.mkdir(parents=True, exist_ok=True)
    for entry in old_dir.iterdir():
        target = new_dir / entry.name
        if entry.is_file() and not target.exists():
            shutil.copy2(entry, target)
    if remove_legacy:
        shutil.rmtree(old_dir)


def migrate_clipboard_paths(
    *,
    config_home: Path | None = None,
    state_home: Path | None = None,
    cache_home: Path | None = None,
    remove_legacy: bool = True,
) -> ClipboardMigrationResult:
    """Migrate ``hypr-pano`` config/history/cache to ``clipboard`` once."""
    default_config, default_state, default_cache = default_paths()
    config_home = config_home or default_config
    state_home = state_home or default_state
    cache_home = cache_home or default_cache

    new_state_dir = state_home / NAMESPACE
    marker = new_state_dir / MARKER_NAME
    if marker.is_file():
        return ClipboardMigrationResult(skipped=True)

    old_config = config_home / LEGACY_NAMESPACE / "config.json"
    new_config = config_home / NAMESPACE / "config.json"
    old_history = state_home / LEGACY_NAMESPACE / "history.json"
    new_history = new_state_dir / "history.json"
    old_cache = cache_home / LEGACY_NAMESPACE
    new_cache = cache_home / NAMESPACE

    migrated: list[str] = []
    removed: list[str] = []
    errors: list[str] = []

    if old_config.is_file():
        try:
            if remove_legacy:
                _move_file(old_config, new_config)
            elif not new_config.exists():
                new_config.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(old_config, new_config)
        except OSError as exc:
            errors.append(f"config: {exc}")
        else:
            migrated.append("config")
            _remove_legacy(old_config, remove_legacy, removed)

    if old_history.is_file():
        try:
            _merge_history(
                old_history,
                new_history,
                old_cache,
                new_cache,
                remove_legacy=remove_legacy,
            )
        except OSError as exc:
            errors.append(f"history: {exc}")
        else:
            migrated.append("history")
            _remove_legacy(old_history, remove_legacy, removed)

    if old_cache.is_dir():
        try:
            if new_cache.exists():
                _merge_cache_dir(old_cache, new_cache, remove_legacy=remove_legacy)
            elif remove_legacy:
                new_cache.parent.mkdir(parents=True, exist_ok=True)
                os.replace(old_cache, new_cache)
            else:
                shutil.copytree(old_cache, new_cache)
        except OSError as exc:
            errors.append(f"cache: {exc}")
        else:
            migrated.append("cache")
            if remove_legacy:
                _remove_legacy(old_cache, True, removed)

    if not errors:
        try:
            _atomic_write_json(
                marker,
                {
                    "version": 1,
                    "from": LEGACY_NAMESPACE,
                    "to": NAMESPACE,
                    "migrated": migrated,
                },
            )
        except OSError as exc:
            errors.append(f"marker: {exc}")

    if errors:
        logger.warning("clipboard migration incomplete: %s", "; ".join(errors))
    elif migrated:
        logger.info("clipboard migration from %s complete: %s", LEGACY_NAMESPACE, migrated)
    return ClipboardMigrationResult(
        migrated=tuple(migrated),
        removed=tuple(removed),
        skipped=False,
        errors=tuple(errors),
    )


def _remove_legacy(path: Path, remove_legacy: bool, removed: list[str]) -> None:
    if not remove_legacy:
        return
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
            parent = path.parent
            if parent.name == LEGACY_NAMESPACE:
                try:
                    parent.rmdir()
                except OSError:
                    pass
    except OSError as exc:
        logger.warning("clipboard migration: cannot remove legacy %s: %s", path, exc)
    else:
        removed.append(str(path))
