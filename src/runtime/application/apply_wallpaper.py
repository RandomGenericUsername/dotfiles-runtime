"""Apply-wallpaper use case — derive, cache, persist the desktop state.

Implements AD-1 (hexagonal), AD-2 (input changes invalidate), AD-5
(state_root), AD-6 (swap symlinks lead), AD-11 (read-only spine reads),
AD-14 (domain purity), AD-15 (cross-package boundary), AD-18 (preserve
monitor configs), AD-19/AD-20 (CLI rendering).

Scope boundary (Story 1.13 — the Epic-1-to-Epic-2 seam):
- Derives the three layers for a user-chosen wallpaper, ensures cache
  entries (cache hit = entry-dir existence; zero tool invocations on
  hit), and persists ``current.json`` (schema_version 2, atomic).
- Does NOT repoint ``current/`` symlinks, does NOT append
  ``history.jsonl``, and does NOT invoke any desktop reload — the swap
  sequence is owned by ``ReconcileDesktopStateUseCase``. The
  ``wallpaper set`` capstone (Story 2.7, shipped) chains this use case
  into reconcile in the CLI composition root; the apply use case's own
  contract is unchanged: it updates desired state and delegates the
  actual swap to reconcile.

Architecture:
- Lives in ``application/`` (use-case layer) per AD-1, AD-13
- Orchestrates injected ports and the injected ``CacheSeeder`` adapter;
  constructs no concrete adapters itself (composition root wires them)
- No raw ``os``/``json`` FS I/O in the use case (delegated to adapters)
- Failure policy mirrors ``SeedCacheUseCase``: palette is a hard
  dependency (``palette apply failed:``), effects/icons degrade
  gracefully with a visible warning; corrupt state propagates loudly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.hashing import hash_file
from runtime.adapters.seeder import CacheSeeder
from runtime.application.bar_backdrop import with_bar_backdrops
from runtime.application.derive import DerivationPipeline
from runtime.application.monitors import preserve_monitors
from runtime.domain.models import (
    DesktopState,
    EffectsEntry,
    IconsEntry,
    MonitorWallpaperConfig,
    PaletteEntry,
    PaletteUnavailableError,
    WallpaperEntry,
)
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.icon_renderer import IIconRenderer
from runtime.ports.monitor_source import IMonitorSource
from runtime.ports.seed_mutex import ISeedMutex
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplyWallpaperResult:
    """Outcome of one ``ApplyWallpaperUseCase.run`` invocation."""

    wallpaper_hash: str
    palette: PaletteEntry
    effects: EffectsEntry | None
    icons: IconsEntry | None
    cache_hit_palette: bool
    cache_hit_effects: bool
    cache_hit_icons: bool
    state: DesktopState


@dataclass(frozen=True, slots=True)
class PreparedPalette:
    """Palette prepared BEFORE the visible swap (BUG-001).

    Produced by :meth:`ApplyWallpaperUseCase.prepare_palette`: the input is
    validated, hashed and imported, then the palette layer is ensured as a
    HARD dependency. Nothing in ``current.json``/``current/`` changes yet.
    """

    wallpaper_hash: str
    palette: PaletteEntry
    cache_hit: bool


class ApplyWallpaperUseCase:
    """Derive, cache, and persist the state for a user-set wallpaper.

    Steps:
    1. Resolve + validate the input (absolute, existing regular file)
    2. Load current state (fail-fast corrupt-state guard)
    3. Hash the wallpaper (sha256 of file bytes) and import it into the
       cache via the seeder (copy policy — the cache owns its bytes for
       user-supplied files; content-verified; meta.json write-once),
       unless the caller passes a pre-imported ``wallpaper_hash`` (the
       visible-first swap already imported it — derivation-only duty,
       no re-import)
    4. Ensure palette (hard), effects and icons (graceful) cache entries
       via the shared ``DerivationPipeline`` — outside the mutex (staging
       is race-safe; tool invocations stay parallel)
    5. Hold the state mutex (blocking) around the read-modify-write of
       ``current.json`` — a concurrent first-run seed can never be
       overtaken by an applier that observed absent state; monitors
       preserved with ``source_hash`` updated (AD-18), defaulted when no
       state or empty monitors
    6. Save ``current.json`` via the injected state repository (atomic)

    Constructor receives ports, the injected ``CacheSeeder`` adapter and
    the state mutex (dependency inversion): the use case wires no concrete
    adapters itself.
    """

    def __init__(
        self,
        state_repo: IStateRepository,
        csg: IColorSchemeGenerator,
        weg: IEffectsGenerator,
        itr: IIconRenderer,
        install_spine: Path,
        state_root: Path,
        seeder: CacheSeeder,
        mutex: ISeedMutex,
        monitor_source: IMonitorSource | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._install_spine = install_spine
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex
        self._monitor_source = monitor_source
        #: Real producer identity for typed failures (tpr must never be csg).
        self._producer_name = getattr(csg, "producer_name", "csg")
        self._pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
        )

    def prepare_palette(
        self,
        image_path: Path,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> PreparedPalette:
        """Prepare the REQUIRED palette before any visible change (BUG-001).

        Validates + hashes + imports the wallpaper, then ensures the
        palette cache entry as a hard dependency. On failure it raises
        :class:`PaletteUnavailableError` carrying the wallpaper identity,
        stage, tool, return code and stderr tail — and writes NOTHING to
        ``current.json``/``current/`` (only harmless cache staging may
        exist; the last-good desktop state is untouched).

        The caller passes the result to ``SwapVisibleUseCase.run`` (which
        persists it) and to :meth:`run` (which skips re-derivation).
        """
        img = self._validate_input(image_path)

        # Corrupt-state guard (fail fast): a corrupt current.json must abort
        # before any cache/state work, exactly like the themed pass.
        self._state_repo.load_current()

        wallpaper_hash = hash_file(img)
        self._seeder.import_wallpaper(img, wallpaper_hash, source_mutable=True)

        try:
            palette, cache_hit = self._pipeline.ensure_palette(img, wallpaper_hash)
        except PaletteUnavailableError:
            raise
        except Exception as exc:
            stderr = str(getattr(exc, "stderr", "") or exc)
            raise PaletteUnavailableError(
                f"palette apply failed: {exc}",
                wallpaper_path=str(img),
                wallpaper_hash=wallpaper_hash,
                tool=self._producer_name,
                returncode=getattr(exc, "returncode", None),
                stdout=str(getattr(exc, "stdout", "") or ""),
                stderr=stderr[-2048:],
                elapsed_seconds=getattr(exc, "elapsed_seconds", None),
                timed_out=bool(getattr(exc, "timed_out", False)),
                termination=getattr(exc, "termination", None),
                missing_artifacts=tuple(getattr(exc, "missing_artifacts", ()) or ()),
                hint=f"re-set the wallpaper / check {self._producer_name}",
            ) from exc

        if on_progress is not None:
            on_progress("palette_generated")
        return PreparedPalette(
            wallpaper_hash=wallpaper_hash,
            palette=palette,
            cache_hit=cache_hit,
        )

    def run(
        self,
        image_path: Path,
        *,
        wallpaper_hash: str | None = None,
        prepared_palette: PreparedPalette | None = None,
        contrast_enabled: bool = True,
        contrast_source: str = "default",
        on_progress: Callable[[str], None] | None = None,
    ) -> ApplyWallpaperResult:
        """Apply a wallpaper: derive → cache → persist ``current.json``.

        Variant inputs (an image already living under
        ``<state_root>/cache/effects/``, i.e. a WEG-derived variant
        promoted to wallpaper) skip the effects layer — feeding a
        derived artifact back into its own derivation is a loop — while
        palette and icons still derive from the variant's pixels.

        Args:
            image_path: user-supplied wallpaper file (validated, and used
                for the persisted ``source_path`` in both paths).
            wallpaper_hash: precomputed SHA-256 of the input. The
                visible-first ``wallpaper set`` passes the hash the
                prepare phase already imported, so this themed pass
                performs NO re-import and NO palette re-derivation when
                ``prepared_palette`` is supplied.
            prepared_palette: palette prepared (hard) BEFORE the swap by
                :meth:`prepare_palette`. ``None`` keeps the standalone
                derivation path (hash + import + ensure_palette).
            contrast_enabled: per-wallpaper icon-contrast policy for this
                run (resolved by the composition root from ``--contrast``
                + the prefs store). ``False`` renders the spine mappings
                unchanged.
            contrast_source: provenance of ``contrast_enabled``
                (``"flag"``/``"store"``/``"default"``), recorded in the
                icons entry ``contrast.policy``.

        Raises:
            ValueError: if the input path is missing, empty, or not a
                regular file; or propagated from a corrupt current.json
            PaletteUnavailableError: if the palette (hard dependency)
                cannot be produced; ``current.json`` is left unchanged
            RuntimeError: if the cache holds content that contradicts its
                hash
            OSError: on filesystem failures (unreadable input, etc.)
        """
        img = self._validate_input(image_path)

        # Corrupt-state guard (fail fast, mirrors seed's fast path): a
        # ValueError/RuntimeError from a corrupt store propagates loudly —
        # do NOT swallow into a fresh state. Re-read inside the critical
        # section below (double-checked pattern).
        self._state_repo.load_current()

        if prepared_palette is not None:
            if (
                wallpaper_hash is not None
                and wallpaper_hash != prepared_palette.wallpaper_hash
            ):
                raise ValueError(
                    "prepared palette wallpaper hash does not match the requested "
                    f"wallpaper: prepared={prepared_palette.wallpaper_hash} "
                    f"requested={wallpaper_hash}"
                )
            wallpaper_hash = prepared_palette.wallpaper_hash
            palette = prepared_palette.palette
            cache_hit_palette = prepared_palette.cache_hit
        else:
            if wallpaper_hash is None:
                wallpaper_hash = hash_file(img)

                # Wallpaper layer: import into the cache (copy policy — cache
                # owns its bytes; idempotent, content-verified, meta.json
                # write-once).
                self._seeder.import_wallpaper(img, wallpaper_hash, source_mutable=True)

            # Palette (hard dependency): failure aborts the apply — save()
            # happens only after all layers succeed/degrade, so current.json
            # is left unchanged.
            try:
                palette, cache_hit_palette = self._pipeline.ensure_palette(
                    img, wallpaper_hash
                )
            except PaletteUnavailableError:
                raise
            except Exception as exc:
                stderr = str(getattr(exc, "stderr", "") or exc)
                raise PaletteUnavailableError(
                    f"palette apply failed: {exc}",
                    wallpaper_path=str(img),
                    wallpaper_hash=wallpaper_hash,
                    returncode=getattr(exc, "returncode", None),
                    stderr=stderr[-2048:],
                    hint="re-set the wallpaper / check csg",
                ) from exc

            # Informational callback only: the CLI adapts this application-
            # stage boundary to wallpaper.state. It never controls derivation
            # or locking. The visible-first CLI emits this during
            # prepare_palette (before the swap), so standalone callers keep it.
            if on_progress is not None:
                on_progress("palette_generated")

        # Informational callback only: the palette is already prepared; the
        # remaining appearance assets (effects/icons) are derived next.
        if on_progress is not None:
            on_progress("preparing_appearance_assets")

        # Effects + icons (graceful degradation): entry None on failure,
        # current.json field null (history schema allows nulls). A variant
        # input (already a WEG artifact) skips effects derivation outright.
        effects: EffectsEntry | None = None
        cache_hit_effects = False
        if self._is_weg_artifact(img):
            logger.info("apply: input is a WEG-derived variant; skipping effects generation")
        else:
            try:
                effects, cache_hit_effects = self._pipeline.ensure_effects(img, wallpaper_hash)
            except Exception as exc:
                logger.warning("apply: effects generation failed; continuing: %s", exc)

        icons: IconsEntry | None = None
        cache_hit_icons = False
        try:
            icons, cache_hit_icons = self._pipeline.ensure_icons(
                palette.entry_hash,
                contrast_enabled=contrast_enabled,
                contrast_source=contrast_source,
            )
        except Exception as exc:
            logger.warning("apply: icon rendering failed; continuing: %s", exc)

        # Persist ONLY current.json — no symlink repoint, no history
        # append, no reload (AC 6; ReconcileDesktopStateUseCase owns the
        # swap sequence; the CLI capstone chains reconcile after apply).
        # The read-modify-write of the state is
        # serialized against a concurrent first-run seed (and other
        # applies) via the blocking state mutex: the authoritative
        # load_current() happens inside the critical section so a seed
        # landing between derivation and save can never be overtaken.
        with self._mutex.hold(blocking=True):
            existing = self._state_repo.load_current()
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            wallpaper_entry = WallpaperEntry(
                hash_algorithm="sha256",
                kind="wallpaper",
                content_hash=wallpaper_hash,
                source_path=str(img),
                imported_at=now,
            )
            state = DesktopState(
                schema_version=2,
                wallpaper=wallpaper_entry,
                monitors=with_bar_backdrops(
                    self._build_monitors(existing, wallpaper_hash),
                    self._state_root,
                    source_paths={wallpaper_hash: img},
                ),
                palette=palette,
                effects=effects,
                icons=icons,
                applied_at=now,
            )
            self._state_repo.save(state)

        if on_progress is not None:
            on_progress("appearance_assets_ready")

        return ApplyWallpaperResult(
            wallpaper_hash=wallpaper_hash,
            palette=palette,
            effects=effects,
            icons=icons,
            cache_hit_palette=cache_hit_palette,
            cache_hit_effects=cache_hit_effects,
            cache_hit_icons=cache_hit_icons,
            state=state,
        )

    def _is_weg_artifact(self, img: Path) -> bool:
        """True when the input already lives in the effects cache layer.

        ``img`` is validated absolute at this point; any file under
        ``<state_root>/cache/effects/`` is a derived artifact, not a
        derivation input, so the effects layer must not run on it
        (variants-of-variants). The palette and icons layers are
        unaffected — they still derive from the variant's pixels.
        """
        try:
            return img.is_relative_to(self._state_root / "cache" / "effects")
        except OSError, ValueError:
            return False

    @staticmethod
    def _validate_input(image_path: Path) -> Path:
        """Resolve the input to an absolute path and validate it.

        Rejects missing files, directories, and anything that is not a
        regular file with ``ValueError`` (the CLI maps this to a
        non-zero exit). Empty paths resolve to the CWD (a directory) and
        are rejected by the same guard.
        """
        img = image_path.expanduser().resolve()
        if not img.exists():
            raise ValueError(f"wallpaper image not found: {img}")
        if not img.is_file():
            raise ValueError(f"wallpaper image must be a regular file, got: {img}")
        return img

    def _build_monitors(
        self, existing: DesktopState | None, wallpaper_hash: str
    ) -> dict[str, MonitorWallpaperConfig]:
        """Preserve existing monitor configs, reconciled against live outputs.

        Thin wrapper over the shared :func:`preserve_monitors
        <runtime.application.monitors.preserve_monitors>` helper (AD-18;
        shared with ``SwapVisibleUseCase``): live detection from the
        injected ``IMonitorSource`` is authoritative for monitor names
        when non-empty, stored configs are kept otherwise. See the helper
        for the full contract.
        """
        detected = (
            self._monitor_source.detect_monitors() if self._monitor_source is not None else []
        )
        return preserve_monitors(existing, wallpaper_hash, detected)
