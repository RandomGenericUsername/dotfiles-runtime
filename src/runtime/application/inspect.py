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
import os
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from runtime.adapters.cache import CACHE_LAYERS, cache_entry_path
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
    trigger: HistoryTrigger
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
        try:
            os.lstat(history_path)
        except FileNotFoundError:
            return []
        except NotADirectoryError:
            # state_root is a regular file — same clean absent path as AC 3.
            return []

        # Bounded window when limit > 0 (O(limit) RAM, still validates every
        # line); unbounded only for limit == 0 (explicit all).
        window: list[HistoryRecord] | deque[HistoryRecord] = (
            [] if limit == 0 else deque(maxlen=limit)
        )
        # Count of parseable on-disk lines (torn tail excluded) for callers
        # that need total without a second scan.
        fd = None
        try:
            try:
                fd = os.open(history_path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                # Deleted between lstat and open — same clean path as absent.
                return []
            except OSError as exc:
                # ELOOP = swapped to symlink between check and open.
                raise ValueError(
                    f"history.jsonl is a symlink (refusing to follow): {history_path}",
                ) from exc
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = None  # fdopen owns the fd now
                    for lineno, line in enumerate(handle, start=1):
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError as exc:
                            # Torn-write artifact is a truncated last line
                            # with NO trailing newline. A complete corrupt
                            # line (ends with newline) or anything followed
                            # by more content is a loud middle-corruption.
                            is_truncated_tail = not line.endswith(
                                "\n"
                            ) and not self._has_nonblank_tail(handle)
                            if not is_truncated_tail:
                                raise ValueError(
                                    f"history.jsonl line {lineno}: not valid JSON ({exc.msg})",
                                ) from exc
                            logger.warning("history: skipping torn trailing line %d", lineno)
                            break
                        record = self._parse_record(obj, lineno)
                        if isinstance(window, deque):
                            window.append(record)
                        else:
                            window.append(record)
            except UnicodeDecodeError as exc:
                # Mid-file undecodable bytes must be loud — never silently
                # truncate. Only a torn multi-byte tail is tolerated, and it
                # surfaces at EOF with nothing parseable after it.
                raise ValueError(f"history.jsonl: invalid UTF-8 ({exc})") from exc
        finally:
            if fd is not None:
                os.close(fd)
        if isinstance(window, deque):
            newest_first = list(reversed(window))
        else:
            newest_first = window[::-1]
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
        if not isinstance(trigger, str) or trigger not in _VALID_HISTORY_TRIGGERS:
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
            trigger=cast("HistoryTrigger", trigger),
            wallpaper=wallpaper,
            palette=palette,
            effects=effects,
            icons=icons,
            source_path=source_path,
        )


# ----------------------------------------------------------------------
# AC 1/3/5 — cache listing (Story 3.4, list-only, read-only)
# ----------------------------------------------------------------------

# Canonical pipeline order (NOT alphabetical): wallpapers → palettes →
# effects → icons. Validated against CACHE_LAYERS (adapters) — never
# sorted(CACHE_LAYERS), which would yield effects/icons/palettes/wallpapers.
_CACHE_LAYER_ORDER: tuple[str, str, str, str] = (
    "wallpapers",
    "palettes",
    "effects",
    "icons",
)

_CACHE_STAGING_PREFIX = ".staging-"

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_cache_entry_name(name: str) -> bool:
    """Return True if ``name`` is a 64-char lowercase-hex entry dir name."""
    return len(name) == 64 and all(c in _HEX_DIGITS for c in name.lower())


@dataclass(frozen=True, slots=True)
class CacheLayerListing:
    """One cache layer's sorted entry hashes (AC 1).

    ``entries`` are FULL 64-char lowercase-hex dir names, sorted
    lexicographically.
    """

    layer: str
    entries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InspectCacheResult:
    """Outcome of one ``InspectCacheUseCase.run`` invocation (AC 1, AC 3).

    ``layers`` maps each canonical layer to its sorted entry-hash tuple
    (insertion order is canonical pipeline order); ``counts`` maps each
    layer to ``len(entries)``; ``total`` is the single-scan sum (no
    second read).
    """

    layers: dict[str, tuple[str, ...]]
    counts: dict[str, int]
    total: int


