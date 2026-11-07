"""Icons-only regeneration use case.

``RegenerateIconsUseCase`` is the GUI's live-toggle path: it re-derives
ONLY the icons layer from the CURRENT palette and reconverges (repoint +
AGS-only reload) — without touching wallpaper/palette/effects or re-setting
pixels (no flicker). Icons render exactly what the manifest resolves; no
runtime retargeting happens here or anywhere else.

Scope boundary vs ``RegenerateStaleUseCase`` (which reconverges ALL stale
layers + full reloads): this command is single-layer by construction.
Palette/effects layers are never touched; wallpaper pixels are never
re-set. History uses the EXISTING closed-enum trigger ``"regenerate"``
plus ``details: {layers: {icons: 1}}`` (both schema-valid today — no
enum change). Events (published by the CLI composition root, not here)
are ``applying`` at start and ``done``/``error`` at finish on
``wallpaper.state`` — never ``visible`` (pixels never change).
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.seeder import CacheSeeder
from runtime.application.derive import DerivationPipeline
from runtime.domain.ags_apps import AppRefreshRecord, ReloadFailure
from runtime.domain.models import DesktopState, IconsEntry
from runtime.ports.ags_app_refresher import IAgsAppRefresher
from runtime.ports.color_scheme_generator import IColorSchemeGenerator
from runtime.ports.desktop_reloader import IDesktopReloader
from runtime.ports.effects_generator import IEffectsGenerator
from runtime.ports.icon_renderer import IIconRenderer
from runtime.ports.seed_mutex import ISeedMutex
from runtime.ports.state_repository import IStateRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RegenerateIconsResult:
    """Outcome of one ``RegenerateIconsUseCase.run`` invocation."""

    icons: IconsEntry
    cache_hit: bool
    state: DesktopState
    repointed: list[Path]
    consumer_symlinks: list[Path]
    reload_failures: list[ReloadFailure]
    app_refresh_results: list[AppRefreshRecord] = field(default_factory=list)


class RegenerateIconsUseCase:
    """Re-derive ONLY icons from the live palette and reconverge.

    Steps:
    1. Load ``current.json`` (fail loud on absent/corrupt — mutating
       nothing).
    2. Ensure the icons entry via ``DerivationPipeline.ensure_icons``
       (same manifest path as a full set, so results are identical).
    3. Under the state mutex (blocking): re-load (concurrent-modification
       guard — a ``wallpaper set`` landing mid-run aborts loud instead of
       clobbering), repoint ONLY the ``current/icons`` symlink, refresh
       the spine consumer pointers, and save ``current.json`` with the
       new icons entry.
    4. Append one ``history.jsonl`` line (trigger ``"regenerate"``,
       ``details.layers == {icons: 1}``).
    5. Reload the injected reloaders (the CLI wires AGS only).

    Constructor receives ports, the injected ``CacheSeeder`` adapter, the
    state mutex, and ``IDesktopReloader`` instances (dependency inversion
    — the use case wires no concrete adapters itself).
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
        app_refresher: IAgsAppRefresher | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._install_spine = install_spine
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex
        self._reloaders: list[IDesktopReloader] = list(reloaders) if reloaders is not None else []
        self._app_refresher = app_refresher
        self._pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
        )

    def run(self) -> RegenerateIconsResult:
        """Regenerate icons from the live palette and reconverge.

        Raises:
            RuntimeError: when ``current.json`` is absent, the palette layer
                is missing, or a concurrent writer won the race mid-run.
            OSError: on filesystem failures.
        """
        state = self._state_repo.load_current()
        if state is None:
            raise RuntimeError(
                "nothing to regenerate: no runtime state recorded yet "
                "(run `dotfiles-runtime wallpaper set <img>` first)"
            )
        if state.palette is None:
            raise RuntimeError("icons regenerate needs a palette layer but none is recorded")

        icons, cache_hit = self._pipeline.ensure_icons(state.palette.entry_hash)

        with self._mutex.hold(blocking=True):
            current = self._state_repo.load_current()
            if current is None or current.wallpaper.content_hash != state.wallpaper.content_hash:
                raise RuntimeError(
                    "concurrent modification detected during icons regeneration "
                    "(another writer won the race); re-run the command"
                )
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            saved = dataclasses.replace(current, icons=icons, applied_at=now)
            # v3: state is an input to the next converge. Never repoint a
            # per-family current/ link or write into the install spine.
            repointed: list[Path] = []
            consumer_symlinks: list[Path] = []
            self._state_repo.save(saved)

        self._seeder.append_history(
            trigger="regenerate",
            wallpaper_hash=saved.wallpaper.content_hash,
            palette_hash=saved.palette.entry_hash if saved.palette else None,
            effects_hash=saved.effects.entry_hash if saved.effects else None,
            icons_hash=saved.icons.entry_hash if saved.icons else None,
            skin_hash=saved.skin.entry_hash if saved.skin else None,
            source_path=saved.wallpaper.source_path,
            details={"layers": {"icons": 1}},
        )

        reload_failures: list[ReloadFailure] = []
        app_results: list[AppRefreshRecord] = []
        if self._app_refresher is not None:
            from runtime.adapters.generation_store import GenerationStore

            generation_hash = GenerationStore(self._state_root).current_hash()
            try:
                if generation_hash is None:
                    raise RuntimeError("no committed generation")
                outcome = self._app_refresher.refresh(
                    saved, generation_hash=generation_hash, trigger="regenerate"
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
        for reloader in self._reloaders:
            try:
                ok = reloader.reload()
            except Exception as exc:
                logger.warning("desktop reload failed for %s: %s", type(reloader).__name__, exc)
                ok = False
            if not ok:
                reload_failures.append(ReloadFailure(type(reloader).__name__))

        return RegenerateIconsResult(
            icons=icons,
            cache_hit=cache_hit,
            state=saved,
            repointed=repointed,
            consumer_symlinks=consumer_symlinks,
            reload_failures=reload_failures,
            app_refresh_results=app_results,
        )
