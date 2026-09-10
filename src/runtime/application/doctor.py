"""Three-way drift check (Phase 3, Story 2.1).

``DoctorUseCase.check()`` compares ``current.json`` (store) vs ``current/``
symlink targets vs ``cache/<layer>/<hash>/`` existence+health and reports
each item as ok/missing/diverged/dangling. Read-only: ``Path`` reads only
(mirror ``inspect.py``'s read-only invariant — established application-layer
pattern, no new port). Repair belongs to Story 2.2 (extends this class).

Health depth is STRUCTURAL (existence + parse + presence): artifact-hash
RECHECK belongs to Story 3.1, quarantine/repopulate to Story 2.2.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from runtime.adapters.cache import resolve_entry_artifact
from runtime.adapters.hashing import hash_file
from runtime.application.inspect import LinkStatusKind
from runtime.application.reconcile import ReconcileDesktopStateUseCase
from runtime.domain.models import DEFAULT_MONITOR, DesktopState
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)

#: Palette artifacts exposed as current/ symlinks (mirrors
#: seeder.repoint_current_symlinks inventory).
PALETTE_LINK_NAMES: tuple[str, ...] = (
    "colors.conf",
    "colors.gtk.css",
    "colors.yaml",
    "colors.adw.css",
    "colors.sequences",
    "colors.rasi",
)


@dataclass(frozen=True, slots=True)
class DriftItem:
    """One checked item and its divergence verdict.

    ``layer``/``entry_hash`` are populated for ``kind == "entry"`` items
    (repair quarantines by them); ``None`` for symlink items.
    """

    name: str
    kind: str
    status: LinkStatusKind
    detail: str
    layer: str | None = None
    entry_hash: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Outcome of one three-way drift check. Read-only; mutates nothing."""

    items: tuple[DriftItem, ...]
    clean: bool


