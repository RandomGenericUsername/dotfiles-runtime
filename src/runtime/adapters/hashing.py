"""Canonical hashing helpers for layered content-addressed cache.

Implements shared-data-contract Derivation-input hashing:

- wallpaper = sha256(file_bytes) chunked
- palette   = sha256(wallpaper_hash || template_set_hash)
- effects   = sha256(wallpaper_hash || catalog_hash)
- icons     = sha256(palette_hash || templates_hash || mappings_hash)

template_set_hash / templates_hash = canonical_hash_dir(sorted relpath:sha)
catalog_hash = hash_file (single file).

Determinism contract (FR-8, Story 1.4):
  Option A (current, proven 2026-08-28): CSG deterministic
  (``tests/integration/.csg_determinism.json`` deterministic:true,
  ``custom`` KMeans(random_state=0)). Cache key stays
  ``sha256(wallpaper_hash || template_set_hash)`` per shared-data-contract.
  Option B (future): if double-run fails, amend to
  ``sha256(... || pinned_seed)`` where pinned_seed is a literal
  (e.g. "v1-seed-0") pinned in runtime.domain + shared-data-contract +
  meta.json. Seam: palette_entry_hash(..., seed=None) would append seed
  before hashing; ``None`` today, spine change if added.

Cache entry contract (Task 4):
  Entry dir = ``cache/<layer>/<hash>/`` where <hash> is the per-layer
  entry_hash (lowercase hex). Future ``meta.json`` MUST contain
  ``hash_algorithm == HASH_ALGORITHM == "sha256"`` plus per-layer input
  hashes and artifact_hashes per shared-data-contract schemas.

``NondeterministicCSGError`` linkage: later cache consumers should raise
``NondeterministicCSGError(AssertionError)`` with
``"cache key must include pinned seed"`` if divergence is observed;
definition lives in ``tests/integration/test_csg_determinism.py``.

D2 decision 2026-08-28: join changed from ``"\\n".join(f"{rel}:{hex}")``
to ``"\\x00".join(f"{rel}\\x00{hex}")`` to avoid ``:``/``\\n`` injection.
Spec divergence noted; spec update required.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final, Literal

HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"

# Build noise that must not pollute canonical_hash_dir. All real
# derivation inputs are *.j2 / *.yaml / *.svg / generic files; these
# patterns are explicitly not derivation inputs.
# Spec allowlist: __pycache__, *.pyc, *.pyo, .git, *.swp, *~
# D3: _IGNORED_EXACT removed (.gitignore now counted per spec).
_IGNORED_NAMES = frozenset({"__pycache__", ".git"})
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})


def _is_ignored(path: Path) -> bool:
    """Return True for build noise that should not affect the hash."""
    if path.name in _IGNORED_NAMES:
        return True
    for part in path.parts:
        if part in _IGNORED_NAMES:
            return True
    if path.suffix in _IGNORED_SUFFIXES:
        return True
    if path.name.endswith("~") or path.name.endswith(".swp"):
        return True
    return False


def _is_hex64(value: str) -> bool:
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value.lower())


def _hash_file_chunked(path: Path) -> str:
    """Chunked SHA-256 of *path* (binary, 64 KiB)."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_file_bytes(data: bytes) -> str:
    """SHA-256 hex (lowercase) of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> str:
    """SHA-256 hex (lowercase) of a single file, chunked.

    Reads ``path`` in 64 KiB chunks so 30-100 MB wallpapers do not OOM.
    Raises ``FileNotFoundError`` if missing, ``IsADirectoryError`` if a
    directory, and ``OSError`` if unreadable.
    Binary only — no text mode / newline translation.

    Separated from ``canonical_hash_dir`` per-file sentinel: single-file
    hashing must raise, directory hashing must sentinel per file.
    """
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        # Explicit guard so callers get documented exception, not bare open error.
        raise IsADirectoryError(path)
    return _hash_file_chunked(path)


def canonical_hash_file(path: Path) -> str:
    """Alias to :func:`hash_file` for single-file catalogs/mappings."""
    return hash_file(path)


def canonical_hash_dir(root: Path) -> str:
    """Canonical hash of a directory per shared-data-contract.

    Canonicalization: sorted list of ``(relpath_posix, sha256(file))``
    joined as ``"\\x00".join(f"{rel}\\x00{hex}")`` then ``sha256(joined)``.
    D2 (2026-08-28): changed from ``"\\n".join(f"{rel}:{hex}")`` to
    ``\\x00``-separated to avoid ``:``/``\\n`` injection; spec update pending.

    Properties:
    - Absolute ``root`` path does not affect hash (only relpaths).
    - ``rglob`` walk order does not affect hash (sorted).
    - Empty dir → ``sha256(b"")`` = ``e3b0c...b855`` (caller should
      treat empty templates dir as config error, not cacheable).
    - Missing ``root`` → ``FileNotFoundError``.
    - Non-dir ``root`` → ``NotADirectoryError``.
    - Per-file ``OSError`` → sentinel ``f"{rel}:unreadable"`` entry,
      still returns a hex hash (does not raise).
    - Symlinks: file symlinks are followed only if target is inside
      ``root`` (``resolve().is_relative_to(root.resolve())``); external
      targets become ``:unreadable`` sentinel. Dir symlinks are not
      recursed (``rglob(follow_symlinks=False)``); broken symlinks become
      ``:unreadable`` sentinel.
    - Build noise (``__pycache__``, ``*.pyc``, ``.git``, ``*.swp``, ``*~``)
      is filtered — dirty checkout does not change hash.
    - Binary chunked reads only; no ``read_text()`` normalization.
    """
    if not root.exists():
        raise FileNotFoundError(root)
    if not root.is_dir():
        raise NotADirectoryError(root)

    entries: list[tuple[str, str]] = []
    root_resolved = root.resolve()

    try:
        # Use try around the generator itself to catch PermissionError on traversal.
        iterator = root.rglob("*")
        for p in iterator:  # noqa: B007
            # Symlink handling first
            if p.is_symlink():
                if not p.exists():
                    # Broken symlink
                    try:
                        rel = p.relative_to(root).as_posix()
                    except ValueError as exc:
                        raise RuntimeError(
                            f"rglob yielded non-descendant {p} for root {root}"
                        ) from exc
                    if _is_ignored(p):
                        continue
                    entries.append((rel, f"{rel}:unreadable"))
                    continue
                # Valid symlink — follow only if file and inside root
                if p.is_file():
                    try:
                        target_resolved = p.resolve()
                        if not target_resolved.is_relative_to(root_resolved):
                            # External target — do not leak outside content; sentinel.
                            rel = p.relative_to(root).as_posix()
                            entries.append((rel, f"{rel}:unreadable"))
                            continue
                    except OSError, ValueError:
                        # Resolve failed — treat as unreadable.
                        try:
                            rel = p.relative_to(root).as_posix()
                        except ValueError as exc:
                            raise RuntimeError(
                                f"rglob yielded non-descendant {p} for root {root}"
                            ) from exc
                        entries.append((rel, f"{rel}:unreadable"))
                        continue
                    # Inside-root file symlink: fall through to hashing
                else:
                    # Dir symlink or other — do not recurse (rglob with
                    # follow_symlinks=False already does not recurse; skip entry)
                    continue

            if not p.is_file():
                continue
            if _is_ignored(p):
                continue

            try:
                rel = p.relative_to(root).as_posix()
            except ValueError as exc:
                raise RuntimeError(f"rglob yielded non-descendant {p} for root {root}") from exc

            try:
                file_hash = _hash_file_chunked(p)
                entries.append((rel, file_hash))
            except OSError:
                entries.append((rel, f"{rel}:unreadable"))
    except OSError as exc:
        # Traversal itself failed (e.g., unreadable subdir). Per AC-2 sentinel
        # contract, do not crash — record as unreadable and continue to hash.
        # Use root-relative sentinel if possible.
        try:
            rel = Path(
                str(exc.filename) if getattr(exc, "filename", None) else "traversal"
            ).as_posix()
        except Exception:
            rel = "traversal"
        # Avoid duplicate sentinel for same rel; just ensure entries captures failure.
        entries.append((rel, f"{rel}:unreadable"))

    if not entries:
        return hashlib.sha256(b"").hexdigest()

    entries.sort(key=lambda x: x[0])
    joined = "\x00".join(f"{rel}\x00{hx}" for rel, hx in entries)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _validate_hex64(name: str, value: str) -> None:
    if not _is_hex64(value):
        raise ValueError(f"{name} must be 64-char lowercase hex sha256, got {value!r}")


def palette_entry_hash(wallpaper_hash: str, template_set_hash: str, seed: str | None = None) -> str:
    """Entry hash for ``cache/palettes/<ph>/``.

    ``sha256(wallpaper_hash || template_set_hash [|| seed])`` as UTF-8 of the
    concatenated 64-char hex strings. Fixed-length hex → no separator
    needed; if inputs ever become variable-length, add ``\\x00``.
    Seed seam (Task 5 / D1): ``seed=None`` today; if a future determinism
    run fails, callers pass a literal pinned_seed (e.g. ``"v1-seed-0"``).

    ``wallpaper_hash`` and ``template_set_hash`` are each expected to be
    64-char lowercase hex.
    """

    _validate_hex64("wallpaper_hash", wallpaper_hash)
    _validate_hex64("template_set_hash", template_set_hash)
    base = f"{wallpaper_hash}{template_set_hash}"
    if seed is not None:
        base += seed
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def effects_entry_hash(wallpaper_hash: str, catalog_hash: str, seed: str | None = None) -> str:
    """Entry hash for ``cache/effects/<eh>/``.

    ``sha256(wallpaper_hash || catalog_hash [|| seed])``.
    """

    _validate_hex64("wallpaper_hash", wallpaper_hash)
    _validate_hex64("catalog_hash", catalog_hash)
    base = f"{wallpaper_hash}{catalog_hash}"
    if seed is not None:
        base += seed
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def icons_entry_hash(
    palette_hash: str, templates_hash: str, mappings_hash: str, seed: str | None = None
) -> str:
    """Entry hash for ``cache/icons/<ih>/``.

    ``sha256(palette_hash || templates_hash || mappings_hash [|| seed])``.
    """

    _validate_hex64("palette_hash", palette_hash)
    _validate_hex64("templates_hash", templates_hash)
    _validate_hex64("mappings_hash", mappings_hash)
    base = f"{palette_hash}{templates_hash}{mappings_hash}"
    if seed is not None:
        base += seed
    return hashlib.sha256(base.encode("utf-8")).hexdigest()
