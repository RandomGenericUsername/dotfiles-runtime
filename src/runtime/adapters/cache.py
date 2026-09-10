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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Final, Literal

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


def _artifact_index(
    root: Path,
) -> tuple[dict[str, Path], dict[str, list[Path]], list[Path], tuple[str, str] | None]:
    """Index the artifact files under ``root`` for key resolution.

    Returns ``(exact_relpath -> path, basename -> [paths], all_files, error)``.
    ``meta.json`` (root) and ``.meta.json.tmp.*`` leftovers are excluded.
    Symlink-escape and enumeration failures are returned as an error tuple
    ``(status, detail)`` (``status`` is ``"missing"`` or ``"corrupt"``).

    Contract: ``artifact_hashes`` keys are FILENAMES (``{"<filename>.png": h}``),
    but generators nest output (WEG: ``<stem>/{effect,composite,preset}/*``), so
    callers resolve a bare key by filename anywhere under the entry and an
    entry-relative key (contains ``/``) as an exact path.
    """
    try:
        root_resolved = root.resolve()
    except (OSError, ValueError) as exc:
        return {}, {}, [], ("corrupt", f"cannot resolve entry {root}: {exc}")
    files: list[Path] = []
    try:
        for path in root.rglob("*"):
            if path.is_dir() and not path.is_symlink():
                continue
            if not path.is_file():
                continue
            if path.name.startswith(".meta.json.tmp."):
                continue
            rel = path.relative_to(root).as_posix()
            if rel == "meta.json":
                continue
            try:
                resolved = path.resolve()
            except (OSError, ValueError) as exc:
                return {}, {}, [], ("corrupt", f"unresolvable artifact {rel!r}: {exc}")
            if not resolved.is_relative_to(root_resolved):
                return {}, {}, [], ("corrupt", f"artifact escapes entry: {rel!r}")
            files.append(path)
    except OSError as exc:
        return {}, {}, [], ("corrupt", f"cannot enumerate {root}: {exc}")
    exact: dict[str, Path] = {}
    by_base: dict[str, list[Path]] = {}
    for path in files:
        exact[path.relative_to(root).as_posix()] = path
        by_base.setdefault(path.name, []).append(path)
    return exact, by_base, files, None


def resolve_entry_artifact(root: Path, key: str) -> Path | None:
    """Resolve one recorded key to a file under ``root`` (or ``None``).

    Bare key → unique file with that filename anywhere under ``root``;
    key containing ``/`` → exact entry-relative path. Ambiguous/missing/
    escaping → ``None``.
    """
    exact, by_base, _files, error = _artifact_index(root)
    if error is not None:
        return None
    if "/" in key:
        return exact.get(key)
    candidates = by_base.get(key, [])
    return candidates[0] if len(candidates) == 1 else None


def _unsafe_artifact_key(key: str) -> bool:
    return (
        not key
        or key in (".", "meta.json")
        or key.startswith("/")
        or "\x00" in key
        or ".." in Path(key).parts
    )


def match_entry_artifacts(
    root: Path, recorded: Mapping[str, str]
) -> tuple[dict[str, Path], tuple[str, str] | None]:
    """Resolve every recorded key to its file, enforcing completeness.

    Returns ``(key -> path, None)`` on success, else ``({}, (status, detail))``
    with ``status`` ``"missing"`` (a recorded artifact has no file) or
    ``"corrupt"`` (ambiguous key, symlink escape, unrecorded file, walk error).
    Every file under ``root`` must be covered by exactly one recorded key.
    """
    exact, by_base, files, error = _artifact_index(root)
    if error is not None:
        return {}, error
    matched: dict[str, Path] = {}
    for key in recorded:
        if "/" in key:
            path = exact.get(key)
            if path is None:
                return {}, ("missing", f"recorded artifact missing: {key!r} in {root}")
            matched[key] = path
        else:
            candidates = by_base.get(key, [])
            if not candidates:
                return {}, ("missing", f"recorded artifact missing: {key!r} in {root}")
            if len(candidates) > 1:
                return {}, ("corrupt", f"ambiguous artifact key {key!r} in {root}")
            matched[key] = candidates[0]
    covered = set(matched.values())
    for path in files:
        if path not in covered:
            rel = path.relative_to(root).as_posix()
            return {}, ("corrupt", f"unrecorded file in {root}: {rel!r}")
    return matched, None


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
    for key in recorded:
        if _unsafe_artifact_key(key):
            return EntryHealth("corrupt", f"unsafe artifact key: {key}")
    matched, error = match_entry_artifacts(entry_dir, recorded)
    if error is not None:
        status: Literal["missing", "corrupt"] = "missing" if error[0] == "missing" else "corrupt"
        return EntryHealth(status, error[1])
    for key, path in matched.items():
        try:
            actual = hash_file(path)
        except OSError as exc:
            return EntryHealth("corrupt", f"unhashable {key}: {exc}")
        if actual != recorded[key]:
            return EntryHealth("corrupt", f"digest mismatch: {key}")
    return EntryHealth("ok", "verified")


def _annotate_legacy(entry_dir: Path, meta: dict[str, object], meta_path: Path) -> EntryHealth:
    """Hash every non-meta file under ``entry_dir`` and record it (AD-27).

    Keys mirror the generator convention (contract: ``<filename>``; a
    basename-colliding file falls back to its entry-relative path). The root
    ``meta.json`` and any ``.meta.json.tmp.*`` crash leftover are never hashed.
    Writes atomically (sibling tmp + ``os.replace``); preserves other keys +
    the file mode.
    """
    exact, by_base, files, error = _artifact_index(entry_dir)
    _ = exact
    if error is not None:
        return EntryHealth("corrupt", error[1])
    hashes: dict[str, str] = {}
    try:
        for path in files:
            rel = path.relative_to(entry_dir).as_posix()
            key = path.name if len(by_base.get(path.name, [])) == 1 else rel
            hashes[key] = hash_file(path)
    except OSError as exc:
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
    for key in recorded:
        if _unsafe_artifact_key(key):
            raise CorruptCacheError(f"malformed artifact key: {key!r} in {staging}")
    matched, error = match_entry_artifacts(staging, recorded)
    if error is not None:
        raise CorruptCacheError(error[1])
    for key, path in matched.items():
        try:
            actual = hash_file(path)
        except OSError as exc:
            raise CorruptCacheError(
                f"cannot hash staged artifact {key!r} in {staging}: {exc}"
            ) from exc
        if actual != recorded[key]:
            raise CorruptCacheError(f"artifact digest mismatch: {key!r} in {staging}")


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
