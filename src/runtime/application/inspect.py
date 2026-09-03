"""Inspect-state use case — read-only status projection (Epic 3, Story 3.2).

Implements FR-7 / CAP-7 (``inspect status``), NFR-3 / AD-3 (filesystem is
the authority), AD-1 (hexagonal), AD-5 (state_root), AD-14 (domain
purity), AD-20 (CLI rendering).

Scope: projects the current wallpaper/monitors/palette/effects/icons from
``current.json`` AND reflects the live ``current/`` symlink targets under
``state_root``. Read-only invariant (AC 4): the use case MUTATES NOTHING —
no cache population, no ``current/`` repoint, no ``history.jsonl`` append,
no seed side-effects. Safe to run anytime, including mid-swap.

Symlink statuses (AC 2): each expected consumer symlink
(``wallpaper-<monitor>.png``, ``colors.yaml``, ``colors.conf``,
``colors.gtk.css``, ``effects/``, ``icons/`` — the name set mirrors
``ReconcileDesktopStateUseCase._build_expected_targets``) is flagged:

- ``ok``       — live symlink resolves to the expected cache-entry path
- ``missing``  — no symlink at the expected name
- ``diverged`` — symlink exists but resolves elsewhere (NFR-3: the
  filesystem, not the index, wins)
- ``dangling`` — symlink whose target no longer exists (tolerated, never
  crashed — mirrors the reconcile resolve() OSError tolerance)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from runtime.adapters.cache import cache_entry_path
from runtime.domain.models import DesktopState
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)

_ABSENT_STATE_MESSAGE = (
    "no state recorded — run `dotfiles-provision apply` / "
    "`dotfiles-runtime wallpaper set <img>` to seed"
)

LinkStatusKind = Literal["ok", "missing", "diverged", "dangling"]


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """Live status of one expected ``current/`` consumer symlink (AC 2).

    ``target`` is the actual resolved symlink target for ``ok``/``diverged``
    statuses, the raw (broken) target string for ``dangling``, and ``None``
    for ``missing`` (no symlink exists to read).
    """

    status: LinkStatusKind
    target: str | None


@dataclass(frozen=True, slots=True)
class InspectStatusResult:
    """Outcome of one ``InspectStateUseCase.run`` invocation (AC 1, AC 2)."""

    wallpaper: str
    wallpaper_source_path: str
    monitors: dict[str, dict[str, str | None]]
    palette: str | None
    effects: str | None
    icons: str | None
    applied_at: str
    current_symlinks: dict[str, LinkStatus]


class InspectStateUseCase:
    """Read-only status projection of the current desktop state (FR-7, CAP-7).

    Constructor receives the state repository port and the state_root
    (dependency inversion — the use case wires no concrete adapters).
    ``run()`` performs two read-only projections:

    1. Index projection (AC 1): ``state_repo.load_current()`` → wallpaper /
       monitors / palette / effects / icons. Absent state raises
       ``RuntimeError`` loudly (AC 3) — never fabricated, never seeded.
    2. Filesystem reflection (AC 2, NFR-3): the live ``current/`` symlink
       targets under ``state_root / "current"`` are compared (read-only)
       against the expected cache-entry targets derived from the state,
       mirroring ``ReconcileDesktopStateUseCase._build_expected_targets``'s
       name set WITHOUT any repointing or revert behavior.
    """

    def __init__(
        self,
        state_repo: IStateRepository,
        state_root: Path,
    ) -> None:
        self._state_repo = state_repo
        self._state_root = state_root

    def run(self) -> InspectStatusResult:
        """Project current.json + live current/ symlinks (read-only).

        Raises:
            ValueError: propagated from a corrupt ``current.json``, or from
                a state carrying path-traversal monitor names.
            RuntimeError: if ``current.json`` is absent (AC 3 — nothing
                recorded yet; the message names the seeding commands).
            OSError: on filesystem read failures.
        """
        state = self._state_repo.load_current()
        if state is None:
            raise RuntimeError(_ABSENT_STATE_MESSAGE)

        monitors: dict[str, dict[str, str | None]] = {}
        for name, cfg in state.monitors.items():
            monitors[name] = {
                "backend": cfg.backend.value,
                "source_hash": cfg.source_hash,
                "fit_mode": cfg.fit_mode.value,
                "mpv_options": cfg.mpv_options,
                "ipc_socket": cfg.ipc_socket,
            }

        current_symlinks = self._inspect_current_symlinks(state)

        return InspectStatusResult(
            wallpaper=state.wallpaper.content_hash,
            wallpaper_source_path=state.wallpaper.source_path,
            monitors=monitors,
            palette=state.palette.entry_hash if state.palette else None,
            effects=state.effects.entry_hash if state.effects else None,
            icons=state.icons.entry_hash if state.icons else None,
            applied_at=state.applied_at,
            current_symlinks=current_symlinks,
        )

    # ------------------------------------------------------------------
    # AC 2 — live current/ symlink reflection (read-only)
    # ------------------------------------------------------------------

    def _inspect_current_symlinks(self, state: DesktopState) -> dict[str, LinkStatus]:
        """Compare live current/ symlinks against expected targets (read-only)."""
        expected = self._build_expected_targets(state)
        current_dir = self._state_root / "current"
        result: dict[str, LinkStatus] = {}
        for name, expected_target in expected.items():
            result[name] = self._link_status(current_dir / name, expected_target)
        return result

    def _link_status(self, link: Path, expected_target: Path) -> LinkStatus:
        """Classify one consumer symlink against its expected target."""
        if not link.is_symlink():
            # No symlink at all (or a regular file/dir squatting on the name).
            return LinkStatus(status="missing", target=None)
        if not link.exists():
            # Dangling symlink: raw target points nowhere (mid-swap or
            # deleted cache entry). Distinct status — never crash.
            return LinkStatus(status="dangling", target=self._raw_target(link))
        try:
            actual = link.resolve()
        except OSError:
            return LinkStatus(status="dangling", target=self._raw_target(link))
        try:
            expected_resolved = expected_target.resolve()
        except OSError:
            expected_resolved = expected_target
        if actual == expected_resolved:
            return LinkStatus(status="ok", target=str(actual))
        return LinkStatus(status="diverged", target=str(actual))

    @staticmethod
    def _raw_target(link: Path) -> str:
        """Best-effort raw symlink target string (never raises)."""
        try:
            return str(link.readlink())
        except OSError:
            return str(link)

    def _build_expected_targets(self, state: DesktopState) -> dict[str, Path]:
        """Map symlink names to expected cache-entry targets from the state.

        Mirrors ``ReconcileDesktopStateUseCase._build_expected_targets``
        exactly (name set + traversal guard) but performs NO filesystem
        writes — this is the read-only counterpart used for inspection.
        """
        targets: dict[str, Path] = {}
        for monitor_name in state.monitors:
            if (
                "/" in monitor_name
                or "\\" in monitor_name
                or monitor_name.strip() != monitor_name
                or ".." in monitor_name
            ):
                raise ValueError(
                    f"monitor name must not contain path separators, got {monitor_name!r}",
                )
            targets[f"wallpaper-{monitor_name}.png"] = (
                cache_entry_path(
                    self._state_root,
                    "wallpapers",
                    state.wallpaper.content_hash,
                )
                / "wallpaper.png"
            )
        if state.palette is not None:
            pal_dir = cache_entry_path(self._state_root, "palettes", state.palette.entry_hash)
            for artifact in ("colors.conf", "colors.gtk.css", "colors.yaml"):
                targets[artifact] = pal_dir / artifact
        if state.effects is not None:
            targets["effects"] = cache_entry_path(
                self._state_root,
                "effects",
                state.effects.entry_hash,
            )
        if state.icons is not None:
            targets["icons"] = cache_entry_path(
                self._state_root,
                "icons",
                state.icons.entry_hash,
            )
        return targets
