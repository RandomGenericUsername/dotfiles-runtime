"""Inotify watch source over the AD-39 watch-root set (AD-40).

The ONLY module that talks to inotify; it is intentionally thin (the
coalescing/decision logic is transport-free in ``application/watch.py``).
Implemented with ``ctypes`` over the process libc — no new dependency — and
Linux-only.

Properties pinned to AD-40:

- watches are installed **explicitly per level** to the root's bounded depth
  (``watch_roots.WatchRoot.depth``); a file root is watched via its
  immediate parent filtered to the exact filename (atomic-replace safe);
- ``IN_CREATE`` of a directory inside a watched tree extends the watch if it
  is still within depth;
- ``IN_Q_OVERFLOW``, ``IN_IGNORED``, ``IN_MOVE_SELF`` and
  ``IN_DELETE_SELF`` are surfaced as typed events; the coordinator forces a
  re-scan / rebuild;
- **no polling**: the source blocks on the inotify fd (plus a stop pipe via
  ``select``) and never stats or re-scans the roots on a timer.

A root that cannot be watched (missing, on a filesystem without inotify,
``max_user_watches`` exhaustion) is surfaced with a warning and left
unwatched — never silently ignored.
"""

from __future__ import annotations

import collections
import ctypes
import logging
import os
import select
import stat as stat_module
import struct
from collections.abc import Iterator
from pathlib import Path

from runtime.adapters.watch_roots import WatchRoot
from runtime.domain.watch import WatchEvent, WatchEventKind
from runtime.ports.watch_source import IWatchSource

logger = logging.getLogger(__name__)

# ── inotify masks (linux/inotify.h) ──────────────────────────────────
IN_MODIFY = 0x00000002
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_CREATE = 0x00000100
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_Q_OVERFLOW = 0x00004000
IN_IGNORED = 0x00008000
IN_ONLYDIR = 0x01000000
IN_ISDIR = 0x40000000

#: inotify_init1 flags.
IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000

_CONTENT_MASK = IN_MODIFY | IN_MOVED_FROM | IN_MOVED_TO | IN_CREATE | IN_DELETE
_SELF_MASK = IN_DELETE_SELF | IN_MOVE_SELF
_DIR_WATCH_MASK = _CONTENT_MASK | _SELF_MASK | IN_ONLYDIR

#: struct inotify_event header: int wd; uint32 mask; uint32 cookie; uint32 len.
_EVENT_HEADER = struct.Struct("iIII")
#: Bytes read from the inotify fd per read(2).
_READ_SIZE = 65536


def _libc() -> ctypes.CDLL:
    """Return a libc handle with inotify argtypes declared."""
    lib = ctypes.CDLL(None, use_errno=True)
    lib.inotify_init1.argtypes = (ctypes.c_int,)
    lib.inotify_init1.restype = ctypes.c_int
    lib.inotify_add_watch.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
    lib.inotify_add_watch.restype = ctypes.c_int
    lib.inotify_rm_watch.argtypes = (ctypes.c_int, ctypes.c_int)
    lib.inotify_rm_watch.restype = ctypes.c_int
    return lib


class _Watch:
    """One installed watch: the directory watched and an optional filename filter."""

    __slots__ = ("directory", "filename", "depth", "root_index")

    def __init__(self, directory: str, filename: str | None, depth: int, root_index: int) -> None:
        self.directory = directory
        self.filename = filename
        self.depth = depth
        self.root_index = root_index