class DoctorUseCase:
    """Three-way drift detection over store, symlinks, and cache entries."""

    def __init__(self, state_repo: IStateRepository, state_root: Path) -> None:
        self._state_repo = state_repo
        self._state_root = state_root

    def check(self) -> DoctorReport:
        """Run the check; return per-item verdicts. Mutates nothing."""
        state = self._state_repo.load_current()
        if state is None:
            raise ValueError(
                "no runtime state recorded yet (missing current.json); "
                "run `dotfiles-runtime wallpaper set <img>` first"
            )
        items: list[DriftItem] = []
        items.extend(self._check_entries(state))
        items.extend(self._check_links(state))
        clean = all(item.status == "ok" for item in items)
        return DoctorReport(items=tuple(items), clean=clean)

    def _entry_dir(self, layer: str, entry_hash: str) -> Path:
        return self._state_root / "cache" / layer / entry_hash.lower()

    def _check_entries(self, state: DesktopState) -> list[DriftItem]:
        """Leg 1 — store vs cache entries: existence + parse + presence."""
        layers: list[tuple[str, str | None, list[str]]] = [
            ("wallpapers", state.wallpaper.content_hash, ["wallpaper.png"]),
        ]
        if state.palette is not None:
            layers.append(("palettes", state.palette.entry_hash, []))
        if state.effects is not None:
            layers.append(("effects", state.effects.entry_hash, []))
        if state.icons is not None:
            layers.append(("icons", state.icons.entry_hash, []))
        items: list[DriftItem] = []
        for layer, entry_hash, fixed_files in layers:
            assert entry_hash is not None
            drift_name = f"cache/{layer}/{entry_hash[:12]}"

            def _item(
                status: LinkStatusKind,
                detail: str,
                name: str = drift_name,
                layer_name: str = layer,
                hash_value: str = entry_hash,
            ) -> DriftItem:
                return DriftItem(
                    name, "entry", status, detail, layer=layer_name, entry_hash=hash_value
                )

            entry_dir = self._entry_dir(layer, entry_hash)
            if not entry_dir.is_dir():
                items.append(_item("missing", f"{entry_dir} absent"))
                continue
            meta_path = entry_dir / "meta.json"
            listed: list[str] = list(fixed_files)
            try:
                raw = meta_path.read_text(encoding="utf-8")
            except OSError, UnicodeDecodeError:
                items.append(_item("diverged", f"{meta_path} unreadable"))
                continue
            try:
                meta = json.loads(raw)
            except json.JSONDecodeError as exc:
                items.append(_item("diverged", f"meta.json unparseable: {exc}"))
                continue
            if not isinstance(meta, dict):
                items.append(_item("diverged", "meta.json not an object"))
                continue
            recorded = meta.get("artifact_hashes")
            if recorded is None:
                if layer == "palettes":
                    # canonical set required; absence is incomplete and
                    # reconcile's completeness guard would evict it (never
                    # let reconcile rmtree what repair should quarantine).
                    items.append(_item("diverged", "palette meta.json lacks artifact_hashes"))
                    continue
                # effects/icons legacy shape: nothing recorded (3.1 annotates on read)
            elif not isinstance(recorded, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in recorded.items()
            ):
                items.append(_item("diverged", "meta.json artifact_hashes malformed"))
                continue
            else:
                listed.extend(recorded)
            if layer == "palettes":
                listed.extend(PALETTE_LINK_NAMES)  # canonical required names
            for rel in listed:
                if (
                    not rel
                    or rel in (".", "meta.json")
                    or rel.startswith("/")
                    or ".." in Path(rel).parts
                ):
                    items.append(_item("diverged", f"unsafe artifact key: {rel}"))
                    break
                # Contract keys are filenames; generators nest output (WEG), so
                # resolve by filename (with an entry-relative-path fallback).
                resolved_artifact = resolve_entry_artifact(entry_dir, rel)
                if resolved_artifact is None:
                    items.append(_item("diverged", f"artifact absent: {rel}"))
                    break
                if recorded is None or rel not in recorded:
                    if layer == "palettes" and rel in PALETTE_LINK_NAMES:
                        # canonical palette artifact absent from meta: incomplete
                        # (reconcile's completeness guard would evict it).
                        items.append(_item("diverged", f"palette artifact not recorded: {rel}"))
                        break
                    continue  # presence-only (no digest recorded for this file)
                try:
                    actual = hash_file(resolved_artifact)
                except OSError:
                    items.append(_item("diverged", f"artifact unhashable: {rel}"))
                    break
                if actual != recorded[rel]:
                    items.append(_item("diverged", f"artifact digest mismatch: {rel}"))
                    break
            else:
                items.append(_item("ok", f"{entry_dir} healthy"))
        return items

    def _check_links(self, state: DesktopState) -> list[DriftItem]:
        """Leg 2 — store vs current/ symlinks against expected cache targets."""
        current_dir = self._state_root / "current"
        expected: dict[str, Path] = {}
        wallpaper_target = (
            self._entry_dir("wallpapers", state.wallpaper.content_hash) / "wallpaper.png"
        )
        # Mirror reconcile/inspect: empty monitors fall back to DEFAULT_MONITOR
        # (production manages those links), with identical name validation.
        monitor_names = list(state.monitors) or [DEFAULT_MONITOR]
        for monitor_name in monitor_names:
            if (
                "/" in monitor_name
                or "\\" in monitor_name
                or ".." in monitor_name
                or monitor_name.strip() != monitor_name
            ):
                raise ValueError(
                    f"monitor name must not contain path separators, got {monitor_name!r}"
                )
            expected[f"wallpaper-{monitor_name}.png"] = wallpaper_target
        expected["wallpaper.png"] = wallpaper_target
        if state.palette is not None:
            palette_dir = self._entry_dir("palettes", state.palette.entry_hash)
            for artifact_name in PALETTE_LINK_NAMES:
                expected[artifact_name] = palette_dir / artifact_name
        if state.effects is not None:
            expected["effects"] = self._entry_dir("effects", state.effects.entry_hash)
        if state.icons is not None:
            expected["icons"] = self._entry_dir("icons", state.icons.entry_hash)
        items: list[DriftItem] = []
        for name, target in expected.items():
            status, detail = self._link_status(current_dir / name, target)
            items.append(DriftItem(f"current/{name}", "symlink", status, detail))
        return items

    def _link_status(self, link: Path, expected_target: Path) -> tuple[LinkStatusKind, str]:
        """Classify one symlink against its expected target (inspect-identical)."""
        if not link.is_symlink():
            return "missing", f"{link} is not a symlink"
        if not link.exists():
            return "dangling", f"{link} points nowhere"
        try:
            actual = link.resolve()
        except OSError, RuntimeError:
            return "dangling", f"{link} unresolvable"
        try:
            expected_resolved = expected_target.resolve()
        except OSError, RuntimeError:
            expected_resolved = expected_target
        if actual == expected_resolved:
            return "ok", f"{link} -> {actual}"
        return "diverged", f"{link} -> {actual} (expected {expected_resolved})"


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Outcome of one doctor repair run."""

    quarantined: tuple[Path, ...]
    repopulated: tuple[str, ...]
    history_trigger: str | None
    reload_failures: tuple[str, ...]
    history_tail_quarantined: Path | None = None


class DoctorRepairUseCase:
    """Heal three-way drift: quarantine bad entries, then reconverge.

    Separation from :class:`DoctorUseCase` is deliberate: ``check()`` stays
    read-only with a tight ``(state_repo, state_root)`` ctor, while all
    writers live here (AD-22's principle — the doctor module owns three-way
    repair — is preserved without widening the read path's surface).

    Repair reuses the Phase 2 pipeline through ``ReconcileDesktopStateUseCase``:
    quarantine first so "present-but-diverged" entry dirs register as cache
    misses, then reconcile's ``_ensure_entries`` repopulates them (same inputs
    → same hash), ``_ensure_wallpaper_entry`` re-imports the wallpaper from
    ``source_path`` when needed, ``_revert_stale_symlinks`` reverts strays,
    and the swap/save/history(``trigger=doctor``)/reload tail runs once.
    Idempotent: a clean report short-circuits before any write.
    """

    def __init__(
        self,
        doctor: DoctorUseCase,
        quarantine: Callable[[str, str], Path | None],
        reconcile: ReconcileDesktopStateUseCase,
        state_root: Path,
        heal_history_tail: Callable[[], Path | None] | None = None,
    ) -> None:
        self._doctor = doctor
        self._quarantine = quarantine
        self._reconcile = reconcile
        self._state_root = state_root
        self._heal_tail = heal_history_tail

    def repair(self) -> RepairResult:
        """Quarantine bad entries and reconverge. No-op when already clean."""
        report = self._doctor.check()
        try:
            tail = self._heal_tail() if self._heal_tail is not None else None
        except OSError as exc:
            raise RuntimeError(f"history tail heal failed: {exc}") from exc
        if report.clean and tail is None:
            return RepairResult((), (), None, ())
        quarantined: list[Path] = []
        for item in report.items:
            if item.kind != "entry" or item.status not in ("missing", "diverged"):
                continue
            if item.layer is None or item.entry_hash is None:
                continue
            moved = self._quarantine(item.layer, item.entry_hash)
            if moved is not None:
                quarantined.append(moved)
        if report.clean:
            # Only the history tail needed healing; nothing to reconverge.
            return RepairResult((), (), None, (), history_tail_quarantined=tail)
        try:
            reconciled = self._reconcile.run(trigger="doctor")
        except BaseException:
            # Never leave the machine worse than before: restore every entry
            # we moved aside (best-effort) before propagating.
            for moved in reversed(quarantined):
                layer = moved.parent.name
                entry_hash = moved.name.split("-", 1)[0]
                restore = self._state_root / "cache" / layer / entry_hash
                if restore.exists() or restore.is_symlink():
                    continue
                try:
                    os.rename(moved, restore)
                except OSError:
                    logger.exception("doctor repair: could not restore %s", moved)
            raise
        return RepairResult(
            quarantined=tuple(quarantined),
            repopulated=tuple(reconciled.cache_regenerated),
            history_trigger="doctor",
            reload_failures=tuple(reconciled.reload_failures),
            history_tail_quarantined=tail,
        )
