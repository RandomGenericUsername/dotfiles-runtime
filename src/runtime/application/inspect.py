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

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from runtime.adapters.cache import cache_entry_path
from runtime.domain.models import (
    DEFAULT_MONITOR,
    DesktopState,
)
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)

_ABSENT_STATE_MESSAGE = (
    "no state recorded — run `dotfiles-provision apply` / "
    "`dotfiles-runtime wallpaper set <img>` to seed"
)

LinkStatusKind = Literal["ok", "missing", "diverged", "dangling"]

HistoryTrigger = Literal["seed", "set", "reconcile", "force"]

_VALID_HISTORY_TRIGGERS = frozenset({"seed", "set", "reconcile", "force"})

_HISTORY_FIELDS = (
    "ts",
    "trigger",
    "wallpaper",
    "palette",
    "effects",
    "icons",
    "source_path",
)


@dataclass(frozen=True, slots=True)
class HistoryRecord:
    """One parsed ``history.jsonl`` line (AC 1, AR-9).

    The pinned 7-field line schema — ``ts``, ``trigger``, ``wallpaper``,
    ``palette``, ``effects``, ``icons``, ``source_path`` — surfaced
    verbatim (never hex-validated, never reformatted). The three layer
    hashes are ``str | None`` (a ``None`` layer degraded at write time).
    There is deliberately NO ``schema_version`` field (version lives only
    in ``current.json``).
    """

    ts: str
    trigger: str
    wallpaper: str
    palette: str | None
    effects: str | None
    icons: str | None
    source_path: str

    def to_dict(self) -> dict[str, str | None]:
        """Return the full 7-field dict in pinned schema order."""
        return {
            "ts": self.ts,
            "trigger": self.trigger,
            "wallpaper": self.wallpaper,
            "palette": self.palette,
            "effects": self.effects,
            "icons": self.icons,
            "source_path": self.source_path,
        }


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
        exactly (name set + traversal guard + empty-monitors
        ``DEFAULT_MONITOR`` fallback) but performs NO filesystem
        writes — this is the read-only counterpart used for inspection.
        """
        targets: dict[str, Path] = {}
        # Mirror reconcile's run-layer fallback so an empty-monitors state
        # still reflects the default-monitor link reconcile manages.
        for monitor_name in list(state.monitors) or [DEFAULT_MONITOR]:
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


class InspectHistoryUseCase:
    """Read-only newest-first reader of the append-only history log (FR-7, CAP-7).

    Projects ``history.jsonl`` (oldest-first on disk, append-only via
    ``CacheSeeder.append_history``) into newest-first :class:`HistoryRecord`
    entries. Read-only invariant (AC 4): the use case performs only
    ``Path`` reads (``is_symlink``/``exists``/``open``) — never
    ``os.open(..., O_APPEND)``, never ``save()``, never seeder/mutex
    construction. Constructor takes ONLY ``state_root`` (history is a flat
    file, not the ``current.json`` index); ``current.json`` is never read
    and never required.

    Corrupt-line policy (AC 5): a non-trailing corrupt line (non-JSON or
    schema-violating) raises ``ValueError`` loudly with the 1-based line
    number; a trailing partial line (the torn-write crash artifact) is
    tolerated — skipped with a ``logger.warning``, parseable prefix still
    returned newest-first. A symlinked ``history.jsonl`` raises
    ``ValueError`` (mirror the writer's O_NOFOLLOW hardening) — never
    followed.
    """

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root

    def run(self, limit: int = 20) -> list[HistoryRecord]:
        """Read history newest-first, bounded by ``limit`` (read-only).

        Args:
            limit: maximum entries returned (newest N); ``0`` means all
                entries, still newest-first.

        Returns:
            Parsed records newest-first (reverse of file order); ``[]``
            when ``history.jsonl`` is absent (AC 3 — never seeded is not
            an error), empty, or blank-lines-only.

        Raises:
            ValueError: negative ``limit``; symlinked history file; a
                non-trailing corrupt line (with line number); a trailing
                schema-violating line; an unknown ``trigger`` value.
            OSError: on filesystem read failures.
        """
        if limit < 0:
            raise ValueError(f"history limit must be >= 0, got {limit}")
        history_path = self._state_root / "history.jsonl"
        if history_path.is_symlink():
            raise ValueError(
                f"history.jsonl is a symlink (refusing to follow): {history_path}",
            )
        if not history_path.exists():
            return []

        oldest_first: list[HistoryRecord] = []
        try:
            with history_path.open("r", encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError as exc:
                        if self._has_nonblank_tail(handle):
                            raise ValueError(
                                f"history.jsonl line {lineno}: not valid JSON ({exc.msg})",
                            ) from exc
                        logger.warning("history: skipping torn trailing line %d", lineno)
                        break
                    oldest_first.append(self._parse_record(obj, lineno))
        except UnicodeDecodeError as exc:
            # A torn multi-byte tail can surface at the iterator itself
            # rather than inside json.loads — same tolerance: the crash
            # artifact lives at the tail, never in the middle.
            logger.warning("history: skipping torn trailing line (%s)", exc)
        newest_first = oldest_first[::-1]
        if limit == 0:
            return newest_first
        return newest_first[:limit]

    @staticmethod
    def _has_nonblank_tail(handle: Iterator[str]) -> bool:
        """Return True if any non-blank line remains in the open handle."""
        for remaining in handle:
            if remaining.strip():
                return True
        return False

    @staticmethod
    def _parse_record(obj: object, lineno: int) -> HistoryRecord:
        """Validate one decoded line against the pinned 7-field schema."""
        if not isinstance(obj, dict):
            raise ValueError(
                f"history.jsonl line {lineno}: expected a JSON object, got {type(obj).__name__}",
            )
        keys = set(obj.keys())
        expected = set(_HISTORY_FIELDS)
        if keys != expected:
            raise ValueError(
                f"history.jsonl line {lineno}: expected exactly keys "
                f"{sorted(expected)}, got {sorted(keys)}",
            )
        trigger = obj["trigger"]
        if trigger not in _VALID_HISTORY_TRIGGERS:
            raise ValueError(
                f"history.jsonl line {lineno}: unknown trigger {trigger!r} "
                f"(expected one of seed|set|reconcile|force)",
            )
        ts = obj["ts"]
        wallpaper = obj["wallpaper"]
        source_path = obj["source_path"]
        palette = obj["palette"]
        effects = obj["effects"]
        icons = obj["icons"]
        if not isinstance(ts, str):
            raise ValueError(f"history.jsonl line {lineno}: 'ts' must be a string")
        if not isinstance(wallpaper, str):
            raise ValueError(f"history.jsonl line {lineno}: 'wallpaper' must be a string")
        if not isinstance(source_path, str):
            raise ValueError(f"history.jsonl line {lineno}: 'source_path' must be a string")
        for field_name, value in (
            ("palette", palette),
            ("effects", effects),
            ("icons", icons),
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"history.jsonl line {lineno}: {field_name!r} must be a string or null",
                )
        return HistoryRecord(
            ts=ts,
            trigger=trigger,
            wallpaper=wallpaper,
            palette=palette,
            effects=effects,
            icons=icons,
            source_path=source_path,
        )