class InspectCacheUseCase:
    """Read-only lister of the layered content-addressed cache (FR-7, CAP-7).

    Reads ONLY ``state_root / "cache"`` dir names (``cache/<layer>/<64hex>``,
    AD-2/AD-8) — never ``meta.json``, never ``current.json``, never the
    ``IStateRepository``. Constructor takes ONLY ``state_root`` (same
    precedent as ``InspectHistoryUseCase``).

    Read-only invariant (AC 4): only ``Path`` reads
    (``is_symlink``/``exists``/``is_dir``/``iterdir``/``scandir``) — never
    ``mkdir``, never ``os.rename``, never ``save()``, never seeder/mutex/
    populator construction, never staging reap.

    Noise policy (AC 5 — diagnostic, not a validator): staging dirs,
    plain files, and symlinks are skipped silently; non-64-hex dir names
    are skipped with ``logger.warning`` (never ``ValueError``). Only a
    symlinked ``cache/`` root raises ``ValueError`` (O_NOFOLLOW mirror),
    and genuine I/O failures (``OSError``) propagate.
    """

    def __init__(self, state_root: Path) -> None:
        self._state_root = state_root

    def run(self) -> InspectCacheResult:
        """List cache entries per layer in canonical order (read-only).

        Returns:
            ``InspectCacheResult`` with all four layers present (``()``
            when the cache or a layer dir is absent — AC 3).

        Raises:
            ValueError: if ``state_root / "cache"`` itself is a symlink
                (refusing to follow, mirror rt-3.3 O_NOFOLLOW).
            OSError: on filesystem read failures.
        """
        if set(_CACHE_LAYER_ORDER) != set(CACHE_LAYERS):
            raise AssertionError(
                f"canonical cache layer order diverged from adapters: "
                f"{_CACHE_LAYER_ORDER!r} vs {sorted(CACHE_LAYERS)!r}",
            )
        cache_root = self._state_root / "cache"
        if cache_root.is_symlink():
            raise ValueError(
                f"cache dir is a symlink (refusing to follow): {cache_root}",
            )
        try:
            is_dir = cache_root.is_dir(follow_symlinks=False)
        except OSError:
            raise
        if not is_dir:
            # Absent cache dir (or a squatting regular file) → all empty
            # (AC 3 — missing cache is not an error; never create dirs).
            return self._empty_result()
        layers: dict[str, tuple[str, ...]] = {}
        for layer in _CACHE_LAYER_ORDER:
            layers[layer] = self._list_layer(cache_root / layer, layer)
        counts = {layer: len(entries) for layer, entries in layers.items()}
        total = sum(counts.values())
        return InspectCacheResult(layers=layers, counts=counts, total=total)

    @staticmethod
    def _empty_result() -> InspectCacheResult:
        """All four layers empty in canonical order (AC 3)."""
        layers: dict[str, tuple[str, ...]] = {layer: () for layer in _CACHE_LAYER_ORDER}
        counts: dict[str, int] = {layer: 0 for layer in _CACHE_LAYER_ORDER}
        return InspectCacheResult(layers=layers, counts=counts, total=0)

    @staticmethod
    def _list_layer(layer_dir: Path, layer: str) -> tuple[str, ...]:
        """List one layer's valid entry hashes, sorted (read-only)."""
        if layer_dir.is_symlink():
            # Symlinked layer — never follow (O_NOFOLLOW mirror).
            return ()
        try:
            is_dir = layer_dir.is_dir(follow_symlinks=False)
        except OSError:
            raise
        if not is_dir:
            # Absent layer (or squatting file) → empty (never create dirs).
            return ()
        entries: list[str] = []
        try:
            with os.scandir(layer_dir) as it:
                for entry in it:
                    # Symlink-first: never follow entry symlinks.
                    try:
                        if entry.is_symlink():
                            continue
                    except OSError:
                        raise
                    name = entry.name
                    if name.startswith(_CACHE_STAGING_PREFIX):
                        # AD-9 transient (silently ignored even inside
                        # a layer — defensive; stagings live at cache/).
                        continue
                    try:
                        is_entry_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        raise
                    if not is_entry_dir:
                        # Plain squatting files — silently ignored.
                        continue
                    if not _is_cache_entry_name(name):
                        logger.warning("cache: skipping non-entry dir %s/%s", layer, name)
                        continue
                    entries.append(name.lower())
        except FileNotFoundError:
            # Layer deleted between is_dir and scandir — same as absent.
            return ()
        entries.sort()
        return tuple(entries)
