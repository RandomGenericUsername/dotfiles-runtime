"""Layered cache populator with staging-dir and hardlink.

Implements AD-9 staging-dir pattern + AD-16 hardlink for write-once
cache entries under ``state_root/cache/<layer>/<hash>/``.

Cache layout (shared-data-contract, cache-model.md)::

    state_root/cache/wallpapers/<wh>/wallpaper.png + meta.json
    state_root/cache/palettes/<ph>/colors.yaml + colors.conf + colors.gtk.css + meta.json
    state_root/cache/effects/<eh>/*.png + meta.json
    state_root/cache/icons/<ih>/*.svg + meta.json

All ``meta.json`` MUST contain ``hash_algorithm == HASH_ALGORITHM == "sha256"``
literal. ``populate_fn`` writes artifacts + ``meta.json`` inside the staging
dir; the staging dir is then atomically renamed to the final target.

Staging invariants (AD-9, cache-model.md):
- Staging is a **sibling** of ``cache/``: ``cache/.staging-<pid>-<rand>/``.
- ``os.rename`` is atomic on same filesystem (POSIX sibling guarantees same mount).
- If ``target`` already existed before rename, staging is discarded and ``False``
  is returned — never overwrites.
- Staging is removed in **all** error paths (populate_fn exception, rename race,
  unexpected OSError) via ``try/finally`` + ``shutil.rmtree(ignore_errors=True)``.

Hardlink invariant (AD-16):
- ``hardlink_or_copy`` tries ``os.link`` first; on ``EXDEV`` falls back to
  ``shutil.copy2`` preserving mtime/mode; other ``OSError`` propagates.

Domain purity (AD-1, AD-14):
- This module lives in ``adapters/`` only (allowed ``os``/``pathlib``/``shutil``).
- ``domain/`` stays pure — no ``os`` imported there.
"""

from __future__ import annotations

import errno
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Final

from runtime.adapters.hashing import HASH_ALGORITHM

assert HASH_ALGORITHM == "sha256"

CACHE_LAYERS: Final[frozenset[str]] = frozenset({"wallpapers", "palettes", "effects", "icons"})
CACHE_STAGING_PREFIX: Final[str] = ".staging-"


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def _validate_target(target: Path) -> None:
    """Validate ``target`` is ``.../cache/<layer>/<64hex>``.

    Raises ``ValueError`` if:
    - ``target.parent.name not in CACHE_LAYERS``
    - ``target.parent.parent.name != "cache"``
    - ``target.name`` is not 64-char lowercase hex
    """
    layer = target.parent.name
    if layer not in CACHE_LAYERS:
        raise ValueError(f"unknown layer {layer!r}, expected one of {sorted(CACHE_LAYERS)}")
    if target.parent.parent.name != "cache":
        raise ValueError(f"target must be under cache/<layer>/<hash>, got {target}")
    if not _is_hex64(target.name):
        raise ValueError(f"entry_hash must be 64-char lowercase hex sha256, got {target.name!r}")


def cache_entry_path(state_root: Path, layer: str, entry_hash: str) -> Path:
    """Return ``state_root / \"cache\" / layer / entry_hash`` with validation.

    Validates ``layer in CACHE_LAYERS`` and ``entry_hash`` is 64-char hex
    (lowercase). Does **not** create directories.

    Example contract for ``meta.json`` written inside staging by ``populate_fn``:

    - ``wallpapers/<wh>/meta.json``::

        {hash_algorithm: "sha256", kind: "wallpaper", content_hash: "<wh>",
         source_path: "/src/wall.png", imported_at: "2026-08-29T00:00:00Z"}

    - ``palettes/<ph>/meta.json``::

        {hash_algorithm: "sha256", kind: "palette", entry_hash: "<ph>",
         source_wallpaper_hash: "<wh>", input_template_hash: "<th>",
         artifact_hashes: {colors.yaml: "<h>", colors.conf: "<h>", colors.gtk.css: "<h>"},
         generated_at: "2026-08-29T00:00:00Z"}

    ``hash_algorithm`` MUST be ``HASH_ALGORITHM`` literal ``"sha256"``.
    ``artifact_hashes`` values are 64-char hex. ``meta.json`` is written
    with ``json.dump(..., indent=2, sort_keys=True)`` UTF-8.
    """
    if layer not in CACHE_LAYERS:
        raise ValueError(f"unknown layer {layer!r}, expected one of {sorted(CACHE_LAYERS)}")
    if not _is_hex64(entry_hash):
        raise ValueError(f"entry_hash must be 64-char lowercase hex sha256, got {entry_hash!r}")
    return state_root / "cache" / layer / entry_hash.lower()