class InotifyWatchSource(IWatchSource):
    """Blocking inotify source over an enumerated :class:`WatchRoot` set."""

    def __init__(self, roots: tuple[WatchRoot, ...]) -> None:
        self._roots = roots
        self._lib = _libc()
        self._fd: int | None = None
        self._stop_r: int | None = None
        self._stop_w: int | None = None
        self._watches: dict[int, _Watch] = {}
        self._pending: collections.deque[WatchEvent] = collections.deque()
        self._closed = False

    # ── lifecycle ────────────────────────────────────────────────────
    def start(self) -> None:
        if self._fd is not None:
            raise RuntimeError("inotify watch source already started")
        self._closed = False
        fd = self._lib.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if fd < 0:
            errno = ctypes.get_errno()
            raise OSError(errno, f"inotify_init1 failed: {os.strerror(errno)}")
        self._fd = fd
        self._stop_r, self._stop_w = os.pipe()
        os.set_blocking(self._stop_r, False)
        os.set_blocking(self._stop_w, False)
        for index, root in enumerate(self._roots):
            self._install_root(index, root)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        stop_w, self._stop_w = self._stop_w, None
        if stop_w is not None:
            try:
                os.write(stop_w, b"x")
            except OSError:
                pass
            os.close(stop_w)
        stop_r, self._stop_r = self._stop_r, None
        if stop_r is not None:
            os.close(stop_r)
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        self._watches.clear()
        self._pending.clear()

    # ── watch installation ───────────────────────────────────────────
    def rebuild(self) -> None:
        """Remove every watch and re-install the enumerated roots (AD-40)."""
        fd = self._require_fd()
        for wd in list(self._watches):
            self._lib.inotify_rm_watch(fd, wd)
        self._watches.clear()
        self._pending.clear()
        for index, root in enumerate(self._roots):
            self._install_root(index, root)

    def _install_root(self, index: int, root: WatchRoot) -> None:
        path = root.path
        if root.is_directory:
            self._install_dir_root(index, path, root.depth)
        else:
            self._install_file_root(index, path)

    def _install_file_root(self, index: int, path: Path) -> None:
        """Watch the immediate parent filtered to the exact filename."""
        parent = path.parent
        if not parent.is_dir():
            logger.warning("watch: file root parent missing, not watched: %s", path)
            return
        self._add_watch(parent, filename=path.name, depth=0, root_index=index)

    def _install_dir_root(self, index: int, path: Path, max_depth: int) -> None:
        if not path.is_dir():
            logger.warning("watch: directory root missing, not watched: %s", path)
            return
        root_str = str(path)
        for dirpath, dirnames, _filenames in os.walk(path):
            rel = os.path.relpath(dirpath, path)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
            self._add_watch(Path(dirpath), filename=None, depth=depth, root_index=index)
            if depth >= max_depth:
                dirnames[:] = []
        logger.debug("watch: installed tree watches under %s", root_str)

    def _add_watch(
        self, directory: Path, *, filename: str | None, depth: int, root_index: int
    ) -> None:
        wd = self._lib.inotify_add_watch(
            self._require_fd(), os.fsencode(str(directory)), _DIR_WATCH_MASK
        )
        if wd < 0:
            errno = ctypes.get_errno()
            logger.warning(
                "watch: cannot watch %s (%s); root(s) at it stay unwatched",
                directory,
                os.strerror(errno),
            )
            return
        self._watches[wd] = _Watch(str(directory), filename, depth, root_index)

    # ── event reading ────────────────────────────────────────────────
    def read_event(self, timeout: float | None = None) -> WatchEvent | None:
        if self._pending:
            return self._pending.popleft()
        fd = self._require_fd()
        stop_r = self._stop_r
        read_fds = [fd] if stop_r is None else [fd, stop_r]
        try:
            ready, _, _ = select.select(read_fds, [], [], timeout)
        except InterruptedError:
            return None
        if stop_r is not None and stop_r in ready:
            return None
        if fd not in ready:
            return None
        try:
            data = os.read(fd, _READ_SIZE)
        except BlockingIOError:
            return None
        except OSError:
            if self._closed:
                return None
            raise
        if not data:
            return None
        for wd, mask, name in self._parse(data):
            self._dispatch(wd, mask, name)
        return self._pending.popleft() if self._pending else None

    def _parse(self, data: bytes) -> Iterator[tuple[int, int, str]]:
        offset = 0
        total = len(data)
        while offset + _EVENT_HEADER.size <= total:
            wd, mask, _cookie, name_len = _EVENT_HEADER.unpack_from(data, offset)
            offset += _EVENT_HEADER.size
            raw_name = data[offset : offset + name_len]
            offset += name_len
            name = raw_name.split(b"\x00", 1)[0].decode("utf-8", "replace")
            yield wd, mask, name

    def _dispatch(self, wd: int, mask: int, name: str) -> None:
        if mask & IN_Q_OVERFLOW or wd < 0:
            self._pending.append(WatchEvent(kind=WatchEventKind.OVERFLOW))
            return
        watch = self._watches.get(wd)
        if mask & IN_IGNORED:
            self._watches.pop(wd, None)
            self._pending.append(
                WatchEvent(
                    kind=WatchEventKind.IGNORED,
                    path=watch.directory if watch else "",
                    is_directory=bool(watch and watch.filename is None),
                )
            )
            return
        if watch is None:
            return
        if mask & IN_DELETE_SELF:
            self._watches.pop(wd, None)
            self._pending.append(
                WatchEvent(kind=WatchEventKind.DELETE_SELF, path=watch.directory)
            )
            return
        if mask & IN_MOVE_SELF:
            self._watches.pop(wd, None)
            self._pending.append(WatchEvent(kind=WatchEventKind.MOVE_SELF, path=watch.directory))
            return

        if watch.filename is not None and name != watch.filename:
            return  # file root: only the exact filename is relevant
        full_path = os.path.join(watch.directory, name) if name else watch.directory
        is_dir = bool(mask & IN_ISDIR)

        if mask & IN_CREATE:
            self._extend_watch(watch, full_path, is_dir)
            self._pending.append(
                WatchEvent(kind=WatchEventKind.CREATED, path=full_path, is_directory=is_dir)
            )
            return
        if mask & IN_MODIFY:
            self._pending.append(
                WatchEvent(kind=WatchEventKind.MODIFIED, path=full_path, is_directory=is_dir)
            )
            return
        if mask & IN_DELETE:
            self._pending.append(
                WatchEvent(kind=WatchEventKind.DELETED, path=full_path, is_directory=is_dir)
            )
            return
        if mask & IN_MOVED_FROM:
            self._pending.append(
                WatchEvent(kind=WatchEventKind.MOVED_FROM, path=full_path, is_directory=is_dir)
            )
            return
        if mask & IN_MOVED_TO:
            self._extend_watch(watch, full_path, is_dir)
            self._pending.append(
                WatchEvent(kind=WatchEventKind.MOVED_TO, path=full_path, is_directory=is_dir)
            )

    def _extend_watch(self, watch: _Watch, full_path: str, is_dir: bool) -> None:
        """Watch a newly created directory if still within the root's depth."""
        if not is_dir or watch.filename is not None:
            return
        root = self._roots[watch.root_index]
        max_depth = root.depth if root.is_directory else 0
        child_depth = watch.depth + 1
        if child_depth > max_depth:
            return
        try:
            st = os.lstat(full_path)
        except OSError:
            return
        if not stat_module.S_ISDIR(st.st_mode):
            return
        self._add_watch(
            Path(full_path), filename=None, depth=child_depth, root_index=watch.root_index
        )

    def _require_fd(self) -> int:
        if self._fd is None:
            raise RuntimeError("inotify watch source is not started")
        return self._fd
