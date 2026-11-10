"""Icons-only regeneration use case.

``RegenerateIconsUseCase`` is the GUI's live-toggle path: it re-derives ONLY
the icons layer from the CURRENT palette and reconverges through the same v3
generation commit as every other mutation. It does not touch wallpaper bytes
or re-set pixels (no flicker). Icons render exactly what the manifest resolves;
no runtime retargeting happens here or anywhere else.

Scope boundary vs ``RegenerateStaleUseCase`` (which reconverges ALL stale
layers + full reloads): this command is single-layer by derivation. The
underlying :class:`ReconcileDesktopStateUseCase` still owns the one
intent → derive → stage → commit → current.json → history → GC scope, so the
icons key, generation manifest and history tail are committed together.

History uses the EXISTING closed-enum trigger ``"regenerate"`` plus
``details: {layers: {icons: 1}}``. Events (published by the CLI composition
root, not here) are ``applying`` at start and ``done``/``error`` at finish on
``wallpaper.state`` — never ``visible`` (pixels never change).

Strict idempotence: when the resolved icons entry (and every other generation
input) equals ``current``, the reconcile layer returns ``no_op=True`` without
committing, moving ``current``, saving ``current.json``, appending history,
touching the appearance ledger or refreshing/reloading consumers. A no-op is
therefore *not* recorded as a history line; history describes committed
content changes only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from runtime.adapters.seeder import CacheSeeder
from runtime.application.reconcile import ReconcileDesktopStateUseCase
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
    no_op: bool = False
    """``True`` when the icons cache hit resolves to the current generation
    content: no generation, pointer, history, appearance ledger, app refresh
    or reloader was touched."""


class RegenerateIconsUseCase:
    """Re-derive ONLY icons from the live palette and reconverge.

    Steps:
    1. Load ``current.json`` (fail loud on absent/corrupt — mutating
       nothing).
    2. Reconcile with ``force_icons``: ensure the icons entry via
       ``DerivationPipeline.ensure_icons`` (same manifest path as a full
       set), then commit one generation, save ``current.json``, append one
       ``history.jsonl`` line (trigger ``"regenerate"``,
       ``details.layers == {icons: 1}``), and refresh the injected consumers.
    3. Return the committed state.

    The caller may already hold ``converge.lock`` (the CLI busy contract)
    and pass ``acquire_lock=False``; otherwise the use case acquires the same
    one lock itself. It never acquires a second state lock.
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
        self._state_root = state_root
        self._state_repo = state_repo
        #: The one convergence implementation. The inner state mutex is the
        #: injected pass-through; ``converge.lock`` is the only real lock.
        self._reconcile = ReconcileDesktopStateUseCase(
            state_repo=state_repo,
            palette_producer=palette_producer,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=mutex,
            reloaders=list(reloaders) if reloaders is not None else [],
            # Icons-only means no wallpaper re-apply and no ordinary reloaders
            # unless the caller injected them; AGS refresh is manifest-driven.
            wallpaper_applier=None,
            app_refresher=app_refresher,
            source_kind="cli",
        )

    def run(self, *, acquire_lock: bool = True) -> RegenerateIconsResult:
        """Regenerate icons from the live palette and commit one generation.

        Raises:
            RuntimeError: when ``current.json`` is absent, the palette layer
                is missing, or the generation cannot be committed.
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

        reconciled = self._reconcile.run(
            trigger="regenerate",
            acquire_lock=acquire_lock,
            force_icons=True,
            idempotent=True,
            history_details={"layers": {"icons": 1}},
        )
        icons = reconciled.state.icons
        if icons is None:
            raise RuntimeError(
                "icons regenerate did not commit an icons layer; the previous "
                "generation remains current"
            )
        return RegenerateIconsResult(
            icons=icons,
            cache_hit="icons" not in reconciled.cache_regenerated,
            state=reconciled.state,
            # v3 has no per-family current/ links to report.
            repointed=[],
            consumer_symlinks=[],
            reload_failures=list(reconciled.reload_failures),
            app_refresh_results=list(reconciled.app_refresh_results),
            no_op=reconciled.no_op,
        )
