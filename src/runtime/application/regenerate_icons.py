"""Icons-only regeneration use case (``icon-contrast-opt-out`` D3).

``RegenerateIconsUseCase`` is the GUI's live-toggle path: it re-derives
ONLY the icons layer from the CURRENT palette under the resolved
contrast policy and reconverges (repoint + AGS-only reload) — without
touching wallpaper/palette/effects or re-setting pixels (no flicker).

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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from runtime.adapters.cache import cache_entry_path
from runtime.adapters.icon_contrast_prefs_store import (
    governing_wallpaper_hash,
    resolve_contrast,
    store_path,
    write_pref,
)
from runtime.adapters.seeder import CacheSeeder
from runtime.application.derive import DerivationPipeline
from runtime.domain.icon_contrast_policy import CONTRAST_FLAGS
from runtime.domain.models import DesktopState, IconsEntry
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
    reload_failures: list[str]
    contrast_enabled: bool
    contrast_source: str


class RegenerateIconsUseCase:
    """Re-derive ONLY icons from the live palette and reconverge.

    Steps:
    1. Load ``current.json`` (fail loud on absent/corrupt — like apply's
       guard — mutating nothing).
    2. Resolve the contrast policy for the governing live hash (D2a: a
       live variant resolves to its parent wallpaper via the effects
       entry's ``source_wallpaper_hash``). An explicit ``on``/``off``
       flag persists the choice for the governing hash BEFORE deriving.
    3. Ensure the icons entry via ``DerivationPipeline.ensure_icons``
       (same overlay path as a full set, so results are identical).
    4. Under the state mutex (blocking): re-load (concurrent-modification
       guard — a ``wallpaper set`` landing mid-run aborts loud instead of
       clobbering), repoint ONLY the ``current/icons`` symlink, refresh
       the spine consumer pointers, and save ``current.json`` with the
       new icons entry.
    5. Append one ``history.jsonl`` line (trigger ``"regenerate"``,
       ``details.layers == {icons: 1}``).
    6. Reload the injected reloaders (the CLI wires AGS only).

    Constructor receives ports, the injected ``CacheSeeder`` adapter, the
    state mutex, and ``IDesktopReloader`` instances (dependency inversion
    — the use case wires no concrete adapters itself).
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
        reloaders: list[IDesktopReloader] | None = None,
    ) -> None:
        self._state_repo = state_repo
        self._install_spine = install_spine
        self._state_root = state_root
        self._seeder = seeder
        self._mutex = mutex
        self._reloaders: list[IDesktopReloader] = list(reloaders) if reloaders is not None else []
        self._pipeline = DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
        )

    def run(self, *, contrast: str = "auto") -> RegenerateIconsResult:
        """Regenerate icons from the live palette and reconverge.

        Args:
            contrast: ``"auto"`` (store → default ON), ``"on"``/``"off"``
                (force this run AND persist for the governing hash).

        Raises:
            ValueError: on an unknown ``contrast`` flag, a corrupt
                ``current.json``, or a missing palette layer.
            RuntimeError: when ``current.json`` is absent or a concurrent
                writer won the race mid-run.
            OSError: on filesystem failures.
        """
        if contrast not in CONTRAST_FLAGS:
            raise ValueError(
                f"invalid contrast flag: {contrast!r} (expected one of {', '.join(CONTRAST_FLAGS)})"
            )
        state = self._state_repo.load_current()
        if state is None:
            raise RuntimeError(
                "nothing to regenerate: no runtime state recorded yet "
                "(run `dotfiles-runtime wallpaper set <img>` first)"
            )
        if state.palette is None:
            raise RuntimeError("icons regenerate needs a palette layer but none is recorded")
        governing = governing_wallpaper_hash(
            Path(state.wallpaper.source_path),
            self._state_root,
            fallback_hash=state.wallpaper.content_hash,
        )
        prefs_file = store_path(self._state_root)
        if contrast == "auto":
            enabled, source = resolve_contrast(
                flag="auto", governing_hash=governing, store_file=prefs_file
            )
        else:
            enabled, source = (contrast == "on"), "flag"
            try:
                write_pref(prefs_file, governing, enabled)
            except OSError as exc:
                logger.warning(
                    "icons regenerate: contrast pref persist failed (%s); "
                    "continuing with the forced value",
                    exc,
                )

        icons, cache_hit = self._pipeline.ensure_icons(
            state.palette.entry_hash,
            contrast_enabled=enabled,
            contrast_source=source,
        )

        with self._mutex.hold(blocking=True):
            current = self._state_repo.load_current()
            if current is None or current.wallpaper.content_hash != state.wallpaper.content_hash:
                raise RuntimeError(
                    "concurrent modification detected during icons regeneration "
                    "(another writer won the race); re-run the command"
                )
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            saved = dataclasses.replace(current, icons=icons, applied_at=now)
            icons_dir = cache_entry_path(self._state_root, "icons", icons.entry_hash)
            repointed = [self._seeder.repoint_current_symlink("icons", icons_dir)]
            consumer_symlinks = self._seeder.repoint_consumer_symlinks(
                self._install_spine,
                saved.palette.entry_hash if saved.palette else None,
            )
            self._state_repo.save(saved)

        self._seeder.append_history(
            trigger="regenerate",
            wallpaper_hash=saved.wallpaper.content_hash,
            palette_hash=saved.palette.entry_hash if saved.palette else None,
            effects_hash=saved.effects.entry_hash if saved.effects else None,
            icons_hash=saved.icons.entry_hash if saved.icons else None,
            source_path=saved.wallpaper.source_path,
            details={"layers": {"icons": 1}},
        )

        reload_failures: list[str] = []
        for reloader in self._reloaders:
            try:
                ok = reloader.reload()
            except Exception as exc:
                logger.warning("desktop reload failed for %s: %s", type(reloader).__name__, exc)
                ok = False
            if not ok:
                reload_failures.append(type(reloader).__name__)

        return RegenerateIconsResult(
            icons=icons,
            cache_hit=cache_hit,
            state=saved,
            repointed=repointed,
            consumer_symlinks=consumer_symlinks,
            reload_failures=reload_failures,
            contrast_enabled=enabled,
            contrast_source=source,
        )
