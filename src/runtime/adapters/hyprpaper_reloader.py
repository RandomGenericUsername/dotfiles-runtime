"""Hyprpaper wallpaper reload adapter — per-monitor IPC channel (AD-17, FR-6, R5).

Implements ``IDesktopReloader``: after the swap repoints
``current/wallpaper-<monitor>.png`` (AD-17 per-monitor consumer wiring),
the adapter applies the VERIFIED hyprpaper wallpaper-change channel, once
per monitor::

    hyprctl hyprpaper wallpaper <monitor>,<path>

Verified channel (rt-2.5 channel-verification spike, recorded per FR-6;
ARCHITECTURE-SPINE.md Deferred item closed 2026-09-02): hyprpaper v0.8.4
is the Hyprwire rewrite, and its ONLY programmatic in-place wallpaper-
change channel is the per-monitor ``hyprctl hyprpaper wallpaper`` IPC —
reload-after-symlink-repoint is UNAVAILABLE (no ``reload`` request exists;
the config is parsed at startup and a ``wallpaper =`` line is only re-read
on process restart, verified in hyprpaper tag v0.8.4 ``src/ipc/IPC.cpp``).
Evidence: the Hyprland 0.56.2 hyprctl source
(``hyprctl/src/hyprpaper/Hyprpaper.cpp``) exposes exactly two requests —
``wallpaper`` and ``listactive``; the old flat ``preload``/``unload``/
``reload``/``setcolor`` requests are GONE (confirmed against the installed
binary's strings and ``hyprctl hyprpaper --help``), so there is NO preload
step. ``doWallpaper`` splits the request RHS on commas
(``CVarList2 args(RHS, 0, ',')``: MONITOR=args[0], PATH=args[1],
FIT=args[2]); hyprctl space-joins its argv into the request, so the
subprocess argv passes the whole ``<monitor>,<abs-path>`` as ONE
space-free comma-delimited element. The path is canonicalized server-side
(must exist on disk), the socket is
``$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.hyprpaper.sock`` and
the monitor must exist; failures arrive on stdout as ``error: <detail>``
with a non-zero exit.

Fit-mode mapping (recorded for the future AD-18 backend story): hyprpaper
``fitFromString`` accepts ``cover`` (default), ``contain``, ``tile`` and
``fit``/``stretch``; ``fill``/``center`` are unrepresentable. This adapter
OMITS the fit field entirely (the argv element is just
``<monitor>,<abs-path>``) so hyprpaper applies its ``cover`` default —
matching the seeded default fit_mode (AD-11/1.13) and keeping the adapter
free of a ``current.json`` read. Mapping table: cover→cover,
contain→contain, tile→tile, stretch→stretch, fill→cover (unrepresentable),
center→cover (unrepresentable).

A resolved path containing a space is NOT representable through this
channel (the space would corrupt hyprctl's argv join and hyprpaper's comma
split); this is safe for the hash-named default cache layout — a
space-containing state_root surfaces as an invocation failure (known
limitation).

Naming note: this is the RELOADER (mirrors ``hyprland_reloader.py`` /
``ags_reloader.py``). It is NOT the AD-18 ``IStaticWallpaperBackend``
listed as ``hyprpaper_backend`` in the spine adapters manifest — that
per-monitor backend hierarchy is future scope.

Per-monitor enumeration is FS-authority (NFR-3): ``reload()`` scans
``state_root/current/wallpaper-*.png`` (sorted for determinism), derives
``monitor`` from the filename and applies the channel per monitor; the
binary path comes from ``link.resolve()``. A dangling wallpaper symlink is
a surfaced failure (the swap produced a broken consumer). Zero matches (or
no ``current/`` dir) is a vacuous ``True`` — there is no wallpaper to
update. Any per-monitor failure returns ``False`` with a warning log
naming the monitor; the reconcile use case collects the class name into
``ReconcileResult.reload_failures`` — mirroring ``HyprlandReloader`` /
``AgsReloader``. Missing ``hyprctl`` in PATH is a surfaced failure, not a
skip (spec-literal R5, carried decision).

Binary resolution imports ``_resolve_via_which`` from ``hyprland_reloader``
(adapters→adapters import — layering-green); the
separator/executable-file/bare-name branch structure mirrors
``hyprland_reloader._resolve_hyprctl`` / ``ags_reloader._resolve_ags``.

References: [ARCHITECTURE-SPINE.md:235 — Deferred "Hyprpaper wallpaper
channel", closed 2026-09-02], [shared-data-contract.md swap step 5],
[SPEC Open Question "Hyprpaper wallpaper channel", closed 2026-09-02].
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from runtime.adapters.hyprland_reloader import _resolve_via_which
from runtime.ports.desktop_reloader import IDesktopReloader

logger = logging.getLogger(__name__)


def _resolve_state_root(state_root: Path | None) -> Path:
    """Resolve the runtime state root (absolute).

    Mirrors ``cli/main.py`` ``_resolve_state_root`` semantics:
    ``$XDG_STATE_HOME/dotfiles`` (default ``~/.local/state/dotfiles``),
    expanded and resolved to an absolute path. An explicit ``state_root``
    (tests, composition root) is used as-is after the same expansion.
    """
    if state_root is not None:
        return state_root.expanduser().resolve()
    xdg_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return (xdg_state / "dotfiles").expanduser().resolve()


def _resolve_hyprctl(hyprctl_path: Path | None) -> Path | None:
    """Resolve ``hyprctl`` binary path.

    When ``hyprctl_path`` is ``None``, search ``PATH`` via
    ``shutil.which`` and verify executable access. When a path is
    explicitly provided, mirror ``hyprland_reloader._resolve_hyprctl``'s
    branch structure: a path containing separators is verified as an
    executable file, while a bare name is resolved via ``shutil.which``.
    Any unresolvable input yields ``None`` for fail-later behaviour.
    """
    if hyprctl_path is not None:
        candidate = str(hyprctl_path)
        if (
            os.path.sep in candidate
            or (os.path.altsep and os.path.altsep in candidate)
            or "\\" in candidate
        ):
            if hyprctl_path.is_file():
                try:
                    return hyprctl_path if os.access(candidate, os.X_OK) else None
                except OSError:
                    return None
            return None
        return _resolve_via_which(candidate)
    return _resolve_via_which("hyprctl")


class HyprpaperReloader(IDesktopReloader):
    """Adapter that applies the verified hyprpaper wallpaper IPC per monitor.

    Args:
        hyprctl_path: explicit path to ``hyprctl``. When ``None``,
            resolved via ``shutil.which("hyprctl")``; if not found, the
            adapter stores ``None`` and ``reload()`` returns ``False``
            without spawning a subprocess (surfaced failure, not a skip).
        state_root: explicit runtime state root. When ``None``, resolved
            to ``$XDG_STATE_HOME/dotfiles`` (default ``~/.local/state/
            dotfiles``). The CLI composition root passes the SAME
            state_root the reconcile use case writes.
    """

    def __init__(self, hyprctl_path: Path | None = None, state_root: Path | None = None) -> None:
        self._hyprctl_path: Path | None = _resolve_hyprctl(hyprctl_path)
        self._state_root: Path = _resolve_state_root(state_root)

    def reload(self) -> bool:
        """Apply the verified hyprpaper IPC once per seeded monitor.

        Returns:
            True only when EVERY monitor's ``hyprctl hyprpaper wallpaper``
            invocation succeeds. Zero ``current/wallpaper-*.png`` symlinks
            (or no ``current/`` dir) is a vacuous ``True``. False on a
            missing ``hyprctl``, a dangling symlink, a non-zero exit
            (hyprctl prints ``error: <detail>``), a timeout, or any
            subprocess error — each logged with the monitor name.
        """
        if self._hyprctl_path is None:
            logger.warning("hyprctl not found in PATH; Hyprpaper reload skipped")
            return False
        current_dir = self._state_root / "current"
        if not current_dir.is_dir():
            logger.debug("no current/ dir under %s; no wallpaper to apply", self._state_root)
            return True
        links = sorted(current_dir.glob("wallpaper-*.png"))
        if not links:
            logger.debug("no wallpaper-*.png symlinks in %s; nothing to apply", current_dir)
            return True
        all_ok = True
        for link in links:
            monitor = link.name[10:-4]
            if link.is_symlink() and not link.exists():
                logger.warning("Hyprpaper reload failed for %s: dangling symlink %s", monitor, link)
                all_ok = False
                continue
            target = link.resolve()
            try:
                result = subprocess.run(
                    [str(self._hyprctl_path), "hyprpaper", "wallpaper", f"{monitor},{target}"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (
                FileNotFoundError,
                PermissionError,
                subprocess.TimeoutExpired,
                OSError,
                ValueError,
            ) as exc:
                logger.warning("Hyprpaper reload failed for %s: %s", monitor, exc)
                all_ok = False
                continue
            if result.returncode != 0:
                logger.warning(
                    "Hyprpaper reload failed for %s (exit %d): %s%s",
                    monitor,
                    result.returncode,
                    result.stdout,
                    result.stderr,
                )
                all_ok = False
        return all_ok
