"""Layered cache populator with staging-dir and hardlink.

Implements AD-9 staging-dir pattern + AD-16 hardlink for write-once
cache entries under ``state_root/cache/<layer>/<hash>/``.

Cache layout (shared-data-contract, cache-model.md)::

    state_root/cache/wallpapers/<wh>/wallpaper.png + meta.json
    state_root/cache/palettes/<ph>/
        colors.yaml + colors.conf + colors.gtk.css
        + colors.adw.css + colors.sequences + colors.rasi + meta.json
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
import json
import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Final

from runtime.adapters.hashing import HASH_ALGORITHM, hash_file
from runtime.domain.models import CorruptCacheError, EntryHealth

if HASH_ALGORITHM != "sha256":
    raise AssertionError(f"HASH_ALGORITHM must be 'sha256', got {HASH_ALGORITHM!r}")

CACHE_LAYERS: Final[frozenset[str]] = frozenset({"wallpapers", "palettes", "effects", "icons"})
CACHE_STAGING_PREFIX: Final[str] = ".staging-"
CACHE_QUARANTINE_DIR: Final[str] = ".quarantine"
# Grace window before a dead-PID staging dir is reaped — protects against
# clock skew and pid reuse racing a just-finished sibling process.
STAGING_REAP_GRACE_SECONDS: Final[int] = 900


def _staging_dir_is_live(name: str) -> bool:
    """Return True if the staging dir's embedded PID belongs to a live process.

    Staging names are ``.staging-<pid>-<uuid>``. Malformed names (no numeric
    PID) are treated as not live and reaped by mtime grace instead.
    """
    pid_part = name[len(CACHE_STAGING_PREFIX) :].split("-", 1)[0]
    if not pid_part.isdigit():
        return False
    pid = int(pid_part)
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return True  # defensive: cannot determine → treat as live
    return True


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def _validate_target(target: Path) -> None:
    """Validate ``target`` is ``.../cache/<layer>/<64hex>``.

    Raises ``ValueError`` if:
    - ``target.parent.name not in CACHE_LAYERS``
    - ``target.parent.parent.name != "cache"``
    - ``target.name`` is not 64-char lowercase hex
    - path contains traversal components (``..``) or is not at least 3 parts deep
    """
    # Reject traversal components and shallow paths before name checks
    if ".." in target.parts:
        raise ValueError(f"target must not contain '..', got {target}")
    if len(target.parts) < 3:
        raise ValueError(f"target must be at least cache/<layer>/<hash>, got {target}")
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
         artifact_hashes: {colors.yaml: "<h>", colors.conf: "<h>",
                           colors.gtk.css: "<h>", colors.adw.css: "<h>",
                           colors.sequences: "<h>", colors.rasi: "<h>"},
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


def quarantine_entry(state_root: Path, layer: str, entry_hash: str) -> Path | None:
    """Rename-aside a bad cache entry into ``cache/.quarantine/`` (Story 2.2).

    Repair's only "destructive-looking" step is a MOVE, never a delete:
    ``cache/<layer>/<hash>/`` → ``cache/.quarantine/<layer>/<hash>-<utc>-<pid>/``
    via atomic ``os.rename`` (same filesystem). Bytes are preserved for
    forensics; eviction is Story 3.x's domain, not repair's.

    - Absent entry path → ``None`` (nothing to move; reconcile rebuilds the
      missing entry in place).
    - A regular file or symlink squatting the entry path is ALSO moved aside
      (a non-dir there is corruption the repair must clear, not skip).
    - Collision-safe: loops on a uuid suffix until the target is free; never
      overwrites an existing quarantine dir.
    - Concurrent repairs: a rename race (entry already moved by another
      process) returns ``None`` instead of crashing.
    - Validates ``layer``/``entry_hash`` via the same rules as
      :func:`cache_entry_path` (raises ``ValueError`` on bad input).

    Returns the quarantine path, or ``None`` when the entry was absent.
    """
    if layer not in CACHE_LAYERS:
        raise ValueError(f"unknown layer {layer!r}, expected one of {sorted(CACHE_LAYERS)}")
    if not _is_hex64(entry_hash):
        raise ValueError(f"entry_hash must be 64-char lowercase hex sha256, got {entry_hash!r}")
    entry_dir = state_root / "cache" / layer / entry_hash.lower()
    if not entry_dir.exists() and not entry_dir.is_symlink():
        return None  # absent: nothing to move
    quarantine_parent = state_root / "cache" / CACHE_QUARANTINE_DIR / layer
    quarantine_parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    base_name = f"{entry_hash.lower()}-{stamp}-{os.getpid()}"
    target = quarantine_parent / base_name
    while target.exists() or target.is_symlink():
        target = quarantine_parent / f"{base_name}-{uuid.uuid4().hex[:8]}"
    try:
        os.rename(entry_dir, target)
    except FileNotFoundError:
        return None  # concurrent repair already moved it
    return target


def _staging_dir_for(target: Path) -> Path:
    """Return sibling staging dir for ``target``: ``cache/.staging-<pid>-<uuid>``."""
    cache_dir = target.parent
    # Sibling of cache/ → cache_dir.parent / ".staging-<pid>-<rand>"
    # e.g. target = state_root/cache/wallpapers/<hash>
    #      cache_dir = state_root/cache/wallpapers
    #      staging = state_root/cache/.staging-<pid>-<uuid>
    # Full uuid4 hex (128 bits) avoids 32-bit birthday collisions of truncated 8-char.
    return cache_dir.parent / f"{CACHE_STAGING_PREFIX}{os.getpid()}-{uuid.uuid4().hex}"


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
    if not src.is_file():
        raise ValueError(f"src must be a regular file, got {src!r}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError as e:
        if e.errno == errno.EXDEV:
            shutil.copy2(src, dst)
        else:
            raise


def verify_entry(entry_dir: Path, *, annotate: bool = False) -> EntryHealth:
    """Read-side health check for one published cache entry (Story 3.1, FR-5).

    Mirrors :func:`verify_staging` for the read path: validates ``meta.json``
    and re-hashes recorded artifacts. Never mutates EXCEPT the sanctioned
    legacy annotation (AD-27): an entry with NO ``artifact_hashes`` map is
    annotated in place (every file under ``entry_dir`` except the root
    ``meta.json`` is hashed and recorded, other meta keys preserved, atomic
    tmp+replace) and reported ``ok``; it is never hard-failed.

    Verdicts:
    - ``missing``: ``meta.json`` absent, or a recorded artifact file absent.
    - ``corrupt``: ``meta.json`` unreadable/unparseable/not an object, a
      malformed ``artifact_hashes`` map, an unsafe artifact key
      (absolute/``..``/empty/``meta.json``/NUL), or a digest mismatch.
    - ``ok``: every recorded artifact present and matching (or legacy
      tolerated/annotated when ``annotate`` is False/True respectively).

    ``annotate=False`` leaves a legacy entry untouched and reports ``ok``
    (unverified). Read-only otherwise: no deletes, no layout change (AD-21).
    """
    meta_path = entry_dir / "meta.json"
    if entry_dir.is_symlink() or meta_path.is_symlink():
        return EntryHealth("corrupt", "symlinked entry/meta refused")
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EntryHealth("missing", f"{meta_path} absent")
    except (OSError, UnicodeDecodeError) as exc:
        return EntryHealth("corrupt", f"meta unreadable: {exc}")
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as exc:
        return EntryHealth("corrupt", f"meta unparseable: {exc}")
    if not isinstance(meta, dict):
        return EntryHealth("corrupt", "meta not an object")
    algorithm = meta.get("hash_algorithm")
    if algorithm is not None and algorithm != HASH_ALGORITHM:
        return EntryHealth("corrupt", f"hash_algorithm={algorithm!r} != {HASH_ALGORITHM!r}")
    recorded = meta.get("artifact_hashes")
    if not recorded:  # None OR {} → nothing recorded (legacy)
        if not annotate:
            return EntryHealth("ok", "legacy entry (unverified)")
        return _annotate_legacy(entry_dir, meta, meta_path)
    if not isinstance(recorded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in recorded.items()
    ):
        return EntryHealth("corrupt", "artifact_hashes malformed")
    try:
        entry_resolved = entry_dir.resolve()
    except (OSError, ValueError) as exc:
        return EntryHealth("corrupt", f"unresolvable entry: {exc}")
    for rel, expected in recorded.items():
        if (
            not rel
            or rel in (".", "meta.json")
            or rel.startswith("/")
            or "\x00" in rel
            or ".." in Path(rel).parts
        ):
            return EntryHealth("corrupt", f"unsafe artifact key: {rel}")
        try:
            resolved = (entry_dir / rel).resolve()
        except (OSError, ValueError) as exc:
            return EntryHealth("corrupt", f"unresolvable artifact {rel}: {exc}")
        if not resolved.is_relative_to(entry_resolved):
            return EntryHealth("corrupt", f"artifact escapes entry: {rel}")
        if not resolved.is_file():
            return EntryHealth("missing", f"artifact absent: {rel}")
        try:
            actual = hash_file(resolved)
        except OSError as exc:
            return EntryHealth("corrupt", f"unhashable {rel}: {exc}")
        if actual != expected:
            return EntryHealth("corrupt", f"digest mismatch: {rel}")
    try:
        recorded_set = set(recorded)
        for path in entry_dir.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            if not path.is_file():
                continue
            rel = path.relative_to(entry_dir).as_posix()
            if rel == "meta.json" or path.name.startswith(f".{meta_path.name}.tmp."):
                continue
            if rel not in recorded_set:
                return EntryHealth("corrupt", f"unrecorded file: {rel}")
    except OSError as exc:
        return EntryHealth("corrupt", f"completeness walk failed: {exc}")
    return EntryHealth("ok", "verified")


def _annotate_legacy(entry_dir: Path, meta: dict[str, object], meta_path: Path) -> EntryHealth:
    """Hash every non-meta file under ``entry_dir`` and record it (AD-27).

    Recursive (WEG nested layouts); the root ``meta.json`` and any
    ``.meta.json.tmp.*`` crash leftover are never hashed. Writes atomically
    (sibling tmp + ``os.replace``) and preserves other keys + the file mode.
    """
    hashes: dict[str, str] = {}
    try:
        entry_resolved = entry_dir.resolve()
        for path in entry_dir.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            if not path.is_file():
                continue
            rel = path.relative_to(entry_dir).as_posix()
            if rel == "meta.json" or path.name.startswith(f".{meta_path.name}.tmp."):
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(entry_resolved):
                return EntryHealth("corrupt", f"artifact escapes entry: {rel}")
            hashes[rel] = hash_file(resolved)
    except (OSError, ValueError) as exc:
        return EntryHealth("corrupt", f"annotate walk failed: {exc}")
    new_meta = dict(meta)
    new_meta["artifact_hashes"] = hashes
    tmp = meta_path.parent / f".{meta_path.name}.tmp.{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(new_meta, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            shutil.copymode(meta_path, tmp)
        except OSError:
            pass
        os.replace(tmp, meta_path)
    except OSError as exc:
        return EntryHealth("corrupt", f"legacy annotation failed: {exc}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
    return EntryHealth("ok", "legacy entry annotated", annotated=True)


def verify_staging(staging: Path) -> None:
    """Verify staged artifacts against ``staging/meta.json`` digests (AD-26).

    Called by :func:`populate_via_staging` after ``populate_fn`` returns and
    before the atomic rename: a mismatch raises :class:`CorruptCacheError`
    while the entry is still invisible, so corruption never enters the cache.
    The caller's ``try/finally`` cleans staging on every failure path.

    Rules (strict on what is recorded, lenient on shape):
    - ``meta.json`` must parse (unparseable → corrupt, never a raw crash).
    - ``artifact_hashes`` ABSENT → skip verification entirely (heterogeneous
      / legacy shape such as wallpaper metas keyed by ``content_hash``: the
      generic mechanism stays shape-agnostic; Story 3.1 annotates such
      entries on read). Presence of the map opts into full enforcement.
    - Map present but malformed (not a ``str -> str`` map) → corrupt (a
      broken writer, not a legacy shape). Map present with
      ``hash_algorithm != "sha256"`` → corrupt.
    - Every recorded ``(relpath, hash)`` must resolve INSIDE staging
      (traversal-escaped paths are corrupt), exist as a file, and match
      ``sha256(bytes)`` (chunked reads via :func:`hash_file`; equality
      decides — no hex-format validator needed since computed digests are
      always hex).
    - Every file under staging (recursive, covering nested generator layouts)
      except exactly ``staging/meta.json`` must be recorded (extra unrecorded
      files are corrupt); empty dirs are ignored (carry no bytes).

    Assumes no concurrent writer to the PID-namespaced staging dir between
    verification and the caller's rename (same-user threat model; the
    namespace makes collision practically impossible — documented, not
    mitigated).

    Raises:
        CorruptCacheError: on any digest/presence/shape violation. Never
            returns normally for a corrupt staging dir.
    """
    meta_path = staging / "meta.json"
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CorruptCacheError(f"cannot read staging meta.json in {staging}: {exc}") from exc
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CorruptCacheError(f"corrupt staging meta.json in {staging}: {exc}") from exc
    if not isinstance(meta, dict):
        raise CorruptCacheError(f"corrupt staging meta.json (not an object) in {staging}")
    if meta.get("hash_algorithm") != HASH_ALGORITHM:
        raise CorruptCacheError(
            f"staging meta.json declares hash_algorithm={meta.get('hash_algorithm')!r} "
            f"(expected {HASH_ALGORITHM!r}) in {staging}"
        )
    recorded = meta.get("artifact_hashes")
    if recorded is None:
        return  # heterogeneous/legacy shape: nothing recorded, nothing to verify
    if not isinstance(recorded, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in recorded.items()
    ):
        raise CorruptCacheError(
            f"staging meta.json carries a malformed artifact_hashes map in {staging}"
        )
    try:
        staging_resolved = staging.resolve()
    except OSError as exc:
        raise CorruptCacheError(f"cannot resolve staging dir {staging}: {exc}") from exc
    for relpath, expected in recorded.items():
        if (
            not relpath
            or relpath in (".", "meta.json")
            or relpath.startswith("/")
            or "\x00" in relpath
            or ".." in Path(relpath).parts
        ):
            raise CorruptCacheError(f"malformed artifact relpath: {relpath!r} in {staging}")
        candidate = staging_resolved / relpath
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError) as exc:
            raise CorruptCacheError(
                f"cannot resolve staged artifact {relpath!r} in {staging}: {exc}"
            ) from exc
        if not resolved.is_relative_to(staging_resolved):
            raise CorruptCacheError(
                f"staged artifact path escapes staging: {relpath!r} in {staging}"
            )
        if not resolved.is_file():
            raise CorruptCacheError(f"recorded artifact missing: {relpath!r} in {staging}")
        try:
            actual = hash_file(resolved)
        except OSError as exc:
            raise CorruptCacheError(
                f"cannot hash staged artifact {relpath!r} in {staging}: {exc}"
            ) from exc
        if actual != expected:
            raise CorruptCacheError(f"artifact digest mismatch: {relpath!r} in {staging}")
    recorded_set = set(recorded)
    try:
        walk = list(staging.rglob("*"))
    except OSError as exc:
        raise CorruptCacheError(f"cannot enumerate staging dir {staging}: {exc}") from exc
    for path in walk:
        if path.is_dir() and not path.is_symlink():
            continue
        try:
            rel = path.relative_to(staging).as_posix()
        except (OSError, ValueError) as exc:
            raise CorruptCacheError(f"cannot inspect staged entry in {staging}: {exc}") from exc
        if rel == "meta.json":
            continue
        if rel not in recorded_set:
            raise CorruptCacheError(f"unrecorded file in staging: {rel!r} in {staging}")


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
      ``ENOTEMPTY``/``EEXIST``/``EISDIR`` on ``os.rename`` → discard staging.
    - Cleans staging in all error paths (populate_fn exception, rename race,
      unexpected OSError) — no orphan ``cache/.staging-*`` left behind.
    """
    _validate_target(target)

    # Fast-path: existing final entry never overwritten
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"cache entry exists but is not a directory: {target}")
        return False

    cache_dir = target.parent
    staging = _staging_dir_for(target)

    # Sweep stale orphans from previous unclean crash (best-effort, ignore errors)
    # Only siblings matching the staging prefix are considered. A sibling whose
    # embedded PID is still alive is NEVER removed — deleting another live
    # process's staging dir would corrupt its in-flight populate_fn. Dead-PID
    # dirs are reaped only after a mtime grace window.
    try:
        cache_root = cache_dir.parent
        if cache_root.exists():
            now = time.time()
            for orphan in cache_root.glob(f"{CACHE_STAGING_PREFIX}*"):
                # Defensive: only remove directories that look like staging
                if not orphan.is_dir():
                    continue
                if _staging_dir_is_live(orphan.name):
                    continue
                try:
                    if now - orphan.stat().st_mtime < STAGING_REAP_GRACE_SECONDS:
                        continue
                except OSError:
                    continue
                shutil.rmtree(orphan, ignore_errors=True)
    except OSError:
        pass

    # Ensure cache_dir exists (e.g. state_root/cache/wallpapers)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # staging is sibling of cache/, so parent (state_root/cache) already exists after above
    # but ensure parent exists for robustness
    staging.parent.mkdir(parents=True, exist_ok=True)

    try:
        staging.mkdir(parents=True, exist_ok=False)
        populate_fn(staging)
        # Guard: populate_fn must have created at least one file and meta.json
        if not any(staging.iterdir()):
            raise RuntimeError(f"populate_fn left staging empty: {staging}")
        if not (staging / "meta.json").is_file():
            raise RuntimeError(f"populate_fn must create meta.json in staging: {staging}")

        # Digest enforcement (AD-26, Story 1.5): verify staged bytes against
        # recorded digests BEFORE the rename. Raises CorruptCacheError while
        # the entry is still invisible; cleanup below removes staging.
        verify_staging(staging)

        try:
            os.rename(staging, target)
        except FileExistsError:
            # Lost race — another writer created target first
            shutil.rmtree(staging, ignore_errors=True)
            return False
        except OSError as e:
            # Handle directory-exists variants; keep narrow string fallback only when errno is None
            # (covers OSError("File exists") without errno as used in tests). Avoids locale-fragile
            # over-broad suppression when errno is set (e.g., ENOSPC with "File exists" in log).
            if e.errno in (errno.ENOTEMPTY, errno.EEXIST, errno.EISDIR):
                shutil.rmtree(staging, ignore_errors=True)
                return False
            if e.errno is None and "File exists" in str(e):
                shutil.rmtree(staging, ignore_errors=True)
                return False
            raise
        return True
    except BaseException:
        # populate_fn failed or rename failed — clean staging, never leave partial target
        shutil.rmtree(staging, ignore_errors=True)
        raise
    finally:
        # Defensive: if staging still exists (e.g., rename succeeded but outer
        # exception after return), ensure removed. Single cleanup path.
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
