"""Three-way drift check (Phase 3, Story 2.1) + store/history agreement (R-5).

``DoctorUseCase.check()`` compares ``current.json`` (store) vs ``current/``
symlink targets vs ``cache/<layer>/<hash>/`` existence+health, and (R-5,
AD-41) the store vs the newest ``history.jsonl`` record, and reports
each item as ok/missing/diverged/dangling. Read-only: ``Path`` reads only
(mirror ``inspect.py``'s read-only invariant — established application-layer
pattern, no new port). Repair belongs to Story 2.2 (extends this class);
R-5 extends repair with a lightweight history-only append.

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
from typing import cast

from runtime.adapters.ags_supervision import (
    AgsSupervisionProbe,
    SupervisionAction,
    SupervisionReport,
)
from runtime.adapters.appearance import expected_mode
from runtime.adapters.appearance_ledger import AppearanceLedger
from runtime.adapters.cache import resolve_entry_artifact
from runtime.adapters.hashing import appearance_generation_hash, hash_file
from runtime.adapters.watch_health import WatchHealthStore
from runtime.application.inspect import InspectHistoryUseCase, LinkStatusKind
from runtime.application.palette_conformance import check_palette_entry
from runtime.application.reconcile import ReconcileDesktopStateUseCase
from runtime.domain.models import (
    DEFAULT_MONITOR,
    DesktopState,
    HistoryLockError,
)
from runtime.ports.host_defaults import IHostDefaults
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
    "colors.kitty",
    "colors.btop.theme",
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


def _normalized_link_target(link: Path) -> Path | None:
    """Return the link's lexical target, resolved relative to its parent.

    Deliberately does not resolve the ``current`` generation hop: a stable
    link and a generation-specific link must stay distinguishable.
    """
    try:
        raw = os.readlink(link)
    except OSError:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = link.parent / target
    return Path(os.path.normpath(os.path.abspath(target)))


def _stable_appearance_link_target(state_root: Path, config_name: str) -> Path:
    """Return the provisioning-owned stable target for one config dir."""
    target = state_root / "current" / "appearance" / f"{config_name}.css"
    return Path(os.path.normpath(os.path.abspath(target)))


def _is_stable_appearance_link(link: Path, state_root: Path) -> bool:
    """True when ``link`` names exactly the live stable appearance target.

    The link target must be ``<state_root>/current/appearance/<config>.css``
    (absolute or relative, lexically normalized) and that target must exist
    through the ``current`` generation hop. A generation path, app tag, v2
    cache target, wrong basename or dangling link is not stable.
    """
    if not link.is_symlink():
        return False
    target = _normalized_link_target(link)
    if target is None:
        return False
    expected = _stable_appearance_link_target(state_root, link.parent.name)
    return target == expected and expected.exists()


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Outcome of one three-way drift check. Read-only; mutates nothing."""

    items: tuple[DriftItem, ...]
    clean: bool
    supervision: SupervisionReport | None = None


