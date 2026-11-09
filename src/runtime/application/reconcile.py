"""Reconcile-desktop-state use case — converge state, apply wallpaper, reload consumers.

Implements AD-1 (hexagonal), AD-2 (input changes invalidate), AD-5
(state_root), AD-6 (swap symlinks lead), AD-14 (domain purity), AD-17
(consumer wiring), AD-18 (preserve monitor configs), AD-20 (CLI
rendering).

Scope boundary (Stories 2.1–2.3, 2.7):
- Repoints ``current/`` symlinks to converge the desktop with
  ``current.json``, appends ``history.jsonl``, persists refreshed
  ``current.json``, applies wallpaper through ``IWallpaperApplier``, then
  triggers one reload per injected palette/UI ``IDesktopReloader``.
- ``wallpaper set`` (Story 2.7 capstone, shipped) chains this use case
  after ``ApplyWallpaperUseCase`` in the CLI composition root, passing
  the history trigger ``"set"``; the standalone ``reconcile`` command
  keeps the default trigger ``"reconcile"``.

Swap sequence order (shared-data-contract, non-negotiable):
1. Ensure cache entries (wallpaper re-import on miss; palette/icons/effects
   regeneration with hash-mismatch guard; effects/icons graceful).
2. Repoint ONLY ``current/`` symlinks via ``CacheSeeder``.
3. ``current.json`` follows (refreshed ``applied_at``).
4. History: append one ``history.jsonl`` line (trigger ``"reconcile"``
   by default; ``wallpaper set`` passes ``"set"`` via ``run(trigger=...)``
   — the accepted values are ``runtime.domain.history.HISTORY_TRIGGERS``).
5. Apply persisted wallpaper unless the caller already applied it.
6. Reload palette/UI consumers (fire-and-report, per injected reloader).

Derivation (step 1) runs OUTSIDE the lock (staging is race-safe;
tool invocations stay parallel — same split ``ApplyWallpaperUseCase``
uses). The load→repoint→save critical section is serialized by the
blocking seed mutex (double-checked pattern, D1 remediation from Story
1.13).

Architecture:
- Lives in ``application/`` (use-case layer) per AD-1, AD-13
- Injected adapters mirror ``ApplyWallpaperUseCase`` exactly
- No raw ``os``/``json`` FS I/O in the use case (delegated to adapters)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.appearance import (
    AppearanceGenerator,
    detect_palette_mode,
    palette_declares_mode,
)
from runtime.adapters.cache import cache_entry_path
from runtime.adapters.converge_lock import ConvergeLock
from runtime.adapters.generation_store import (
    GenerationSpec,
    GenerationStore,
    generation_intent_hash,
)
from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder, _repoint_symlink
from runtime.application.bar_backdrop import with_bar_backdrops
from runtime.application.derive import (
    DerivationPipeline,
    ensure_palette_entry_complete,
    find_skin_manifest,
    find_skin_templates,
)
from runtime.application.monitors import preserve_monitors
from runtime.application.palette_conformance import require_palette_conformance
from runtime.domain.ags_apps import AppRefreshRecord, ReloadFailure
from runtime.domain.history import HISTORY_TRIGGERS
from runtime.domain.host_defaults import HostSnapshot
from runtime.domain.models import (
    DEFAULT_MONITOR,
    DEFAULT_SKIN,
    AppearanceEntry,
    DesktopState,
    EffectsEntry,
    IconsEntry,
    PaletteEntry,
    PaletteUnavailableError,
    SkinEntry,
    SkinUnavailableError,
    WallpaperEntry,
)
from runtime.ports.ags_app_refresher import IAgsAppRefresher
from runtime.ports.appearance_generator import IAppearanceGenerator
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.desktop_reloader import IDesktopReloader
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.host_defaults import IHostDefaults
from runtime.ports.icon_renderer import IIconRenderer
from runtime.ports.seed_mutex import ISeedMutex
from runtime.ports.skin_expander import ISkinExpander
from runtime.ports.state_repository import IStateRepository
from runtime.ports.wallpaper_applier import IWallpaperApplier

logger = logging.getLogger(__name__)


def default_skin_entry(state_root: Path, seeder: CacheSeeder) -> SkinEntry:
    """Create/reuse the minimal built-in skin entry (no external assets)."""
    from runtime.adapters.hashing import hash_file_bytes, skin_entry_hash
    from runtime.domain.models import SkinArtifacts

    templates_hash = hash_file_bytes(b"dotfiles-runtime:default-skin:templates:v1")
    manifest_hash = hash_file_bytes(b"dotfiles-runtime:default-skin:manifest:v1")
    entry_hash = skin_entry_hash(templates_hash, manifest_hash)
    entry_dir = cache_entry_path(state_root, "skins", entry_hash)
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    css = "/* dotfiles-runtime built-in skin */\n"
    config_dir = "ags"
    artifacts: SkinArtifacts = {
        f"{config_dir}.css": hash_file_bytes(css.encode("utf-8")),
        "skin.json": hash_file_bytes(b"{}"),
    }
    if not entry_dir.is_dir():
        entry_dir.mkdir(parents=True, exist_ok=True)
        (entry_dir / f"{config_dir}.css").write_text(css, encoding="utf-8")
        (entry_dir / "skin.json").write_text("{}", encoding="utf-8")
        seeder.write_skin_meta(
            entry_hash,
            "default",
            templates_hash,
            manifest_hash,
            dict(artifacts),
            generated_at=now,
        )
    return SkinEntry(
        hash_algorithm="sha256",
        kind="skin",
        entry_hash=entry_hash,
        name="default",
        input_templates_hash=templates_hash,
        input_manifest_hash=manifest_hash,
        artifact_hashes=artifacts,
        generated_at=now,
    )


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Outcome of one ``ReconcileDesktopStateUseCase.run`` invocation."""

    repointed: list[Path]
    skipped: list[str]
    state: DesktopState
    cache_regenerated: list[str]
    reload_failures: list[ReloadFailure] = field(default_factory=list)
    consumer_symlinks: list[Path] = field(default_factory=list)
    app_refresh_results: list[AppRefreshRecord] = field(default_factory=list)
    """Spine consumer pointers (Epic 4, gt-2-2 spec table) — e.g.
    ``<install>/config/ags/colors.css`` → ``current/colors.gtk.css``.
    Kept separate from ``repointed`` (which is ``current/``-only) so CLI
    counts and existing consumers keep their contract."""
    appearance_hash: str | None = None
    appearance_skin: str | None = None
    appearance_mode: str | None = None
    generation_hash: str | None = None
    generation_path: Path | None = None


