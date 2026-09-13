"""Last-converged backstop input hash — computed over exactly the watched set.

AD-36/AD-39: the backstop is a hash of the four derivation inputs plus the
intent document, computed over **exactly** the watched root set (no deeper),
so a hash change always corresponds to a fireable inotify event. Directory
roots are hashed to their bounded watch depth (``WatchRoot.depth``) rather
than with the unbounded ``canonical_hash_dir``.

The computation is total and non-raising: a missing/unreadable/symlinked
input contributes a stable sentinel string. Absent inputs therefore have a
stable hash; if one appears later the hash changes and the daemon converges.
A corrupt intent document is not read here (the reader owns validation) —
only its raw bytes are hashed when it is a regular file.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from runtime.adapters.hashing import hash_file
from runtime.adapters.watch_roots import WatchRoot

#: Stable sentinels for non-hashable input states (never a real sha256).
_SENTINEL_ABSENT = "__absent__"
_SENTINEL_UNREADABLE = "__unreadable__"
_SENTINEL_SYMLINK = "__symlink__"
_SENTINEL_WRONG_KIND = "__wrong-kind__"


def _file_component(path: Path) -> str:
    """Hash one file root; sentinel when absent/symlinked/wrong kind."""
    try:
        st = path.lstat()
    except FileNotFoundError:
        return _SENTINEL_ABSENT
    except OSError:
        return _SENTINEL_UNREADABLE
    if stat.S_ISLNK(st.st_mode):
        return _SENTINEL_SYMLINK
    if not stat.S_ISREG(st.st_mode):
        return _SENTINEL_WRONG_KIND
    try:
        return hash_file(path)
    except OSError:
        return _SENTINEL_UNREADABLE


def _bounded_dir_component(root: Path, max_depth: int) -> str:
    """Canonical hash of a directory tree bounded to ``max_depth`` levels.

    Depth 0 = files directly in ``root``. A subdirectory at depth ``d`` is
    descended only while ``d < max_depth``; files in a directory at
    ``max_depth`` are still included (so a change a watcher can observe is a
    change the hash observes).
    """
    try:
        st = root.lstat()
    except FileNotFoundError:
        return _SENTINEL_ABSENT
    except OSError:
        return _SENTINEL_UNREADABLE
    if not stat.S_ISDIR(st.st_mode):
        return _SENTINEL_WRONG_KIND

    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth >= max_depth:
            dirnames[:] = []
        for name in filenames:
            file_path = Path(dirpath) / name
            try:
                rel = file_path.relative_to(root).as_posix()
            except ValueError:
                continue
            try:
                entries.append((rel, hash_file(file_path)))
            except OSError:
                entries.append((rel, _SENTINEL_UNREADABLE))
    entries.sort(key=lambda item: item[0])
    joined = "\x00".join(f"{rel}\x00{hx}" for rel, hx in entries)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def compute_watch_input_hash(roots: tuple[WatchRoot, ...]) -> str:
    """Hash the four derivation inputs + intent document over the watched set."""
    parts: list[str] = []
    for root in roots:
        if root.is_directory:
            component = _bounded_dir_component(root.path, root.depth)
        else:
            component = _file_component(root.path)
        parts.append(f"{root.path.as_posix()}\x00{component}")
    joined = "\x00".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()