def _staging_dir_for(target: Path) -> Path:
    """Return sibling staging dir for ``target``: ``cache/.staging-<pid>-<uuid8>``."""
    cache_dir = target.parent
    # Sibling of cache/ → cache_dir.parent / ".staging-<pid>-<rand>"
    # e.g. target = state_root/cache/wallpapers/<hash>
    #      cache_dir = state_root/cache/wallpapers
    #      staging = state_root/cache/.staging-<pid>-<8hex>
    return cache_dir.parent / f"{CACHE_STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex[:8]}"


def hardlink_or_copy(src: Path, dst: Path) -> None:
    """Attempt hardlink ``src`` → ``dst``, fallback to copy on cross-device.

    - Tries ``os.link(src, dst)`` (hardlink) first.
    - On ``OSError`` with ``errno.EXDEV`` (cross-filesystem) falls back to
      ``shutil.copy2(src, dst)`` preserving metadata (mtime, mode).
    - On any other ``OSError`` propagates without copying.
    - Ensures ``dst.parent`` exists (``mkdir(parents=True, exist_ok=True)``)
      before link/copy — staging dir is fresh but defensive.
    - Does NOT use ``Path.hardlink_to`` or ``os.symlink``.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            shutil.copy2(src, dst)
        else:
            raise


def populate_via_staging(target: Path, populate_fn: Callable[[Path], None]) -> bool:
    """Populate ``target`` via staging-dir atomically, write-once.

    - Generates into sibling staging dir ``cache/.staging-<pid>-<rand>/`` then
      atomically ``os.rename`` to ``target``.
    - Returns ``True`` if this call created ``target``, ``False`` if ``target``
      already existed (staging discarded, existing entry unchanged — never overwritten).
    - ``populate_fn`` receives the staging ``Path`` and must create artifacts +
      ``meta.json`` inside it (with ``hash_algorithm == HASH_ALGORITHM``).
    - Validates ``target`` is under ``cache/<layer>/<64hex>`` (ValueError otherwise).
    - Handles TOCTOU race: ``target.exists()`` fast-path plus ``FileExistsError``/
      ``ENOTEMPTY``/``EEXIST``/``"File exists"`` on ``os.rename`` → discard staging.
    - Cleans staging in all error paths (populate_fn exception, rename race,
      unexpected OSError) — no orphan ``cache/.staging-*`` left behind.
    """
    _validate_target(target)

    # Fast-path: existing final entry never overwritten
    if target.exists():
        return False

    cache_dir = target.parent
    staging = _staging_dir_for(target)

    # Ensure cache_dir exists (e.g. state_root/cache/wallpapers)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # staging is sibling of cache/, so parent (state_root/cache) already exists after above
    # but ensure parent exists for robustness
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=False)

    try:
        populate_fn(staging)

        try:
            os.rename(staging, target)
        except FileExistsError:
            # Lost race — another writer created target first
            shutil.rmtree(staging, ignore_errors=True)
            return False
        except OSError as e:
            # Handle ENOTEMPTY/EEXIST and string "File exists" variants
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST) or "File exists" in str(e):
                shutil.rmtree(staging, ignore_errors=True)
                return False
            raise
        return True
    except BaseException:
        # populate_fn failed or rename failed — clean staging, never leave partial target
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        # Defensive: if staging still exists (rename succeeded but outer
        # exception), ensure removed
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