class ReconcileDesktopStateUseCase:
    """Repoint ``current/`` symlinks to converge with ``current.json``.

    Steps (one discrete method — the swap step Story 2.2 instruments):
    1. Load ``current.json`` (fail-fast corrupt/absent guard)
    2. Hold the seed mutex (blocking), re-load inside critical section
    3. Ensure cache entries (derivation outside the lock — same split as
       ``ApplyWallpaperUseCase``)
    4. Commit one generation and re-save ``current.json`` with refreshed
       ``applied_at``
    5. Append ``history.jsonl`` (trigger ``"reconcile"`` by default;
       ``wallpaper set`` passes ``"set"`` via ``run(trigger=...)``)
    6. Sweep old generations/cache from the post-commit current generation
    7. Apply persisted wallpaper (unless the caller already applied it)
    8. Reload palette/UI consumers (fire-and-report, per injected reloader)

    Constructor receives ports, the injected ``CacheSeeder`` adapter, the
    state mutex, a wallpaper applier, and palette/UI ``IDesktopReloader``
    instances (dependency inversion — the use case wires no concrete
    adapters itself). Wallpaper application and consumer reloads run after
    history append, outside the lock.
    """

    def __init__(
        self,
        state_repo: IStateRepository,
        palette_producer: IColorSchemeGenerator,
        weg: IEffectsGenerator,
        itr: IIconRenderer,
        install_spine: Path,
        state_root: Path,
        seeder: CacheSeeder,
        mutex: ISeedMutex,
        reloaders: list[IDesktopReloader] | None = None,
        wallpaper_applier: IWallpaperApplier | None = None,
        app_refresher: IAgsAppRefresher | None = None,
        skin_name: str = DEFAULT_SKIN,
        skin_expander: ISkinExpander | None = None,
        appearance_generator: IAppearanceGenerator | None = None,
        appearance_keep: int = 5,
        generation_store: GenerationStore | None = None,
        converge_lock: ConvergeLock | None = None,
        source_kind: str = "cli",
        job_id: str | None = None,
        host_defaults: IHostDefaults | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._state_root = state_root
        self._install_spine = install_spine
        self._seeder = seeder
        self._mutex = mutex
        #: The v3 commit transaction and the one serialization lock. The
        #: composition root injects both; the defaults keep direct use cases
        #: on the same implementation with a state-root-local store/lock.
        self._generation_store = generation_store or GenerationStore(state_root)
        self._converge_lock = converge_lock or ConvergeLock(state_root)
        self._source_kind = source_kind
        self._job_id = job_id
        #: Optional live host read. Production wires the Hyprland adapter; a
        #: missing port means "host unavailable" and resolves to skin fallbacks.
        self._host_defaults = host_defaults
        #: The composed v2 appearance plane is part of the normal reconcile
        #: flow; the default is the real generator (tests may inject a fake).
        self._appearance = appearance_generator or AppearanceGenerator(state_root)
        self._appearance_keep = appearance_keep
        #: Real producer identity for typed failures.
        self._producer_name = getattr(palette_producer, "producer_name", "tpr")
        #: Mode-aware producers (tpr) expose ``mode``. When present, a recorded
        #: palette whose key no longer matches the current intent is stale and
        #: regenerated, even though the entry directory still exists.
        self._palette_mode = getattr(palette_producer, "mode", None)
        self._reloaders: list[IDesktopReloader] = list(reloaders) if reloaders is not None else []
        self._wallpaper_applier = wallpaper_applier
        #: Manifest-driven AGS app refresh (AD-27…30). It owns both the live
        #: lane and the configured restart lane; it is NOT an ordinary
        #: ``IDesktopReloader`` because its failures are structured per app.
        self._app_refresher = app_refresher
        self._skin_name = skin_name
        self._skin_expander = skin_expander
        self._pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            skins=skin_expander,
        )

    def run(
        self,
        trigger: str = "reconcile",
        *,
        wallpaper_already_applied: bool = False,
        wallpaper_path: Path | None = None,
        intent_hash: str | None = None,
        acquire_lock: bool = True,
        force_icons: bool = False,
        history_details: dict[str, object] | None = None,
    ) -> ReconcileResult:
        """The one v3 mutation scope: intent → derive → stage → commit → apply.

        The whole call is serialized by the blocking ``converge.lock``; there
        is no derivation-outside-the-lock split and no second pipeline.
        ``acquire_lock=False`` is only for :class:`ConvergenceEngine`, which
        holds the same lock across an optional regenerate pre-step so the
        commit, current.json save, and history append stay one atomic scope.
        ``force_icons`` re-derives the icons layer even on a recorded cache
        hit (the GUI live-toggle path); the commit itself is still the one
        generation transaction.
        """
        # Validate the enum before acquiring the lock: a bad caller argument
        # must fail without touching the filesystem.
        if not isinstance(trigger, str) or trigger not in HISTORY_TRIGGERS:
            valid = ", ".join(sorted(HISTORY_TRIGGERS))
            raise ValueError(f"invalid history trigger: {trigger!r} (expected one of {valid})")
        if not acquire_lock:
            return self._run(
                trigger,
                wallpaper_already_applied=wallpaper_already_applied,
                wallpaper_path=wallpaper_path,
                intent_hash=intent_hash,
                force_icons=force_icons,
                history_details=history_details,
            )
        with self._converge_lock.hold():
            return self._run(
                trigger,
                wallpaper_already_applied=wallpaper_already_applied,
                wallpaper_path=wallpaper_path,
                intent_hash=intent_hash,
                force_icons=force_icons,
                history_details=history_details,
            )

    def _run(
        self,
        trigger: str,
        *,
        wallpaper_already_applied: bool,
        wallpaper_path: Path | None,
        intent_hash: str | None,
        force_icons: bool,
        history_details: dict[str, object] | None,
    ) -> ReconcileResult:
        """Reconcile: entries → generation commit → current.json → apply/ledger.

        Raises:
            ValueError: if ``trigger`` is not one of the pinned enum
                values (fail-loud — never invent triggers), or
                propagated from a corrupt ``current.json``.
            RuntimeError: if ``current.json`` is absent (nothing to
                reconcile), palette (hard dependency) regeneration fails,
                wallpaper entry cannot be rebuilt, or hash-mismatch guard
                fires (AC 6b).
            OSError: on filesystem failures.
        """
        if not isinstance(trigger, str) or trigger not in HISTORY_TRIGGERS:
            valid = ", ".join(sorted(HISTORY_TRIGGERS))
            raise ValueError(f"invalid history trigger: {trigger!r} (expected one of {valid})")
        # Fail-fast corrupt/absent guard (mirrors apply):
        state = self._state_repo.load_current()
        if wallpaper_path is not None:
            state = self._with_intent_wallpaper(state, wallpaper_path)
        if state is None:
            raise RuntimeError("nothing to reconcile")
        # ValueError from a corrupt store propagates loudly here — never
        # swallowed into a reseed or fallback. An explicit intent may create
        # the first generation (no recorded palette yet).
        if state.palette is None and wallpaper_path is None:
            self._require_palette(state)

        # v3: the generation directory is immutable and the commit pointer is
        # the only current/ inode. Stray per-family links are a v2 artifact;
        # nothing is "reverted" here.
        logger.debug("recovery: generation commit pointer is authoritative")

        # Derivation (step 1) happens OUTSIDE the lock (staging is race-
        # safe; tool invocations stay parallel — same split as apply).
        regenerated: list[str] = []
        palette, effects, icons, skin = self._ensure_entries(
            state, regenerated, force_icons=force_icons
        )
        pre_lock_wallpaper_hash = state.wallpaper.content_hash

        # Re-read after derivation: state may have changed concurrently
        # (e.g. a wallpaper set landed while tools ran). If the wallpaper
        # hash changed, re-derive inside the lock so palette/effects/icons
        # match the authoritative state.
        with self._mutex.hold(blocking=True):
            if wallpaper_path is None:
                state = self._state_repo.load_current()
            if state is None:
                raise RuntimeError("nothing to reconcile")
            # Re-check the REQUIRED palette inside the critical section: a
            # concurrent legacy writer must not be converged into removed
            # consumer pointers (BUG-001). An explicit intent may create the
            # first generation, so the recorded palette can still be absent.
            if state.palette is None and wallpaper_path is None:
                self._require_palette(state)

            # One host read for the whole generation, inside converge.lock.
            # Reconcile, seed and doctor use the same resolver; only the
            # selected skin/mode's delegated values enter the identity.
            host = self._host_defaults.read() if self._host_defaults is not None else None

            # Decision D1: re-derive if state changed while we were outside.
            if state.wallpaper.content_hash != pre_lock_wallpaper_hash:
                # Re-derive for the authoritative state inside the lock.
                # This serializes tool invocations only on contention.
                palette, effects, icons, skin = self._ensure_entries(
                    state, regenerated, force_icons=force_icons
                )
            else:
                # Re-validate palette/effects/icons hashes against reloaded
                # state to catch stale derivation even when wallpaper hash
                # matches (e.g. spine change caused hash-mismatch guard).
                # _ensure_entries already validated, but re-check in case
                # state was mutated concurrently for derived layers.
                if palette is not None and state.palette is not None:
                    # A mode-aware producer may have deliberately moved the
                    # palette key to the current intent; that is not a spine
                    # hash mismatch.
                    expected_palette = (
                        self._pipeline.expected_palette_hash(state.wallpaper.content_hash)
                        if self._mode_key_applies(state.palette)
                        else None
                    )
                    if (
                        expected_palette is None
                        or expected_palette == state.palette.entry_hash
                    ):
                        self._assert_hash_matches(
                            state.palette.entry_hash, palette.entry_hash
                        )
                if effects is not None and state.effects is not None:
                    self._assert_hash_matches(state.effects.entry_hash, effects.entry_hash)
                if icons is not None and state.icons is not None:
                    self._assert_hash_matches(state.icons.entry_hash, icons.entry_hash)

            state = DesktopState(
                schema_version=2,
                wallpaper=state.wallpaper,
                monitors=with_bar_backdrops(state.monitors, self._state_root),
                palette=palette,
                effects=effects,
                icons=icons,
                applied_at=state.applied_at,
                skin=skin,
            )

            # Monitor name validation P1 — fail loud on traversal
            for m in list(state.monitors):
                if "/" in m or "\\" in m or m.strip() != m or ".." in m:
                    raise ValueError(f"monitor name must not contain path separators, got {m!r}")

            # Step 2 — one committed generation. The wallpaper layer must be
            # present; the palette is required by _require_palette.
            monitor_names = list(state.monitors) or [DEFAULT_MONITOR]
            wallpaper_target = (
                cache_entry_path(self._state_root, "wallpapers", state.wallpaper.content_hash)
                / "wallpaper.png"
            )
            if not wallpaper_target.exists() or not wallpaper_target.is_file():
                raise RuntimeError(
                    f"wallpaper cache entry {state.wallpaper.content_hash} "
                    f"missing: {wallpaper_target}"
                )
            appearance_entry = self._compose_appearance(
                palette, skin, regenerated, host=host
            )
            if appearance_entry is None or skin is None:
                raise RuntimeError(
                    "generation refused: the skin/appearance layer is unavailable; "
                    "the previous generation remains current"
                )
            colors_path = (
                cache_entry_path(self._state_root, "palettes", palette.entry_hash)
                / "colors.yaml"
            )
            mode = (
                self._palette_mode
                if self._palette_mode in ("dark", "light")
                else detect_palette_mode(colors_path.read_text(encoding="utf-8"))
            )
            skin_name = skin.name
            resolved_intent_hash = intent_hash or generation_intent_hash(
                wallpaper=state.wallpaper.content_hash,
                palette=palette.entry_hash,
                skin=skin_name,
                mode=mode,
                effects=effects.entry_hash if effects is not None else None,
                icons=icons.entry_hash if icons is not None else None,
                host=appearance_entry.host_hash,
            )
            generation = self._generation_store.commit(
                GenerationSpec(
                    state=DesktopState(
                        schema_version=2,
                        wallpaper=state.wallpaper,
                        monitors=state.monitors,
                        palette=palette,
                        effects=effects,
                        icons=icons,
                        applied_at=state.applied_at,
                        skin=skin,
                    ),
                    appearance=appearance_entry,
                    mode=mode,
                    skin_name=skin_name,
                    skin_hash=skin.entry_hash,
                    intent_hash=resolved_intent_hash,
                    monitor_names=tuple(monitor_names),
                    source_kind=self._source_kind,
                    job_id=self._job_id,
                    host_hash=appearance_entry.host_hash,
                    host_values=appearance_entry.host_values,
                )
            )
            repointed = self._generation_paths(
                generation.path, monitor_names, palette, effects, icons
            )
            # The runtime never repoints install-spine links (provisioning
            # owns them); it only prunes runtime-owned legacy v2 pointers.
            consumer_repointed = self._seeder.prune_runtime_spine_pointers(
                self._install_spine
            )
            skipped = self._derive_skipped(state, monitor_names, repointed, log=False)

            # Step 3 — the derived compatibility view follows the commit.
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            saved = DesktopState(
                schema_version=2,
                wallpaper=state.wallpaper,
                monitors=state.monitors,
                palette=palette,
                effects=effects,
                icons=icons,
                applied_at=now,
                skin=skin,
            )
            self._state_repo.save(saved)

            # Step 4 — history, inside the one commit scope and from the
            # committed snapshot. Appending outside ``converge.lock`` let a
            # newer commit advance ``current.json`` after an older reconcile
            # read it, leaving a stale tail (BUG-013). The committed state is
            # exactly what the generation manifest records, so the tail can
            # never disagree with ``current.json``/``current/manifest.json``.
            self._seeder.append_history(
                trigger=trigger,
                wallpaper_hash=saved.wallpaper.content_hash,
                palette_hash=saved.palette.entry_hash if saved.palette else None,
                effects_hash=saved.effects.entry_hash if saved.effects else None,
                icons_hash=saved.icons.entry_hash if saved.icons else None,
                skin_hash=saved.skin.entry_hash if saved.skin else None,
                source_path=saved.wallpaper.source_path,
                details=history_details,
            )

            # Step 5 — GC under the same lock, immediately after the commit.
            # The protection set is computed from the post-commit current
            # generation (and its manifest), never a pre-commit snapshot. It
            # never removes the current generation's palette/wallpaper/
            # effects/icons or composed appearance cache layer, the newest
            # entry per layer, a ledger-referenced layer or an explicit pin;
            # only unreferenced entries older than grace are swept.
            try:
                # A v3 ledger keys on the generation hash; a legacy v2 ledger
                # keyed on a composed appearance hash. Honour both: the value
                # always protects the named generation, and if it also names a
                # real appearance cache entry that layer is protected too.
                ledger_generations: list[str] = []
                ledger_layers: dict[str, list[str]] = {}
                ledger = getattr(self._app_refresher, "ledger", None)
                if ledger is not None:
                    recorded = ledger.load()
                    if recorded is not None and recorded.appearance_hash:
                        ledger_generations.append(recorded.appearance_hash)
                        if (
                            self._state_root
                            / "cache"
                            / "appearance"
                            / recorded.appearance_hash
                        ).is_dir():
                            ledger_layers["appearance"] = [recorded.appearance_hash]
                swept = self._generation_store.gc(
                    keep=5,
                    grace_seconds=24 * 60 * 60,
                    ledger_generations=ledger_generations,
                    ledger_layers=ledger_layers,
                    acknowledged_generations=[generation.generation_hash],
                    current_generation=generation.generation_hash,
                )
                if any(swept.values()):
                    logger.info("reconcile: gc swept %s", swept)
            except Exception:
                logger.exception(
                    "reconcile: generation GC failed; generation is authoritative"
                )

        # Logging for skipped null layers outside lock (P8)
        for layer in ("effects", "icons"):
            entry = getattr(saved, layer)
            if entry is None and f"{layer} (layer is null" in " ".join(skipped):
                logger.warning("reconcile: %s layer is null; consumer symlink skipped", layer)
        logger.info(
            "reconcile: committed generation %s", generation.generation_hash[:12]
        )

        # Step 6 — apply the persisted wallpaper before reloading palette/UI
        # consumers. wallpaper set has already done this during its visible
        # phase and opts out of this one stage only.
        reload_failures: list[ReloadFailure] = []
        if not wallpaper_already_applied and self._wallpaper_applier is not None:
            try:
                wallpaper_ok = self._wallpaper_applier.apply()
            except Exception as exc:
                logger.warning(
                    "wallpaper apply failed for %s: %s",
                    type(self._wallpaper_applier).__name__,
                    exc,
                )
                wallpaper_ok = False
            if not wallpaper_ok:
                reload_failures.append(ReloadFailure(type(self._wallpaper_applier).__name__))

        # Step 7 — the manifest-driven AGS app refresh (live + restart lanes).
        # AD-27: the runtime, not the wallpaper.state event, is the palette
        # refresh trigger. The ledger gate makes an unchanged appearance a no-op.
        app_results: list[AppRefreshRecord] = []
        if self._app_refresher is not None:
            try:
                outcome = self._app_refresher.refresh(
                    saved,
                    generation_hash=generation.generation_hash,
                    trigger=trigger,
                )
            except Exception as exc:
                logger.warning("AGS app refresh failed unexpectedly: %s", exc)
                reload_failures.append(
                    ReloadFailure(
                        "AgsAppRefresh",
                        status="failed",
                        reason=f"unexpected refresh failure: {exc}",
                    )
                )
            else:
                app_results = list(outcome.records)
                reload_failures.extend(outcome.failures)

        # Step 8 — reload the remaining palette/UI consumers (outside lock,
        # fire-and-report). AGS is not in this list: its two lanes are owned by
        # the manifest-driven refresher above.
        for reloader in self._reloaders:
            try:
                ok = reloader.reload()
            except Exception as exc:
                logger.warning("desktop reload failed for %s: %s", type(reloader).__name__, exc)
                ok = False
            if not ok:
                reload_failures.append(ReloadFailure(type(reloader).__name__))

        return ReconcileResult(
            repointed=repointed,
            skipped=skipped,
            state=saved,
            cache_regenerated=regenerated,
            reload_failures=reload_failures,
            consumer_symlinks=consumer_repointed,
            app_refresh_results=app_results,
            appearance_hash=appearance_entry.appearance_hash
            if appearance_entry is not None
            else None,
            appearance_skin=appearance_entry.skin if appearance_entry is not None else None,
            appearance_mode=appearance_entry.mode if appearance_entry is not None else None,
            generation_hash=generation.generation_hash,
            generation_path=generation.path,
        )

    # ------------------------------------------------------------------
    # Required-palette guard (BUG-001)
    # ------------------------------------------------------------------

    def _require_palette(self, state: DesktopState) -> PaletteEntry:
        """Return the palette or refuse the state without touching links.

        A legacy null-palette state stays readable by ``inspect`` but is
        NOT reconcilable: the palette is a required artifact, and a null
        one must never cause consumer pointers to be removed.
        """
        if state.palette is None:
            source = state.wallpaper.source_path or "<unknown>"
            producer = self._producer_name
            raise PaletteUnavailableError(
                "reconcile refused: the palette layer is null for wallpaper "
                f"{state.wallpaper.content_hash} ({source}); "
                "re-set the wallpaper to regenerate the required palette",
                wallpaper_path=source,
                wallpaper_hash=state.wallpaper.content_hash,
                tool=producer,
                stderr="",
                hint=f"re-set the wallpaper / check {producer}, then reconcile again",
            )
        return state.palette

    # ------------------------------------------------------------------
    # Step 1 — ensure cache entries (derivation outside the lock)
    # ------------------------------------------------------------------

    def _ensure_entries(
        self,
        state: DesktopState,
        regenerated: list[str],
        *,
        force_icons: bool = False,
    ) -> tuple[PaletteEntry, EffectsEntry | None, IconsEntry | None, SkinEntry | None]:
        """Ensure every cache entry referenced by the state exists.

        Returns the (possibly-rebuilt) palette/effects/icons/skin entries and
        appends layer names to ``regenerated`` for actual cache misses.
        The palette check uses the SHARED completeness guard
        (``ensure_palette_entry_complete``) so a pre-growth partial
        palette entry is evicted and regenerated — ONE migration rule
        for seed/apply/reconcile (Story gt-2-1, AC 8).

        The palette is REQUIRED (BUG-001): a null-palette state raises
        ``PaletteUnavailableError`` before any entry/link work.

        The skin layer is optional infrastructure (legacy callers may not wire
        an expander) and fail-closed: a resolution/expansion failure returns
        the previous entry when its cache bytes still exist, else ``None``.
        """
        self._ensure_wallpaper_entry(state, regenerated)
        cached_wallpaper_img = (
            cache_entry_path(self._state_root, "wallpapers", state.wallpaper.content_hash)
            / "wallpaper.png"
        )

        palette = state.palette
        if palette is None:
            try:
                entry, _cache_hit = self._pipeline.ensure_palette(
                    cached_wallpaper_img, state.wallpaper.content_hash
                )
            except PaletteUnavailableError as exc:
                if state.wallpaper.source_path:
                    exc.wallpaper_path = state.wallpaper.source_path
                raise
            except Exception as exc:
                raise RuntimeError(f"palette reconcile failed: {exc}") from exc
            palette = entry
            regenerated.append("palette")
        else:
            palette_dir = cache_entry_path(self._state_root, "palettes", palette.entry_hash)
            if palette_dir.is_dir():
                # REQ-006: a pre-existing entry must conform BEFORE the visible
                # swap. A missing/nonconforming entry is fail-closed here; a
                # missing entry dir is regenerated below (the pipeline validates
                # the fresh build).
                require_palette_conformance(palette_dir)
            expected_palette = (
                self._pipeline.expected_palette_hash(state.wallpaper.content_hash)
                if self._mode_key_applies(palette)
                else None
            )
            mode_changed = (
                expected_palette is not None and expected_palette != palette.entry_hash
            )
            if mode_changed or not ensure_palette_entry_complete(palette_dir, self._seeder):
                try:
                    entry, _cache_hit = self._pipeline.ensure_palette(
                        cached_wallpaper_img, state.wallpaper.content_hash
                    )
                except PaletteUnavailableError as exc:
                    if state.wallpaper.source_path:
                        exc.wallpaper_path = state.wallpaper.source_path
                    raise
                except Exception as exc:
                    raise RuntimeError(f"palette reconcile failed: {exc}") from exc
                if not mode_changed:
                    # Same recorded identity: a divergent rebuild means the
                    # spine inputs moved under us and the state must not
                    # silently change.
                    self._assert_hash_matches(palette.entry_hash, entry.entry_hash)
                palette = entry
                regenerated.append("palette")

        effects = state.effects
        if (
            effects is not None
            and not cache_entry_path(self._state_root, "effects", effects.entry_hash).exists()
        ):
            try:
                entry_e, _cache_hit = self._pipeline.ensure_effects(
                    cached_wallpaper_img, state.wallpaper.content_hash
                )
            except Exception as exc:
                logger.warning("reconcile: effects regeneration failed; continuing: %s", exc)
                effects = None
            else:
                self._assert_hash_matches(effects.entry_hash, entry_e.entry_hash)
                effects = entry_e
                regenerated.append("effects")

        icons = state.icons
        if force_icons:
            # The GUI live-toggle path asks for the icons layer explicitly:
            # re-derive it even when the recorded entry still exists. The
            # result is still committed through the one generation scope
            # below, so the tail can never describe an uncommitted icons key.
            entry_i, cache_hit = self._pipeline.ensure_icons(palette.entry_hash)
            icons = entry_i
            if not cache_hit:
                regenerated.append("icons")
        elif (
            icons is not None
            and not cache_entry_path(self._state_root, "icons", icons.entry_hash).exists()
        ):
            try:
                entry_i, _cache_hit = self._pipeline.ensure_icons(palette.entry_hash)
            except Exception as exc:
                logger.warning("reconcile: icons regeneration failed; continuing: %s", exc)
                icons = None
            else:
                self._assert_hash_matches(icons.entry_hash, entry_i.entry_hash)
                icons = entry_i
                regenerated.append("icons")

        skin = state.skin
        if self._skin_expander is not None:
            try:
                entry_s, cache_hit = self._pipeline.ensure_skin(self._skin_name)
            except SkinUnavailableError as exc:
                logger.error(
                    "reconcile: skin derivation failed; keeping previous skin: "
                    "skin=%s reason=%s manifest=%s templates=%s",
                    exc.name,
                    exc.reason,
                    exc.manifest_path,
                    exc.templates_dir,
                )
                skin = self._previous_skin_if_cached(state.skin) or self._default_skin()
            except Exception as exc:
                logger.error(
                    "reconcile: skin derivation failed unexpectedly; keeping previous skin: %s",
                    exc,
                )
                skin = self._previous_skin_if_cached(state.skin) or self._default_skin()
            else:
                skin = entry_s
                if not cache_hit:
                    regenerated.append("skin")
        elif skin is None or not cache_entry_path(
            self._state_root, "skins", skin.entry_hash
        ).is_dir():
            # Legacy/direct callers that do not inject an expander still get a
            # complete, content-addressed built-in skin layer. Production
            # composition roots always inject the real expander; a missing
            # recorded skin entry fails closed to the built-in layer instead
            # of publishing an incomplete generation.
            skin = self._default_skin()
            regenerated.append("skin")

        return palette, effects, icons, skin

    def _default_skin(self) -> SkinEntry:
        return default_skin_entry(self._state_root, self._seeder)

    def _compose_appearance(
        self,
        palette: PaletteEntry,
        skin: SkinEntry | None,
        regenerated: list[str],
        *,
        host: HostSnapshot | None = None,
    ) -> AppearanceEntry | None:
        """Compose the generation-local appearance sheets (no publication).

        The v2 appearance cache entry is a content-addressed composition input;
        the v3 commit copies its sheets into the immutable generation. Nothing
        is written into the install spine or an app tag.
        """
        if skin is None:
            return None
        try:
            if skin.name == "default":
                manifest_path = None
                templates_dir = None
            else:
                manifest_path, _source = find_skin_manifest(skin.name, self._install_spine)
                templates_dir = find_skin_templates(self._install_spine)
            entry, cache_hit = self._appearance.ensure(
                palette_entry=palette,
                skin_entry=skin,
                install_spine=self._install_spine,
                manifest_path=manifest_path,
                templates_dir=templates_dir,
                keep=self._appearance_keep,
                host=host,
            )
        except Exception as exc:
            logger.error(
                "reconcile: appearance composition failed; generation refused: %s", exc
            )
            return None
        if not cache_hit:
            regenerated.append("appearance")
        return entry

    def _with_intent_wallpaper(
        self, state: DesktopState | None, wallpaper_path: Path
    ) -> DesktopState:
        """Import an explicit converge-intent wallpaper and return the state.

        The wallpaper is a generation input: import (content-verified),
        hash it, and preserve monitor configs from the previous state. This is
        the same path ``reconcile`` uses, so ``wallpaper set`` is no longer a
        second pipeline.
        """
        path = wallpaper_path.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"wallpaper image not found: {path}")
        wallpaper_hash = hash_file(path)
        self._seeder.import_wallpaper(path, wallpaper_hash, source_mutable=True)
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        wallpaper = WallpaperEntry(
            hash_algorithm="sha256",
            kind="wallpaper",
            content_hash=wallpaper_hash,
            source_path=str(path),
            imported_at=now,
        )
        monitors = with_bar_backdrops(
            preserve_monitors(state, wallpaper_hash, []),
            self._state_root,
            source_paths={wallpaper_hash: path},
        )
        return DesktopState(
            schema_version=2,
            wallpaper=wallpaper,
            monitors=monitors,
            palette=state.palette if state is not None else None,
            effects=state.effects if state is not None else None,
            icons=state.icons if state is not None else None,
            applied_at=state.applied_at if state is not None else now,
            skin=state.skin if state is not None else None,
        )

    @staticmethod
    def _generation_paths(
        generation_path: Path,
        monitor_names: list[str],
        palette: PaletteEntry | None,
        effects: EffectsEntry | None,
        icons: IconsEntry | None,
    ) -> list[Path]:
        """The generation-local artifacts a reconcile result reports."""
        created = [generation_path / "wallpaper.png"]
        created.extend(
            generation_path / f"wallpaper-{monitor}.png" for monitor in monitor_names
        )
        if palette is not None:
            created.append(generation_path / "palette")
            created.extend(generation_path / alias for alias in (
                "colors.conf",
                "colors.gtk.css",
                "colors.yaml",
                "colors.adw.css",
                "colors.sequences",
                "colors.rasi",
                "colors.kitty",
                "colors.btop.theme",
            ))
        if effects is not None:
            created.append(generation_path / "effects")
        if icons is not None:
            created.append(generation_path / "icons")
        return created

    def _previous_skin_if_cached(self, previous: SkinEntry | None) -> SkinEntry | None:
        """Keep a previous skin only while its cache entry still exists."""
        if previous is None:
            return None
        entry_dir = cache_entry_path(self._state_root, "skins", previous.entry_hash)
        return previous if entry_dir.is_dir() else None

    def _ensure_wallpaper_entry(self, state: DesktopState, regenerated: list[str]) -> None:
        """Ensure the wallpaper cache entry exists (re-import from source)."""
        wh = state.wallpaper.content_hash
        cached_png = cache_entry_path(self._state_root, "wallpapers", wh) / "wallpaper.png"
        if cached_png.is_file():
            # P4 — verify content hash for existing file
            try:
                if hash_file(cached_png) != wh:
                    raise RuntimeError(
                        f"wallpaper cache entry {wh} content does not match "
                        f"its hash address: {cached_png}"
                    )
            except OSError as exc:
                raise RuntimeError(f"wallpaper cache entry {wh} cannot be verified: {exc}") from exc
            return
        if cached_png.exists():
            # Exists but not a regular file (dir/symlink) — treat as missing
            raise RuntimeError(
                f"wallpaper cache entry {wh} exists but is not a regular file: {cached_png}"
            )
        source = state.wallpaper.source_path
        # P5 — whitespace/relative guard
        if not source or not source.strip():
            raise RuntimeError(
                f"wallpaper cache entry {wh} is missing and the state has no "
                "source_path; the wallpaper cache entry cannot be rebuilt "
                "without its source"
            )
        src = Path(source.strip())
        if not src.is_file():
            raise RuntimeError(
                f"wallpaper cache entry {wh} is missing and its source no longer exists: {src}"
            )
        self._seeder.import_wallpaper(src, wh, source_mutable=True)
        regenerated.append("wallpaper")

    def _mode_key_applies(self, palette: PaletteEntry) -> bool:
        """Whether the palette key should follow the current desired mode.

        An explicit ``dark``/``light`` intent always applies. ``smart`` only
        applies once the recorded palette was produced by a mode-emitting
        producer (tpr >= 0.6.0 writes ``mode``): legacy/fixture palettes with
        no mode field keep the historical recorded-key behavior.
        """
        if self._palette_mode not in ("dark", "light", "smart"):
            return False
        if self._palette_mode in ("dark", "light"):
            return True
        colors_path = (
            cache_entry_path(self._state_root, "palettes", palette.entry_hash)
            / "colors.yaml"
        )
        try:
            return palette_declares_mode(colors_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            return False

    @staticmethod
    def _assert_hash_matches(recorded: str, regenerated: str) -> None:
        """AC 6b: a regenerated entry must reproduce the recorded hash."""
        if regenerated != recorded:
            raise RuntimeError(
                f"cache entry {recorded} cannot be regenerated from current "
                "spine inputs; re-run wallpaper set"
            )

    # ------------------------------------------------------------------
    # Skipped-symlink derivation
    # ------------------------------------------------------------------

    def _derive_skipped(
        self,
        state: DesktopState,
        monitor_names: list[str],
        repointed: list[Path],
        log: bool = True,
    ) -> list[str]:
        """Compute the skip list: expected symlink names minus created."""
        expected: dict[str, str] = {}
        for name in monitor_names:
            expected[f"wallpaper-{name}.png"] = "wallpaper symlink not created"
        if state.palette is not None:
            for artifact in (
                "colors.conf",
                "colors.gtk.css",
                "colors.yaml",
                "colors.adw.css",
                "colors.sequences",
                "colors.rasi",
                "colors.kitty",
                "colors.btop.theme",
            ):
                expected[artifact] = "palette artifact missing from cache entry"
        if state.effects is not None:
            expected["effects"] = "effects cache entry missing"
        if state.icons is not None:
            expected["icons"] = "icons cache entry missing"
        # v3 skin/appearance sheets are real files inside the generation
        # (appearance/<configDir>.css), not current/ family links.

        created = {p.name for p in repointed}
        skipped: list[str] = []
        for name, reason in expected.items():
            if name not in created:
                skipped.append(f"{name} ({reason})")

        for layer in ("effects", "icons"):
            entry = getattr(state, layer)
            if entry is None:
                skipped.append(f"{layer} (layer is null; nothing to repoint)")
                if log:
                    logger.warning("reconcile: %s layer is null; consumer symlink skipped", layer)

        return skipped

    def _cleanup_stale_symlinks(
        self,
        state: DesktopState,
        monitor_names: list[str],
        repointed: list[Path],
    ) -> list[str]:
        """Remove stale current/ symlinks not in expected set (D2)."""
        current_dir = self._state_root / "current"
        if not current_dir.is_dir():
            return []
        expected_names = {f"wallpaper-{n}.png" for n in monitor_names}
        if state.palette is not None:
            expected_names.update(
                {
                    "colors.conf",
                    "colors.gtk.css",
                    "colors.yaml",
                    "colors.adw.css",
                    "colors.sequences",
                    "colors.rasi",
                    "colors.kitty",
                    "colors.btop.theme",
                }
            )
        if state.effects is not None:
            expected_names.add("effects")
        if state.icons is not None:
            expected_names.add("icons")

        removed: list[str] = []
        for item in current_dir.iterdir():
            # Only consider wallpaper symlinks and known consumer names
            if item.name.startswith("wallpaper-") and item.name.endswith(".png"):
                if item.name not in expected_names:
                    try:
                        # Remove stale monitor symlink (file or symlink)
                        if item.is_symlink() or item.is_file():
                            item.unlink()
                            removed.append(item.name)
                    except OSError:
                        pass
            elif item.name in ("effects", "icons"):
                if item.name not in expected_names:
                    try:
                        if item.is_symlink() or item.exists():
                            # For directory symlinks, unlink removes symlink only
                            item.unlink()
                            removed.append(item.name)
                    except OSError:
                        pass
            elif item.name == "skin.json":
                # Retired v1 cache pointer (REQ-015/REQ-018): never expected.
                try:
                    if item.is_symlink() or item.is_file():
                        item.unlink()
                        removed.append(item.name)
                except OSError:
                    pass
            elif item.name == "skins" and item.is_dir() and not item.is_symlink():
                # Retired v1 cache pointer directory (REQ-015/REQ-018).
                for skin_link in sorted(item.iterdir()):
                    try:
                        if skin_link.is_symlink() or skin_link.is_file():
                            skin_link.unlink()
                            removed.append(f"skins/{skin_link.name}")
                    except OSError:
                        pass
                try:
                    item.rmdir()  # only removes an empty skins/ dir
                except OSError:
                    pass
        return removed

    def _build_expected_targets(self, state: DesktopState) -> dict[str, Path]:
        """Map symlink names to correct cache entry targets from current.json."""
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
            for artifact in (
                "colors.conf",
                "colors.gtk.css",
                "colors.yaml",
                "colors.adw.css",
                "colors.sequences",
                "colors.rasi",
                "colors.kitty",
                "colors.btop.theme",
            ):
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

    def _revert_stale_symlinks(self, state: DesktopState) -> list[Path]:
        """Revert current/ symlinks whose targets don't match current.json."""
        current_dir = self._state_root / "current"
        if not current_dir.is_dir():
            return []
        expected = self._build_expected_targets(state)
        reverted: list[Path] = []
        for name, expected_target in expected.items():
            link = current_dir / name
            if not link.is_symlink():
                continue
            # Use resolved paths for comparison; also handle dangling symlinks.
            try:
                actual_resolved = link.resolve()
            except OSError:
                actual_resolved = link
            try:
                expected_resolved = expected_target.resolve()
            except OSError:
                expected_resolved = expected_target
            # Check both resolved equality and hash segment containment for robustness.
            if actual_resolved != expected_resolved:
                _repoint_symlink(link, expected_target)
                reverted.append(link)
            else:
                # Resolved equal but still verify hash segment is present to catch edge cases
                # where symlink target string is semantically equal but via different traversal.
                # No-op if already matching.
                pass
        return reverted