class DoctorUseCase:
    """Three-way drift detection over store, symlinks, and cache entries."""

    def __init__(
        self,
        state_repo: IStateRepository,
        state_root: Path,
        install_spine: Path | None = None,
        supervision_probe: AgsSupervisionProbe | None = None,
        host_probe: IHostDefaults | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._state_root = state_root
        self._install_spine = install_spine
        # Optional by default so direct/unit construction stays hermetic; the
        # CLI injects the real live probe.
        self._supervision_probe = supervision_probe
        # Optional live host read for the committed host-default fingerprint.
        self._host_probe = host_probe

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
        items.extend(self._check_vocabulary(state))
        items.extend(self._check_links(state))
        items.extend(self._check_appearance(state))
        items.extend(self._check_input_provenance(state))
        items.extend(self._check_history(state))
        supervision_items, supervision = self._check_supervision(state)
        items.extend(supervision_items)
        # Watch-registration exhaustion is an environment/AD-40 report, not
        # desktop drift: it stays visible but never changes the exit verdict.
        clean = all(
            item.status == "ok" or item.name == "appearance:watch" for item in items
        )
        return DoctorReport(items=tuple(items), clean=clean, supervision=supervision)

    def _check_input_provenance(self, state: DesktopState | None = None) -> list[DriftItem]:
        """R-3/AD-43: report where each derivation input resolved from.

        ``spine`` is healthy. ``repo`` is only reachable with the explicit dev
        override set and is informational (never dirties the report). ``missing``
        is a provisioning defect surfaced loudly.
        """
        if self._install_spine is None:
            return []
        from runtime.application.derive import input_provenance

        items: list[DriftItem] = []
        for key, (path, source) in input_provenance(self._install_spine).items():
            if key == "skin_templates" and (
                state is None or state.skin is None or state.skin.name == "default"
            ):
                # The built-in default skin is internal and asset-free.
                items.append(
                    DriftItem(
                        name=f"input:{key}",
                        kind="input",
                        status="ok",
                        detail="built-in default skin (no external templates)",
                    )
                )
                continue
            if source == "spine":
                items.append(
                    DriftItem(name=f"input:{key}", kind="input", status="ok", detail=str(path))
                )
            elif source == "repo":
                items.append(
                    DriftItem(
                        name=f"input:{key}",
                        kind="input",
                        status="diverged",
                        detail=(
                            f"read from a repo checkout, not the install spine "
                            f"({path}) — DOTFILES_DEV_INPUTS_ROOT must not be set in production"
                        ),
                    )
                )
            else:
                items.append(
                    DriftItem(
                        name=f"input:{key}",
                        kind="input",
                        status="missing",
                        detail="not found in install spine (provisioning defect)",
                    )
                )
        return items

    def _check_history(self, state: DesktopState) -> list[DriftItem]:
        """R-5/AD-41: store vs history-tail agreement (read-only).

        The swap sequence persists the store and the audit trail in two
        separate steps (save, then append) with a crash window between
        them; a kill in the window leaves ``current.json`` ahead of the
        history tail. This leg closes that blind spot by comparing the
        store's content projection against the newest history record.

        Comparison scope is the four content hashes
        (wallpaper/palette/effects/icons, null-aware) only: trigger values,
        ``details``, timestamps (``applied_at``/``ts``) and ``source_path``
        churn never dirty the verdict. History robustness (corrupt middle
        line loud, torn tail tolerated, symlinked file refused) is the
        reader's — ``ValueError`` propagates, exactly like a corrupt
        ``current.json`` from ``load_current``.
        """
        records = InspectHistoryUseCase(self._state_root).run(limit=1)
        if not records:
            return [
                DriftItem(
                    name="history:tail",
                    kind="history",
                    status="diverged",
                    detail=(
                        "history.jsonl holds no records but current.json "
                        "records state (the store was never audited)"
                    ),
                )
            ]
        tail = records[0]
        store_projection = (
            state.wallpaper.content_hash,
            state.palette.entry_hash if state.palette is not None else None,
            state.effects.entry_hash if state.effects is not None else None,
            state.icons.entry_hash if state.icons is not None else None,
            state.skin.entry_hash if state.skin is not None else None,
        )
        tail_projection = (
            tail.wallpaper,
            tail.palette,
            tail.effects,
            tail.icons,
            tail.skin,
        )
        if store_projection == tail_projection:
            return [
                DriftItem(
                    name="history:tail",
                    kind="history",
                    status="ok",
                    detail=f"history tail agrees with current.json (trigger={tail.trigger!r})",
                )
            ]
        layers = ("wallpaper", "palette", "effects", "icons", "skin")
        differing = sorted(
            layer
            for layer, before, after in zip(layers, store_projection, tail_projection, strict=True)
            if before != after
        )
        return [
            DriftItem(
                name="history:tail",
                kind="history",
                status="diverged",
                detail=(
                    f"current.json disagrees with the history tail on "
                    f"{', '.join(differing)} "
                    f"(tail trigger={tail.trigger!r} ts={tail.ts!r}); "
                    f"run `dotfiles-runtime doctor --repair`"
                ),
            )
        ]

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
        if state.skin is not None:
            layers.append(("skins", state.skin.entry_hash, ["skin.json"]))
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

    def _check_vocabulary(self, state: DesktopState) -> list[DriftItem]:
        """REQ-006 leg — live palette vs the declared ``palette.v1`` roles.

        Read-only. Every palette must conform to every declared role in
        ``colors.yaml`` and ``colors.gtk.css``; missing or malformed roles are
        ``diverged`` and repair quarantines/regenerates the entry. Extra roles
        are informational and never dirty the report.
        """
        if state.palette is None:
            return []
        entry_hash = state.palette.entry_hash
        conformance = check_palette_entry(self._entry_dir("palettes", entry_hash))
        extras = ", ".join(conformance.extra_roles) or "none"
        name = f"cache/palettes/{entry_hash[:12]}:vocabulary"
        if conformance.errors():
            status: LinkStatusKind = "diverged"
            detail = "palette vocabulary v1 unreadable: " + "; ".join(conformance.errors())
        elif conformance.conforming:
            status = "ok"
            detail = (
                f"palette vocabulary v{conformance.vocabulary_version} conforming; "
                f"extra roles: {extras}"
            )
        else:
            status = "diverged"
            parts: list[str] = []
            if conformance.missing_roles:
                parts.append("missing: " + ", ".join(conformance.missing_roles))
            if conformance.malformed_roles:
                parts.append("malformed: " + ", ".join(conformance.malformed_roles))
            detail = (
                f"palette vocabulary v{conformance.vocabulary_version} nonconforming ("
                + "; ".join(parts)
                + f"); extra roles: {extras}"
            )
        return [
            DriftItem(
                name,
                "vocabulary",
                status,
                detail,
                layer="palettes",
                entry_hash=entry_hash,
            )
        ]

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

    def _check_appearance(self, state: DesktopState) -> list[DriftItem]:
        """Report the committed v3 generation and legacy-pointer leftovers.

        Read-only. ``current/manifest.json`` is authoritative; the recorded
        state is compared against the manifest intent (palette/skin/mode/
        wallpaper), never against the pointer it resolves to. A real config
        dir may carry the provisioning-owned stable ``appearance.css`` link to
        ``current/appearance/<configDir>.css``; only links that resolve
        elsewhere are retired drift.
        """
        from runtime.adapters.generation_store import GenerationStore

        if state.palette is None:
            return []
        store = GenerationStore(self._state_root)
        generation = store.current_hash()
        items: list[DriftItem] = []
        if generation is None:
            return [
                DriftItem(
                    "generation:pointer",
                    "appearance",
                    "missing",
                    "state/current does not resolve to a generation",
                )
            ]
        try:
            manifest = store.verify(generation)
        except Exception as exc:
            items.append(
                DriftItem(
                    "generation:manifest",
                    "appearance",
                    "corrupt",
                    f"current/manifest.json failed its self-check: {exc}",
                )
            )
            return items
        palette_mode = expected_mode(self._state_root, state.palette)
        checks = {
            "palette": manifest["intent"]["palette"] == state.palette.entry_hash,
            "skin": state.skin is None
            or manifest["intent"]["skin"] == state.skin.name,
            "mode": manifest["mode"] == palette_mode,
            "wallpaper": manifest["intent"]["wallpaper"]
            == state.wallpaper.content_hash,
        }
        mismatched = [name for name, ok in checks.items() if not ok]
        if mismatched:
            items.append(
                DriftItem(
                    "generation:identity",
                    "appearance",
                    "diverged",
                    (
                        f"committed generation {generation[:12]} disagrees with the "
                        f"recorded state on: {', '.join(mismatched)} "
                        f"(skin={manifest['intent']['skin']}, mode={manifest['mode']})"
                    ),
                )
            )
        else:
            items.append(
                DriftItem(
                    "generation:identity",
                    "appearance",
                    "ok",
                    (
                        f"generation {generation[:12]} "
                        f"(skin={manifest['intent']['skin']}, mode={manifest['mode']}, "
                        f"{len(manifest['appearance']['targets'])} css target(s))"
                    ),
                )
            )
        items.extend(self._check_host(manifest))
        watch = WatchHealthStore(self._state_root).read()
        items.append(
            DriftItem(
                "appearance:watch",
                "appearance",
                "ok" if watch is None or not watch.degraded else "diverged",
                (
                    "watch health unknown (daemon has not reported; current is the signal)"
                    if watch is None
                    else (
                        f"watch set degraded: {', '.join(watch.failed_roots) or 'unknown root'}"
                        if watch.degraded
                        else "watch set healthy"
                    )
                ),
            )
        )
        if self._install_spine is not None:
            config_root = self._install_spine / "config"
            candidates = (
                sorted(path for path in config_root.iterdir() if path.is_dir())
                if config_root.is_dir()
                else []
            )
            for config_dir in candidates:
                # Never traverse a link into an app tag: an undeletable tag
                # write is the v2 defect, and its presence is drift.
                if config_dir.is_symlink():
                    continue
                legacy = config_dir / "appearance.css"
                if legacy.is_symlink() and not _is_stable_appearance_link(legacy, self._state_root):
                    items.append(
                        DriftItem(
                            f"consumer/config/{config_dir.name}/appearance.css",
                            "consumer",
                            "diverged",
                            (
                                f"retired v2 convenience link: {legacy}; v3 apps read "
                                "$XDG_STATE_HOME/dotfiles/current/appearance/"
                                f"{config_dir.name}.css"
                            ),
                        )
                    )
                for name in ("skin.css", "skin.json"):
                    stale = config_dir / name
                    if stale.is_symlink():
                        items.append(
                            DriftItem(
                                f"consumer/config/{config_dir.name}/{name}",
                                "consumer",
                                "diverged",
                                f"retired v1 skin pointer: {stale}",
                            )
                        )
        return items

    def _check_host(self, manifest: dict[str, object]) -> list[DriftItem]:
        """Compare the committed host fingerprint with a fresh live read.

        Only the paths recorded in the generation's host ``defaults`` are
        compared, so a change to a non-delegated compositor setting is never
        drift. An unavailable host is reported as unknown, not as drift; the
        committed generation remains authoritative.
        """
        host = manifest.get("host")
        if not isinstance(host, dict):
            return []
        fingerprint = host.get("fingerprint")
        defaults = host.get("defaults")
        paths = list(defaults) if isinstance(defaults, dict) else []
        if self._host_probe is None:
            return [
                DriftItem(
                    "generation:host",
                    "host",
                    "ok",
                    "host probe not wired; committed host fingerprint not verified",
                )
            ]
        snapshot = self._host_probe.read()
        if snapshot is None:
            return [
                DriftItem(
                    "generation:host",
                    "host",
                    "ok",
                    "live host unavailable; committed host fingerprint unknown",
                )
            ]
        missing = [path for path in paths if path not in snapshot.values]
        if missing:
            return [
                DriftItem(
                    "generation:host",
                    "host",
                    "ok",
                    (
                        "live host unavailable for delegated token(s) "
                        f"{', '.join(sorted(missing))}; committed host fingerprint unknown"
                    ),
                )
            ]
        live = snapshot.fingerprint(paths)
        if live != fingerprint:
            return [
                DriftItem(
                    "generation:host",
                    "host",
                    "diverged",
                    (
                        f"live host fingerprint {(live or 'none')[:12]} differs from "
                        f"committed {str(fingerprint)[:12]}; delegated host values drifted"
                    ),
                )
            ]
        return [
            DriftItem(
                "generation:host",
                "host",
                "ok",
                f"host fingerprint {str(fingerprint)[:12]} matches the live delegated values",
            )
        ]

    def _check_supervision(
        self, state: DesktopState
    ) -> tuple[list[DriftItem], SupervisionReport | None]:
        """Report live systemd --user/unit/bus/orphan health (RFC §4.3).

        Optional and read-only: with no probe the leg is omitted (direct
        constructions stay hermetic). An absent systemd/user session is an
        explicit skip item, never drift. Stale appearance freshness comes from
        the ledger; absent ledger/entry is reported as unknown, never guessed.
        """
        if self._supervision_probe is None:
            return [], None
        expected_hash = self._expected_appearance_hash(state)
        applied = AppearanceLedger(self._state_root).load() if expected_hash is not None else None
        try:
            report = self._supervision_probe.inspect(
                expected_appearance_hash=expected_hash,
                applied=applied,
            )
        except OSError as exc:
            return [
                DriftItem(
                    "supervision:systemd",
                    "supervision",
                    "ok",
                    f"skipped: supervision unavailable ({exc})",
                )
            ], None
        if not report.available:
            return [
                DriftItem(
                    "supervision:systemd",
                    "supervision",
                    "ok",
                    f"skipped: {report.note}",
                )
            ], report

        items: list[DriftItem] = []
        for unit in report.units:
            items.append(
                DriftItem(
                    f"supervision:unit/{unit.app}",
                    "supervision",
                    "ok" if unit.healthy else "diverged",
                    f"{unit.unit} ({unit.instance}): {unit.detail}",
                )
            )
            if unit.stale_appearance is True:
                items.append(
                    DriftItem(
                        f"supervision:appearance/{unit.app}",
                        "supervision",
                        "diverged",
                        (
                            f"{unit.instance} last applied an older appearance hash than "
                            "the current generation; restart the unit to apply it"
                        ),
                    )
                )
        for orphan in report.orphans:
            items.append(
                DriftItem(
                    f"supervision:orphan/{orphan.pid}",
                    "supervision",
                    "diverged",
                    (
                        f"{orphan.kind} pid {orphan.pid} for configDir "
                        f"{orphan.config_dir!r} is outside {orphan.unit}; "
                        f"quit instance {orphan.instance!r} and restart the unit"
                    ),
                )
            )
        return items, report

    def _expected_appearance_hash(self, state: DesktopState) -> str | None:
        """The appearance identity the supervision leg compares against.

        v3 makes ``current/manifest.json`` authoritative (REQ-020): the
        committed ``generationHash`` is the ledger/refresh identity, exactly
        as ``inspect`` and the refresh lane already use it. The v2
        ``appearance_generation_hash(palette, skin, mode)`` recompute is only
        the pre-v3 fallback; on a v3 host it would produce a different hash
        and mark every applied app stale.

        A v3 pointer whose manifest is missing or invalid returns ``None``
        (unknown) rather than guessing a v2 identity; the appearance leg
        already reports the corrupt/missing manifest.
        """
        from runtime.adapters.generation_store import GenerationStore

        store = GenerationStore(self._state_root)
        generation = store.current_hash()
        if generation is not None:
            manifest = store.load_manifest(generation)
            if manifest is None:
                return None
            return cast("str", manifest["generationHash"])
        if (self._state_root / "current" / "manifest.json").is_file():
            # A manifest under current/ without a valid generation pointer is
            # still a v3 artifact, never a pre-v3 host to guess for.
            return None
        if state.palette is None or state.skin is None:
            return None
        palette_mode = expected_mode(self._state_root, state.palette)
        return appearance_generation_hash(
            state.palette.entry_hash, state.skin.entry_hash, palette_mode
        )

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
    supervision_actions: tuple[SupervisionAction, ...] = ()


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

    R-5 adds a lightweight path: a *pure* store↔history divergence (entries
    and symlinks healthy — an audit-only gap from the save↔append crash
    window) appends exactly one ``trigger="doctor"`` line for the
    authoritative store, with no repopulate/repoint/reload. The store wins;
    history is repaired, never the reverse.
    """

    def __init__(
        self,
        doctor: DoctorUseCase,
        quarantine: Callable[[str, str], Path | None],
        reconcile: ReconcileDesktopStateUseCase,
        state_root: Path,
        heal_history_tail: Callable[[], Path | None] | None = None,
        append_history: Callable[..., None] | None = None,
        state_repo: IStateRepository | None = None,
        supervision_repair: Callable[[SupervisionReport], tuple[SupervisionAction, ...]]
        | None = None,
    ) -> None:
        self._doctor = doctor
        self._quarantine = quarantine
        self._reconcile = reconcile
        self._state_root = state_root
        self._heal_tail = heal_history_tail
        # R-5 lightweight-path seams (both optional for backward
        # compatibility): when either is absent a pure history divergence
        # falls back to the full reconverge below, which appends the same
        # ``doctor`` line as part of its tail.
        self._append_history = append_history
        self._state_repo = state_repo
        # Optional live-supervision repair (RFC §4.3): only the safe
        # systemctl restart / ags quit actions are allowed.
        self._repair_supervision = supervision_repair

    def repair(self) -> RepairResult:
        """Quarantine bad entries and reconverge. No-op when already clean."""
        report = self._doctor.check()
        try:
            tail = self._heal_tail() if self._heal_tail is not None else None
        except (OSError, HistoryLockError) as exc:
            raise RuntimeError(f"history tail heal failed: {exc}") from exc
        history_dirty = any(item.kind == "history" and item.status != "ok" for item in report.items)
        supervision_dirty = any(
            item.kind == "supervision" and item.status != "ok" for item in report.items
        )
        other_dirty = any(
            item.kind not in ("history", "supervision") and item.status != "ok"
            for item in report.items
        )
        supervision_actions: tuple[SupervisionAction, ...] = ()
        if supervision_dirty and report.supervision is not None and self._repair_supervision:
            supervision_actions = self._repair_supervision(report.supervision)
        if report.clean and tail is None:
            return RepairResult((), (), None, ())
        if history_dirty and not other_dirty:
            healed = self._repair_history_only(tail, supervision_actions)
            if healed is not None:
                return healed
            # Seams absent (backward-compatible ctor): fall through to the
            # full reconverge, whose tail appends the same doctor line.
        if supervision_dirty and not other_dirty and not history_dirty:
            # Pure supervision drift: only the safe restart/quit actions run.
            return RepairResult(
                (),
                (),
                None,
                (),
                history_tail_quarantined=tail,
                supervision_actions=supervision_actions,
            )
        quarantined: list[Path] = []
        for item in report.items:
            if item.kind not in ("entry", "vocabulary") or item.status not in (
                "missing",
                "diverged",
            ):
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
            supervision_actions=supervision_actions,
        )

    def _repair_history_only(
        self,
        tail: Path | None,
        supervision_actions: tuple[SupervisionAction, ...] = (),
    ) -> RepairResult | None:
        """Append one ``doctor`` line for the authoritative store (R-5).

        Pure store↔history divergence only: entries and symlinks are
        healthy, so there is nothing to repopulate, repoint, or reload —
        the audit trail just missed a save (the save↔append crash window).

        The ``check()`` verdict is instantly stale, so the tail is
        re-read here: a concurrent writer may have converged store+history
        in between, in which case there is nothing to append (idempotent
        no-op result instead of a duplicate ``doctor`` line).

        Returns the result, or ``None`` when the lightweight seams were
        not injected (caller falls back to the full reconverge). Never
        holds the history lock itself — ``append_history`` locks
        internally (flock is non-reentrant).
        """
        if self._append_history is None or self._state_repo is None:
            return None
        state = self._state_repo.load_current()
        if state is None:
            raise RuntimeError(
                "doctor repair found store/history divergence but current.json "
                "is now absent; re-run the command"
            )
        if self._history_agrees(state):
            return RepairResult(
                quarantined=(),
                repopulated=(),
                history_trigger=None,
                reload_failures=(),
                history_tail_quarantined=tail,
                supervision_actions=supervision_actions,
            )
        try:
            self._append_history(
                trigger="doctor",
                wallpaper_hash=state.wallpaper.content_hash,
                palette_hash=state.palette.entry_hash if state.palette is not None else None,
                effects_hash=state.effects.entry_hash if state.effects is not None else None,
                icons_hash=state.icons.entry_hash if state.icons is not None else None,
                skin_hash=state.skin.entry_hash if state.skin is not None else None,
                source_path=state.wallpaper.source_path,
            )
        except (OSError, HistoryLockError) as exc:
            raise RuntimeError(f"history append failed: {exc}") from exc
        return RepairResult(
            quarantined=(),
            repopulated=(),
            history_trigger="doctor",
            reload_failures=(),
            history_tail_quarantined=tail,
            supervision_actions=supervision_actions,
        )

    def _history_agrees(self, state: DesktopState) -> bool:
        """Best-effort tail re-read: True when the newest record matches the store."""
        records = InspectHistoryUseCase(self._state_root).run(limit=1)
        if not records:
            return False
        tail_record = records[0]
        return (
            tail_record.wallpaper == state.wallpaper.content_hash
            and tail_record.palette
            == (state.palette.entry_hash if state.palette is not None else None)
            and tail_record.effects
            == (state.effects.entry_hash if state.effects is not None else None)
            and tail_record.icons == (state.icons.entry_hash if state.icons is not None else None)
            and tail_record.skin == (state.skin.entry_hash if state.skin is not None else None)
        )
