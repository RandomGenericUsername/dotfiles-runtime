"""AD-39 enumerated watch-root set — the explicit spine-only allowlist.

The watched roots are hard-coded SPINE locations, **never** the result of
``derive.find_*`` (which may return a repo path under the dev override).
This module is the single definition of the watched set; the inotify
adapter installs watches from it and the last-converged backstop hashes
exactly this set, so a backstop hash change always corresponds to a
fireable event.

Excluded by construction: the consumer-pointer destinations
(``<install>/config/ags/colors.css`` etc.) and all of ``state_root``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Bounded recursion depths for directory roots (AD-39). ``icon-templates``
#: nests up to 4 levels; the CSG template tree up to 2; the icon-mappings
#: tree is shallow (its ``icons.yaml`` plus siblings) and bounded at 2.
CSG_TEMPLATES_DEPTH = 2
ICON_TEMPLATES_DEPTH = 4
ICON_MAPPINGS_DEPTH = 2


@dataclass(frozen=True, slots=True)
class WatchRoot:
    """One watched location: a directory tree (bounded) or a single file."""

    path: Path
    is_directory: bool
    depth: int = 0


def enumerate_watch_roots(install_spine: Path, intent_path: Path) -> tuple[WatchRoot, ...]:
    """Return the AD-39 watch-root set for a resolved spine + intent path.

    ``install_spine`` is the read-only install spine (never a repo path);
    ``intent_path`` is the relocated intent document
    (``$XDG_CONFIG_HOME/dotfiles/desired.json``). Both are resolved by the
    caller (composition root).
    """
    return (
        WatchRoot(
            install_spine / "config" / "color-scheme-generator" / "templates",
            is_directory=True,
            depth=CSG_TEMPLATES_DEPTH,
        ),
        WatchRoot(
            install_spine / "config" / "weg" / "effects.yaml",
            is_directory=False,
        ),
        WatchRoot(
            install_spine / "icon-templates",
            is_directory=True,
            depth=ICON_TEMPLATES_DEPTH,
        ),
        WatchRoot(
            install_spine / "icon-mappings",
            is_directory=True,
            depth=ICON_MAPPINGS_DEPTH,
        ),
        WatchRoot(intent_path, is_directory=False),
    )
