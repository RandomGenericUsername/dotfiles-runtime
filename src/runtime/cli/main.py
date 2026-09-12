"""Typer CLI — the composition root (outer shell) of the runtime hexagon."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from cli_output.adapters.factory import create_renderer
from cli_output.domain.enums import OutputFormat
from cli_output.domain.views import CustomView, ErrorView

if TYPE_CHECKING:
    from cli_output.adapters.factory import Renderer

    from runtime.application.apply_wallpaper import ApplyWallpaperResult
    from runtime.application.check_inputs import CheckInputsResult
    from runtime.application.doctor import DoctorReport, RepairResult
    from runtime.application.inspect import (
        HistoryRecord,
        InspectCacheResult,
        InspectStatusResult,
    )
    from runtime.application.planner import ConvergenceReport
    from runtime.application.prune import PrunePlan
    from runtime.application.reconcile import ReconcileResult
    from runtime.application.regenerate import RegenerateResult
    from runtime.application.verify_cache import VerifyCacheResult
    from runtime.domain.models import ChangeSet, DesktopState
    from runtime.ports.desktop_reloader import IDesktopReloader

app = typer.Typer(
    name="dotfiles-runtime",
    help=(
        "Dotfiles runtime engine.\n\n"
        "Manages wallpaper, state, history, and cache for the dotfiles system.\n\n"
        "Commands:\n"
        "  version  Show the installed package version\n"
        "  wallpaper set  Derive, cache, swap, reload, and persist state for a wallpaper"
    ),
)

wallpaper_app = typer.Typer(help="Wallpaper commands")
app.add_typer(wallpaper_app, name="wallpaper")

inspect_app = typer.Typer(help="Inspect commands")
app.add_typer(inspect_app, name="inspect")

cache_app = typer.Typer(help="Cache commands")
inspect_app.add_typer(cache_app, name="cache")

logger = logging.getLogger(__name__)


def _resolve_install_spine() -> Path:
    """Resolve provisioning install spine path (read-only, absolute).

    Resolution order (AD-15):
    1. Check ``$DOTFILES_INSTALL_SPINE`` env var (explicit override)
    2. Fallback: ``$XDG_DATA_HOME/dotfiles`` (default ``~/.local/share/dotfiles``)

    The result is always expanded and resolved to an absolute path: relative
    XDG/env values would otherwise make symlink targets and repo-fallback
    template discovery depend on the invoking CWD.
    """
    if explicit := os.environ.get("DOTFILES_INSTALL_SPINE"):
        return Path(explicit).expanduser().resolve()
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return (xdg_data / "dotfiles").expanduser().resolve()


def _resolve_state_root() -> Path:
    """Resolve runtime state root path (absolute).

    Uses ``$XDG_STATE_HOME/dotfiles`` (default ``~/.local/state/dotfiles``).
    Resolved to an absolute path so symlink targets never dangle when the
    env var holds a relative value.
    """
    xdg_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return (xdg_state / "dotfiles").expanduser().resolve()


def _run_seed_if_needed() -> None:
    """Run first-run seeding if current.json is absent.

    Validates provisioning output before constructing the use case, then
    runs it. Failures are logged loudly (error level) but do not prevent
    CLI commands from executing.
    """
    state_root = _resolve_state_root()
    install_spine = _resolve_install_spine()

    # Pre-validate before constructing anything (Task 3 contract): a missing
    # provisioning spine is expected on machines provisioned without a default
    # desktop — skip quietly; anything else fails loudly below.
    # The wallpaper itself lives in the assets role's output (Story 2-6
    # AC 2): the ``wallpapers.tar.gz`` tarball unpacks to
    # ``<install>/wallpapers/`` including ``default.png``.
    default_png = install_spine / "wallpapers" / "default.png"
    if not default_png.is_file():
        logger.warning(
            "seed skipped: provisioning output not found (%s); "
            "run provisioning to enable first-run seeding",
            default_png,
        )
        return

    try:
        from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
        from runtime.adapters.csg_adapter import CsgAdapter
        from runtime.adapters.flock_seed_mutex import FlockSeedMutex
        from runtime.adapters.hyprland_monitor_source import HyprlandMonitorSource
        from runtime.adapters.itr_adapter import ItrAdapter
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.adapters.seeder import CacheSeeder
        from runtime.adapters.weg_adapter import WegAdapter
        from runtime.application.seed_cache import SeedCacheUseCase
        from runtime.ports.seed_mutex import SeedLockedError
        from runtime.ports.wallpaper_backend import IStaticWallpaperBackend, IVideoWallpaperBackend
        from runtime.ports.wallpaper_backend_factory import (
            IWallpaperBackendFactory,
            _validate_static_backend,
            _validate_video_backend,
        )

        state_repo = JsonStateRepository(state_root=state_root)
        csg = CsgAdapter()
        weg = WegAdapter()
        itr = ItrAdapter()
        seeder = CacheSeeder(state_root, consumer_spec=StaticConsumerPathSpec())
        mutex = FlockSeedMutex(state_root / ".seed.lock")

        # Factory is not used by SeedCacheUseCase (wallpaper backend deferred to Epic 2).
        # Faithful placeholder: validates backend types per the port contract
        # (Literal is static-only) and refuses to create backends.
        class _NoOpFactory(IWallpaperBackendFactory):
            def create_static(
                self,
                backend_type: object,
            ) -> IStaticWallpaperBackend:
                _validate_static_backend(backend_type)
                raise NotImplementedError("wallpaper backend deferred to Epic 2")

            def create_video(
                self,
                backend_type: object,
            ) -> IVideoWallpaperBackend:
                _validate_video_backend(backend_type)
                raise NotImplementedError("wallpaper backend deferred to Epic 2")

            def auto_detect(self, source_path: str) -> None:
                return None

        use_case = SeedCacheUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            factory=_NoOpFactory(),
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=mutex,
            monitor_source=HyprlandMonitorSource(),
        )
        use_case.run()
    except SeedLockedError as exc:
        # Another process is seeding right now — its result is authoritative.
        logger.info("seed skipped: %s", exc)
    except (ValueError, RuntimeError) as exc:
        # Corrupt state, palette generation failure, provisioning contract
        # violations — must be observable (debug-level hid real failures).
        logger.error(
            "first-run seeding failed: %s — inspect/repair state under %s and re-run",
            exc,
            state_root,
        )
    except Exception:
        logger.exception("first-run seeding failed unexpectedly")


@app.callback()
def main_callback(
    ctx: typer.Context,
    output_format: OutputFormat = typer.Option(
        OutputFormat.PLAIN,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    # Surface INFO logs (e.g. prune's per-removal line) when no handler is
    # configured; never override an embedding app's logging.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # P3 — do not auto-seed before reconcile (reconcile must fail loud on
    # absent state). Same for the read-only inspect commands (Story 3.2,
    # AC 3): auto-seeding would mask the absent-state error on provisioned
    # machines and invoke csg/weg/itr for free behind a read command.
    # The COMMAND comes from typer's resolved context (never argv parsing:
    # operands named "reconcile"/flag values must not skip seeding, and
    # flag-first invocations must).
    if ctx.invoked_subcommand in ("reconcile", "inspect", "doctor"):
        return
    _run_seed_if_needed()


@dataclass(frozen=True, slots=True)
class _WallpaperSetResult:
    """Combined outcome of the ``wallpaper set`` apply→reconcile chain.

    Keeps the apply's per-layer cache-hit descriptors (which layers were
    derived vs served from cache) alongside the reconcile's swap/reload
    data (``repointed``/``skipped``/``cache_regenerated``/
    ``reload_failures``) so the CLI renders both coherently.
    """

    apply: ApplyWallpaperResult
    reconcile: ReconcileResult


def _build_reloaders(state_root: Path) -> list[IDesktopReloader]:
    """Build the deterministic four-consumer reloader list (AD-17).

    Shared by ``reconcile`` and ``wallpaper set`` so both commands reload
    the IDENTICAL consumers in the same order: Hyprland (``hyprctl
    reload``), AGS (restart), Hyprpaper (per-monitor IPC from
    ``current.json``), terminal palette (OSC from ``current/colors.sequences``).
    """
    from runtime.adapters.ags_reloader import AgsReloader
    from runtime.adapters.hyprland_reloader import HyprlandReloader
    from runtime.adapters.hyprpaper_reloader import HyprpaperReloader
    from runtime.adapters.terminal_color_applier import TerminalColorApplier

    return [
        HyprlandReloader(),
        AgsReloader(),
        HyprpaperReloader(state_root=state_root),
        TerminalColorApplier(state_root=state_root),
    ]


def _run_wallpaper_set(image_path: Path) -> _WallpaperSetResult:
    """Compose and run ApplyWallpaperUseCase → ReconcileDesktopStateUseCase.

    The full ``wallpaper set`` pipeline (AD-12): apply derives the three
    layers, ensures cache entries, and persists ``current.json``; the
    chained reconcile then performs the swap sequence (cache-ensure →
    parent-first symlink repoint → ``current.json`` → ``history.jsonl``
    with trigger ``"set"``) and reloads all four desktop consumers.
    Composition-root-only orchestration: no application-layer
    orchestrator merges the two use cases. Both passes are wired with
    the SAME adapters and mutex as ``_run_seed_if_needed`` /
    ``_run_reconcile`` — the mutex is the same flock file the seeder
    uses, held sequentially by apply (load→save) then reconcile
    (load→repoint→save), matching the AD-12 pipeline.
    """
    state_root = _resolve_state_root()
    install_spine = _resolve_install_spine()

    from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
    from runtime.adapters.csg_adapter import CsgAdapter
    from runtime.adapters.flock_seed_mutex import FlockSeedMutex
    from runtime.adapters.hyprland_monitor_source import HyprlandMonitorSource
    from runtime.adapters.itr_adapter import ItrAdapter
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.seeder import CacheSeeder
    from runtime.adapters.weg_adapter import WegAdapter
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    apply_result = ApplyWallpaperUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        csg=CsgAdapter(),
        weg=WegAdapter(),
        itr=ItrAdapter(),
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root, consumer_spec=StaticConsumerPathSpec()),
        mutex=FlockSeedMutex(state_root / ".seed.lock"),
        monitor_source=HyprlandMonitorSource(),
    ).run(image_path)

    reconcile_result = ReconcileDesktopStateUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        csg=CsgAdapter(),
        weg=WegAdapter(),
        itr=ItrAdapter(),
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root, consumer_spec=StaticConsumerPathSpec()),
        mutex=FlockSeedMutex(state_root / ".seed.lock"),
        reloaders=_build_reloaders(state_root),
    ).run(trigger="set")

    return _WallpaperSetResult(apply=apply_result, reconcile=reconcile_result)


@app.command(help="Show the installed package version")
def version(
    output_format: OutputFormat = typer.Option(OutputFormat.PLAIN, "--format", "-f"),
) -> None:
    renderer = create_renderer(output_format)
    try:
        ver = _pkg_version("dotfiles-runtime")
    except PackageNotFoundError:
        renderer.error(
            ErrorView(
                kind="PackageNotFoundError",
                message="dotfiles-runtime package not installed",
            )
        )
        raise typer.Exit(code=1) from None

    renderer.custom(CustomView(plain=ver, object={"version": ver}, rich=ver))


_IMAGE_PATH_ARG = typer.Argument(help="Path to the wallpaper image file")
_OUTPUT_FORMAT_OPTION = typer.Option(OutputFormat.PLAIN, "--format", "-f")
_HISTORY_LIMIT_OPTION = typer.Option(
    20, "--limit", "-n", help="Maximum history entries to show (newest first, 0 = all)"
)


@wallpaper_app.command("set")
def wallpaper_set(
    image_path: Path = _IMAGE_PATH_ARG,
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
) -> None:
    """Derive, cache, swap, reload, and persist state for a wallpaper.

    The full end-to-end pipeline in one synchronous command (AD-12):
    apply derives the palette/effects/icons layers, ensures cache
    entries (zero tool invocations on cache hits), and writes
    ``current.json``; the chained reconcile then repoints the
    ``current/`` symlinks atomically, appends a ``history.jsonl`` line
    (trigger ``"set"``), and reloads all four desktop consumers
    (Hyprland, AGS, Hyprpaper, terminal palette). Reload failures are
    surfaced per consumer and exit non-zero (R5).
    """
    renderer = create_renderer(output_format)
    try:
        result = _run_wallpaper_set(image_path)
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("wallpaper set failed: %s", exc)
        renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
        raise typer.Exit(code=1) from None
    except Exception:
        logger.exception("wallpaper set failed unexpectedly")
        renderer.error(
            ErrorView(
                kind="UnexpectedError",
                message="wallpaper set failed unexpectedly; see logs",
            )
        )
        raise typer.Exit(code=1) from None

    # Reload result (contract step 5): the wired reloaders populate
    # ReconcileResult.reload_failures; a non-empty list is surfaced and
    # exits non-zero per R5 (no daemon retry) — the exact pattern the
    # reconcile command uses.
    if result.reconcile.reload_failures:
        failed = ", ".join(result.reconcile.reload_failures)
        logger.error("wallpaper set: reload failed for %s", failed)
        renderer.error(
            ErrorView(
                kind="ReloadError",
                message=f"reload failed for: {failed}",
            )
        )
        raise typer.Exit(code=1) from None

    # Render the AUTHORITATIVE post-swap state (what reconcile actually
    # repointed/persisted), not apply's pre-swap snapshot: reconcile can
    # regenerate/degrade layers or converge a concurrent write, so
    # apply-pass descriptors are kept only for the cache-hit flags.
    state = result.reconcile.state
    palette_desc = "cache hit" if result.apply.cache_hit_palette else "generated"
    effects_desc = (
        "cache hit"
        if result.apply.cache_hit_effects
        else ("generated" if result.apply.effects else "unavailable")
    )
    icons_desc = (
        "cache hit"
        if result.apply.cache_hit_icons
        else ("generated" if result.apply.icons else "unavailable")
    )
    summary = (
        f"wallpaper applied: {state.wallpaper.content_hash[:12]}"
        f" (palette {palette_desc}, effects {effects_desc}, icons {icons_desc})"
        f", {len(result.reconcile.repointed)} symlink(s) repointed"
        + (
            f", {len(result.reconcile.consumer_symlinks)} consumer link(s)"
            if result.reconcile.consumer_symlinks
            else ""
        )
    )
    obj: dict[str, object] = {
        "wallpaper": state.wallpaper.content_hash,
        "palette": state.palette.entry_hash if state.palette else None,
        "effects": state.effects.entry_hash if state.effects else None,
        "icons": state.icons.entry_hash if state.icons else None,
        "cache_hits": {
            "palette": result.apply.cache_hit_palette,
            "effects": result.apply.cache_hit_effects,
            "icons": result.apply.cache_hit_icons,
        },
        "repointed": [str(p) for p in result.reconcile.repointed],
        "consumer_symlinks": [str(p) for p in result.reconcile.consumer_symlinks],
        "skipped": list(result.reconcile.skipped),
        "cache_regenerated": list(result.reconcile.cache_regenerated),
        "reload_failures": list(result.reconcile.reload_failures),
        "inputs_stale": [],
        "inputs_fresh": [],
        "inputs_check_error": None,
    }
    summary = _append_inputs_check(summary, obj)
    renderer.custom(CustomView(plain=summary, object=obj, rich=summary))


def _append_inputs_check(summary: str, obj: dict[str, object]) -> str:
    """Run the invalidation check and append its result (Story 4.7).

    Informational only (AC 4): a stale input never changes the exit code —
    the reload gate already owns set failure — and a check failure is a
    warning line, never swallowed and never promoted to a set failure.
    """
    try:
        check = _run_check_inputs()
    except (ValueError, RuntimeError, OSError) as exc:
        logger.warning("wallpaper set: inputs check failed: %s", exc)
        obj["inputs_check_error"] = str(exc)
        return summary + f"\ninputs check failed: {exc}"
    except Exception:
        logger.warning("wallpaper set: inputs check failed unexpectedly", exc_info=True)
        obj["inputs_check_error"] = "unexpected failure; see logs"
        return summary + "\ninputs check failed unexpectedly; see logs"
    stale = sorted(check.stale)
    fresh = sorted(check.fresh)
    obj["inputs_stale"] = stale
    obj["inputs_fresh"] = fresh
    if stale:
        fresh_desc = ", ".join(fresh) if fresh else "none"
        return summary + f"\ninputs stale: {', '.join(stale)} (fresh: {fresh_desc})"
    return summary + "\ninputs: all layers fresh"


def _run_reconcile() -> ReconcileResult:
    """Compose and run ReconcileDesktopStateUseCase (reconcile command).

    Mirrors ``_run_wallpaper_set``'s wiring: resolve state_root /
    install_spine (both absolute), construct the JSON state repository,
    the CSG/WEG/ITR adapters, the ``CacheSeeder``, and the state mutex,
    then inject all of them into ``ReconcileDesktopStateUseCase``. The
    mutex is the same flock file the seeder and apply use: reconcile
    serializes against concurrent ``wallpaper set`` (Story 1.13 review,
    D1 decision).
    """
    state_root = _resolve_state_root()
    install_spine = _resolve_install_spine()

    from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
    from runtime.adapters.csg_adapter import CsgAdapter
    from runtime.adapters.flock_seed_mutex import FlockSeedMutex
    from runtime.adapters.itr_adapter import ItrAdapter
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.seeder import CacheSeeder
    from runtime.adapters.weg_adapter import WegAdapter
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    use_case = ReconcileDesktopStateUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        csg=CsgAdapter(),
        weg=WegAdapter(),
        itr=ItrAdapter(),
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root, consumer_spec=StaticConsumerPathSpec()),
        mutex=FlockSeedMutex(state_root / ".seed.lock"),
        reloaders=_build_reloaders(state_root),
    )
    return use_case.run()


def _run_check_inputs() -> CheckInputsResult:
    """Compose and run CheckInputsUseCase (reconcile --check-inputs).

    Read-only wiring (Story 1.3): resolve state_root / install_spine (both
    absolute), construct the JSON state repository, resolve the four spine
    inputs via ``derive.find_*`` (composition owns discovery per the Story 1.2
    seam), build the ``InvalidationQueryAdapter``, and inject the repository,
    the port, and the adapter's ``recorded_inputs`` reader into
    ``CheckInputsUseCase``. No seeder, mutex, derivation adapters, or
    reloaders — inspection mutates nothing and invokes no tools.
    """
    state_root = _resolve_state_root()
    install_spine = _resolve_install_spine()

    from runtime.adapters.invalidation import InvalidationQueryAdapter
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.application.check_inputs import CheckInputsUseCase
    from runtime.application.derive import (
        find_effects_catalog,
        find_icon_mappings,
        find_icon_templates,
        find_templates_dir,
    )

    invalidation = InvalidationQueryAdapter(
        state_root=state_root,
        templates_dir=find_templates_dir(install_spine),
        catalog_path=find_effects_catalog(install_spine),
        icon_templates=find_icon_templates(install_spine),
        icon_mappings=find_icon_mappings(install_spine),
    )
    use_case = CheckInputsUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        invalidation=invalidation,
        recorded_inputs=invalidation.recorded_inputs,
    )
    return use_case.run()


@dataclass(frozen=True, slots=True)
class _ReconcilePlanResult:
    """Combined outcome of the ``reconcile --plan`` read-only preview.

    Keeps the stale set from ``CheckInputsUseCase`` alongside the removal
    plan from ``PruneUseCase`` so the CLI renders both coherently. Pure
    composition-root data — no application-layer unit backs it (the Phase 4
    diff engine will replace these internals behind this same surface).
    """

    stale: frozenset[str]
    fresh: frozenset[str]
    removals: dict[str, tuple[str, ...]]
    kept: dict[str, int]
    total_removable: int
    keep: int
    prune_pinned: bool


@dataclass(frozen=True, slots=True)
class _DeclarativePlanResult:
    """Declarative ``reconcile --plan`` preview (Story 4.5).

    The real gap (``ChangeSet``) plus the stale/fresh continuity rows and the
    AD-30 prunable set. Pure composition-root data like
    :class:`_ReconcilePlanResult`.
    """

    stale: frozenset[str]
    fresh: frozenset[str]
    changeset: ChangeSet
    prunable: dict[str, tuple[str, ...]]
    total_removable: int
    compute_keep: int
    current_keep: int
    prune_pinned: bool


def _run_reconcile_plan(
    keep: int | None, prune_pinned: bool
) -> _ReconcilePlanResult | _DeclarativePlanResult:
    """Compose and run the ``reconcile --plan`` read-only preview.

    Composes the two existing read-only use cases (precedent:
    ``_run_wallpaper_set`` composes apply + reconcile): ``CheckInputsUseCase``
    for the stale set, ``PruneUseCase`` for the removal set. No seeder,
    mutex (no lock — read-only like dry-run), derivation adapters,
    reloaders, or history appends — the preview mutates nothing and invokes
    no tools.

    With a ``desired.json`` present (Story 4.5), the declarative branch
    engages: the same adapter callables feed ``build_actual_state`` and the
    ``ChangeSet`` diff. Absent file → legacy imperative output untouched.
    Malformed file → ``ValueError`` (loud, never silent fallback).
    """
    state_root = _resolve_state_root()
    install_spine = _resolve_install_spine()

    from runtime.adapters.desired_state_reader import read_desired_state
    from runtime.adapters.invalidation import InvalidationQueryAdapter
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.prune_source import entries_for, seed_pins
    from runtime.application.check_inputs import CheckInputsUseCase
    from runtime.application.derive import (
        find_effects_catalog,
        find_icon_mappings,
        find_icon_templates,
        find_templates_dir,
    )
    from runtime.application.prune import PruneUseCase

    state_repo = JsonStateRepository(state_root=state_root)
    # Fail fast on malformed intent: before any invalidation work, loud
    # ValueError on bad file (AC 4) — never silent fallback to imperative.
    desired = read_desired_state(state_root)
    invalidation = InvalidationQueryAdapter(
        state_root=state_root,
        templates_dir=find_templates_dir(install_spine),
        catalog_path=find_effects_catalog(install_spine),
        icon_templates=find_icon_templates(install_spine),
        icon_mappings=find_icon_mappings(install_spine),
    )
    check_result = CheckInputsUseCase(
        state_repo=state_repo,
        invalidation=invalidation,
        recorded_inputs=invalidation.recorded_inputs,
    ).run()
    if desired is None:
        resolved_keep = keep if keep is not None else 5
        plan = PruneUseCase(
            state_repo=state_repo,
            entries_for=lambda layer: entries_for(state_root, layer),
            seed_pins=lambda: seed_pins(state_root),
            keep=resolved_keep,
        ).run(prune_pinned=prune_pinned)
        return _ReconcilePlanResult(
            stale=frozenset(check_result.stale),
            fresh=frozenset(check_result.fresh),
            removals=dict(plan.removals),
            kept=dict(plan.kept),
            total_removable=plan.total_removable,
            keep=resolved_keep,
            prune_pinned=prune_pinned,
        )
    # Declarative branch (AD-30 precedence: code default < desired < CLI flag).
    if keep is not None:
        desired = replace(desired, keep=keep)
    compute_keep = keep if keep is not None else desired.keep
    current_keep = keep if keep is not None else 5
    from runtime.application.actual_state import build_actual_state
    from runtime.application.diff import diff_states

    actual = build_actual_state(
        state_repo.load_current(),
        lambda layer: entries_for(state_root, layer),
        lambda: seed_pins(state_root),
        compute_keep,
        prune_pinned,
    )
    changeset = diff_states(desired, actual, current_keep)
    prunable = dict(actual.prunable_hashes)
    return _DeclarativePlanResult(
        stale=frozenset(check_result.stale),
        fresh=frozenset(check_result.fresh),
        changeset=changeset,
        prunable=prunable,
        total_removable=sum(len(hashes) for hashes in prunable.values()),
        compute_keep=compute_keep,
        current_keep=current_keep,
        prune_pinned=prune_pinned,
    )


def _run_regenerate_stale() -> RegenerateResult:
    """Compose and run RegenerateStaleUseCase (reconcile --regenerate-stale).

    Mirrors ``_run_wallpaper_set``'s construction: resolve state_root /
    install_spine (both absolute), build one shared set of adapters
    (``CacheSeeder``, CSG/WEG/ITR, ``FlockSeedMutex``), resolve the four spine
    inputs via ``derive.find_*`` (composition owns discovery), then wire
    ``CheckInputsUseCase`` + ``DerivationPipeline`` + ``ReconcileDesktopState-
    UseCase`` (with ``_build_reloaders``) into ``RegenerateStaleUseCase``.
    Shared instances (not duplicated per use case) so locking and staging
    behave as one pipeline.
    """
    state_root = _resolve_state_root()
    install_spine = _resolve_install_spine()

    from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
    from runtime.adapters.csg_adapter import CsgAdapter
    from runtime.adapters.flock_seed_mutex import FlockSeedMutex
    from runtime.adapters.invalidation import InvalidationQueryAdapter
    from runtime.adapters.itr_adapter import ItrAdapter
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.seeder import CacheSeeder
    from runtime.adapters.weg_adapter import WegAdapter
    from runtime.application.check_inputs import CheckInputsUseCase
    from runtime.application.derive import (
        DerivationPipeline,
        find_effects_catalog,
        find_icon_mappings,
        find_icon_templates,
        find_templates_dir,
    )
    from runtime.application.reconcile import ReconcileDesktopStateUseCase
    from runtime.application.regenerate import RegenerateStaleUseCase

    state_repo = JsonStateRepository(state_root=state_root)
    seeder = CacheSeeder(state_root, consumer_spec=StaticConsumerPathSpec())
    mutex = FlockSeedMutex(state_root / ".seed.lock")
    csg, weg, itr = CsgAdapter(), WegAdapter(), ItrAdapter()
    invalidation = InvalidationQueryAdapter(
        state_root=state_root,
        templates_dir=find_templates_dir(install_spine),
        catalog_path=find_effects_catalog(install_spine),
        icon_templates=find_icon_templates(install_spine),
        icon_mappings=find_icon_mappings(install_spine),
    )
    use_case = RegenerateStaleUseCase(
        check=CheckInputsUseCase(
            state_repo=state_repo,
            invalidation=invalidation,
            recorded_inputs=invalidation.recorded_inputs,
        ),
        pipeline=DerivationPipeline(
            state_root=state_root,
            seeder=seeder,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
        ),
        state_repo=state_repo,
        reconcile=ReconcileDesktopStateUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=mutex,
            reloaders=_build_reloaders(state_root),
        ),
        mutex=mutex,
        state_root=state_root,
    )
    return use_case.run()


def _run_doctor_check() -> DoctorReport:
    """Compose and run DoctorUseCase.check (doctor command).

    Read-only wiring (Story 2.1): resolve state_root (absolute), construct
    the JSON state repository, inject both into ``DoctorUseCase``. No seeder,
    mutex, derivation adapters, or reloaders — the check mutates nothing.
    Shaped forward-compatibly: Story 2.2 adds ``--repair`` to the same
    command and a ``repair()`` step on the same use case.
    """
    state_root = _resolve_state_root()

    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.application.doctor import DoctorUseCase

    use_case = DoctorUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
    )
    return use_case.check()


def _run_doctor_repair() -> RepairResult:
    """Compose and run DoctorRepairUseCase.repair (doctor --repair).

    Mutation wiring (Story 2.2), sharing ONE set of adapters with the
    composed reconcile (mirrors ``_run_regenerate_stale``): resolve
    state_root / install_spine (absolute), build the JSON repository, the
    seeder, the flock mutex, the CSG/WEG/ITR adapters, the derivation
    pipeline, and the reconcile use case (with ``_build_reloaders``), then
    inject the read-only ``DoctorUseCase``, the ``quarantine_entry`` seam,
    and reconcile into ``DoctorRepairUseCase``.
    """
    state_root = _resolve_state_root()
    install_spine = _resolve_install_spine()

    from runtime.adapters.cache import quarantine_entry
    from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
    from runtime.adapters.csg_adapter import CsgAdapter
    from runtime.adapters.flock_seed_mutex import FlockSeedMutex
    from runtime.adapters.itr_adapter import ItrAdapter
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.seeder import CacheSeeder
    from runtime.adapters.weg_adapter import WegAdapter
    from runtime.application.doctor import DoctorRepairUseCase, DoctorUseCase
    from runtime.application.reconcile import ReconcileDesktopStateUseCase

    state_repo = JsonStateRepository(state_root=state_root)
    seeder = CacheSeeder(state_root, consumer_spec=StaticConsumerPathSpec())
    mutex = FlockSeedMutex(state_root / ".seed.lock")
    csg, weg, itr = CsgAdapter(), WegAdapter(), ItrAdapter()
    use_case = DoctorRepairUseCase(
        doctor=DoctorUseCase(state_repo=state_repo, state_root=state_root),
        quarantine=lambda layer, entry_hash: quarantine_entry(state_root, layer, entry_hash),
        reconcile=ReconcileDesktopStateUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            install_spine=install_spine,
            state_root=state_root,
            seeder=seeder,
            mutex=mutex,
            reloaders=_build_reloaders(state_root),
        ),
        state_root=state_root,
        heal_history_tail=seeder.heal_torn_history_tail,
    )
    return use_case.repair()


@app.command(help="Check current.json vs current/ symlinks vs cache entries for drift")
def doctor(
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
    repair: bool = typer.Option(
        False,
        "--repair",
        help="Quarantine bad entries and reconverge the desktop (mutating)",
    ),
) -> None:
    """Report store/symlink/cache divergence; optionally repair it.

    Read-only drift detection (FR-3, CAP-3) by default: classifies every
    checked item ok/missing/diverged/dangling and exits non-zero on drift.
    With ``--repair`` (FR-4): quarantine bad entries by rename-aside and
    reconverge via reconcile (repopulate/repoint/save/history ``doctor``/reload),
    idempotent. Mutates nothing without the flag.
    """
    renderer = create_renderer(output_format)
    if repair:
        try:
            repair_result = _run_doctor_repair()
        except (ValueError, RuntimeError, OSError) as exc:
            logger.error("doctor --repair failed: %s", exc)
            renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
            raise typer.Exit(code=1) from None
        except Exception:
            logger.exception("doctor --repair failed unexpectedly")
            renderer.error(
                ErrorView(
                    kind="UnexpectedError",
                    message="doctor --repair failed unexpectedly; see logs",
                )
            )
            raise typer.Exit(code=1) from None

        if repair_result.reload_failures:
            failed = ", ".join(repair_result.reload_failures)
            logger.error("doctor --repair: reload failed for %s", failed)
            renderer.error(ErrorView(kind="ReloadError", message=f"reload failed for: {failed}"))
            raise typer.Exit(code=1) from None
        if repair_result.history_trigger is None and repair_result.history_tail_quarantined is None:
            plain = "already clean: nothing to repair"
        elif repair_result.history_trigger is None:
            plain = "repaired: healed torn history tail"
        else:
            repopulated = ", ".join(repair_result.repopulated) or "none"
            tail_note = (
                ", history tail healed"
                if repair_result.history_tail_quarantined is not None
                else ""
            )
            plain = (
                f"repaired: quarantined {len(repair_result.quarantined)}, "
                f"regenerated {repopulated}{tail_note}"
            )
        renderer.custom(
            CustomView(
                plain=plain,
                object={
                    "quarantined": [str(p) for p in repair_result.quarantined],
                    "repopulated": list(repair_result.repopulated),
                    "history_trigger": repair_result.history_trigger,
                    "history_tail_quarantined": (
                        str(repair_result.history_tail_quarantined)
                        if repair_result.history_tail_quarantined is not None
                        else None
                    ),
                    "reload_failures": list(repair_result.reload_failures),
                },
                rich=plain,
            )
        )
        return
    try:
        report = _run_doctor_check()
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("doctor failed: %s", exc)
        renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
        raise typer.Exit(code=1) from None
    except Exception:
        logger.exception("doctor failed unexpectedly")
        renderer.error(
            ErrorView(
                kind="UnexpectedError",
                message="doctor failed unexpectedly; see logs",
            )
        )
        raise typer.Exit(code=1) from None

    if report.clean:
        plain = f"desktop clean: {len(report.items)} item(s) checked"
    else:
        counts: dict[str, int] = {}
        for item in report.items:
            if item.status != "ok":
                counts[item.status] = counts.get(item.status, 0) + 1
        breakdown = ", ".join(f"{status}: {n}" for status, n in sorted(counts.items()))
        plain = f"drift detected: {breakdown}"
    renderer.custom(
        CustomView(
            plain=plain,
            object={
                "clean": report.clean,
                "items": [
                    {
                        "name": item.name,
                        "kind": item.kind,
                        "status": item.status,
                        "detail": item.detail,
                    }
                    for item in report.items
                ],
            },
            rich=plain,
        )
    )
    if not report.clean:
        raise typer.Exit(code=1) from None


def _render_imperative_plan(renderer: Renderer, plan_result: _ReconcilePlanResult) -> None:
    """Render the legacy imperative preview (byte-identical, AC 2)."""
    stale = sorted(plan_result.stale)
    fresh = sorted(plan_result.fresh)
    from runtime.application.prune import LAYERS

    removals = {layer: sorted(plan_result.removals.get(layer, ())) for layer in LAYERS}
    parts = []
    if stale:
        fresh_desc = ", ".join(fresh) if fresh else "none"
        parts.append(f"stale layers: {', '.join(stale)} (fresh: {fresh_desc})")
    else:
        parts.append("all layers fresh")
    if plan_result.total_removable:
        parts.append(f"reclaimable: {plan_result.total_removable}")
        for layer in LAYERS:
            if removals[layer]:
                parts.append(f"{layer} ({len(removals[layer])}):")
                parts.extend(f"  {h[:12]}" for h in removals[layer])
    else:
        parts.append("nothing reclaimable")
    plain = "\n".join(parts)
    renderer.custom(
        CustomView(
            plain=plain,
            object={
                "stale": stale,
                "fresh": fresh,
                "removals": removals,
                "kept": dict(plan_result.kept),
                "total_removable": plan_result.total_removable,
                "keep": plan_result.keep,
                "prune_pinned": plan_result.prune_pinned,
            },
            rich=plain,
        )
    )


def _render_declarative_plan(renderer: Renderer, result: _DeclarativePlanResult) -> None:
    """Render the ChangeSet gap (AC 1). Pins-absent rows are informational."""
    from runtime.application.prune import LAYERS

    changeset = result.changeset
    stale = sorted(result.stale)
    fresh = sorted(result.fresh)
    parts = ["declarative gap vs desired.json:"]
    if changeset.wallpaper_target is None:
        parts.append("wallpaper: converged")
    else:
        parts.append(f"wallpaper -> {changeset.wallpaper_target}")
    if changeset.pins_to_add:
        parts.append(f"pins to add ({len(changeset.pins_to_add)}):")
        parts.extend(f"  {h[:12]}" for h in changeset.pins_to_add)
    else:
        parts.append("pins to add: none")
    if changeset.pins_absent:
        parts.append(f"pins absent (protected, informational) ({len(changeset.pins_absent)}):")
        parts.extend(f"  {h[:12]}" for h in changeset.pins_absent)
    if changeset.keep_target is None:
        parts.append(f"keep: {result.current_keep} (converged)")
    else:
        parts.append(f"keep: {result.current_keep} -> {changeset.keep_target}")
    if result.total_removable:
        parts.append(
            f"prunable under AD-30 floor (keep={result.compute_keep}): {result.total_removable}"
        )
        for layer in LAYERS:
            hashes = sorted(result.prunable.get(layer, ()))
            if hashes:
                parts.append(f"{layer} ({len(hashes)}):")
                parts.extend(f"  {h[:12]}" for h in hashes)
    else:
        parts.append("nothing prunable")
    if stale:
        fresh_desc = ", ".join(fresh) if fresh else "none"
        parts.append(f"stale layers: {', '.join(stale)} (fresh: {fresh_desc})")
    else:
        parts.append("all layers fresh")
    if changeset.is_empty and not result.total_removable:
        parts.append("already converged: desired state matches actual")
    plain = "\n".join(parts)
    renderer.custom(
        CustomView(
            plain=plain,
            object={
                "declarative": True,
                "wallpaper_target": changeset.wallpaper_target,
                "pins_to_add": list(changeset.pins_to_add),
                "pins_absent": list(changeset.pins_absent),
                "keep_target": changeset.keep_target,
                "is_empty": changeset.is_empty,
                "prunable": {layer: sorted(result.prunable.get(layer, ())) for layer in LAYERS},
                "total_removable": result.total_removable,
                "compute_keep": result.compute_keep,
                "prune_pinned": result.prune_pinned,
                "stale": stale,
                "fresh": fresh,
            },
            rich=plain,
        )
    )


@app.command(help="Repoint current/ symlinks to converge the desktop with current.json")
def reconcile(
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
    check_inputs: bool = typer.Option(
        False,
        "--check-inputs",
        help="Read-only: report stale derivation layers without mutating anything",
    ),
    regenerate_stale: bool = typer.Option(
        False,
        "--regenerate-stale",
        help="Regenerate stale layers (+ cascade) and reconverge the desktop",
    ),
    plan: bool = typer.Option(
        False,
        "--plan",
        help="Read-only: preview stale layers + prune removal plan without mutating anything",
    ),
    keep: int | None = typer.Option(
        None,
        "--keep",
        min=0,
        help="Keep N recent entries per layer (explicit flag overrides desired.json keep)",
    ),
    prune_pinned: bool = typer.Option(
        False, "--prune-pinned", help="Include seed-pinned entries in the plan/prune removal set"
    ),
) -> None:
    """Repoint current/ symlinks to match current.json (swap sequence steps 1-4).

    Ensures every cache entry referenced by current.json exists (regenerating
    on miss), repoints current/ symlinks atomically, saves refreshed
    current.json, and appends a history.jsonl line with trigger "reconcile".

    Desktop reload (contract step 5) restarts Hyprland, restarts AGS,
    applies the Hyprpaper wallpaper IPC per monitor, and applies the
    terminal palette via OSC sequences via the reloaders wired in the
    composition root.
    """
    renderer = create_renderer(output_format)
    if (keep is not None or prune_pinned) and not plan:
        logger.error("reconcile: --keep/--prune-pinned require --plan")
        renderer.error(
            ErrorView(
                kind="MutuallyExclusiveOptions",
                message="--keep and --prune-pinned require --plan",
            )
        )
        raise typer.Exit(code=2) from None
    modes = [check_inputs, regenerate_stale, plan]
    if sum(1 for mode in modes if mode) > 1:
        logger.error(
            "reconcile: --check-inputs, --regenerate-stale, and --plan are mutually exclusive"
        )
        renderer.error(
            ErrorView(
                kind="MutuallyExclusiveOptions",
                message="--check-inputs (read), --regenerate-stale (mutate), and "
                "--plan (preview) are mutually exclusive",
            )
        )
        raise typer.Exit(code=2) from None
    if regenerate_stale:
        try:
            regen_result = _run_regenerate_stale()
        except (ValueError, RuntimeError, OSError) as exc:
            logger.error("reconcile --regenerate-stale failed: %s", exc)
            renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
            raise typer.Exit(code=1) from None
        except Exception:
            logger.exception("reconcile --regenerate-stale failed unexpectedly")
            renderer.error(
                ErrorView(
                    kind="UnexpectedError",
                    message="reconcile --regenerate-stale failed unexpectedly; see logs",
                )
            )
            raise typer.Exit(code=1) from None

        if regen_result.reload_failures:
            failed = ", ".join(regen_result.reload_failures)
            logger.error("reconcile --regenerate-stale: reload failed for %s", failed)
            renderer.error(ErrorView(kind="ReloadError", message=f"reload failed for: {failed}"))
            raise typer.Exit(code=1) from None
        regenerated = sorted(regen_result.regenerated)
        if regenerated:
            plain = f"regenerated: {', '.join(regenerated)}"
        else:
            plain = "already converged: all layers fresh"
        state = regen_result.state
        renderer.custom(
            CustomView(
                plain=plain,
                object={
                    "regenerated": regenerated,
                    "repointed": [str(p) for p in regen_result.repointed],
                    "reload_failures": list(regen_result.reload_failures),
                    "wallpaper": state.wallpaper.content_hash,
                    "palette": state.palette.entry_hash if state.palette else None,
                    "effects": state.effects.entry_hash if state.effects else None,
                    "icons": state.icons.entry_hash if state.icons else None,
                },
                rich=plain,
            )
        )
        return
    if plan:
        try:
            plan_result = _run_reconcile_plan(keep, prune_pinned)
        except (ValueError, RuntimeError, OSError) as exc:
            logger.error("reconcile --plan failed: %s", exc)
            renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
            raise typer.Exit(code=1) from None
        except Exception:
            logger.exception("reconcile --plan failed unexpectedly")
            renderer.error(
                ErrorView(
                    kind="UnexpectedError",
                    message="reconcile --plan failed unexpectedly; see logs",
                )
            )
            raise typer.Exit(code=1) from None
        if isinstance(plan_result, _DeclarativePlanResult):
            _render_declarative_plan(renderer, plan_result)
        else:
            _render_imperative_plan(renderer, plan_result)
        return
    if check_inputs:
        try:
            check_result = _run_check_inputs()
        except (ValueError, RuntimeError, OSError) as exc:
            logger.error("reconcile --check-inputs failed: %s", exc)
            renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
            raise typer.Exit(code=1) from None
        except Exception:
            logger.exception("reconcile --check-inputs failed unexpectedly")
            renderer.error(
                ErrorView(
                    kind="UnexpectedError",
                    message="reconcile --check-inputs failed unexpectedly; see logs",
                )
            )
            raise typer.Exit(code=1) from None

        stale = sorted(check_result.stale)
        fresh = sorted(check_result.fresh)
        if stale:
            fresh_desc = ", ".join(fresh) if fresh else "none"
            plain = f"stale layers: {', '.join(stale)} (fresh: {fresh_desc})"
        else:
            plain = "all layers fresh"
        renderer.custom(
            CustomView(
                plain=plain,
                object={"stale": stale, "fresh": fresh},
                rich=plain,
            )
        )
        return
    try:
        from runtime.adapters.desired_state_reader import read_desired_state

        # Pre-validate intent before ANY mutation (AC 4): a malformed file
        # must fail here, not after repoint + history append.
        read_desired_state(_resolve_state_root())
        result = _run_reconcile()
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("reconcile failed: %s", exc)
        renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
        raise typer.Exit(code=1) from None
    except Exception:
        logger.exception("reconcile failed unexpectedly")
        renderer.error(
            ErrorView(
                kind="UnexpectedError",
                message="reconcile failed unexpectedly; see logs",
            )
        )
        raise typer.Exit(code=1) from None

    # Reload result (contract step 5): the wired reloaders populate
    # ReconcileResult.reload_failures; a non-empty list is surfaced and
    # exits non-zero per AC 4 (no daemon retry).
    if result.reload_failures:
        failed = ", ".join(result.reload_failures)
        logger.error("reconcile: reload failed for %s", failed)
        renderer.error(
            ErrorView(
                kind="ReloadError",
                message=f"reload failed for: {failed}",
            )
        )
        raise typer.Exit(code=1) from None
    # Skipped entries: the reconcile use case populates skipped with entries
    # containing "reload" or "consumer" — CLI already renders skipped.

    summary = (
        f"desktop reconciled: {len(result.repointed)} symlink(s) repointed"
        + (f", {len(result.skipped)} skipped" if result.skipped else "")
        + (
            f", regenerated: {', '.join(result.cache_regenerated)}"
            if result.cache_regenerated
            else ""
        )
        + (
            f", {len(result.consumer_symlinks)} consumer link(s)"
            if result.consumer_symlinks
            else ""
        )
    )
    obj: dict[str, object] = {
        "repointed": [str(p) for p in result.repointed],
        "consumer_symlinks": [str(p) for p in result.consumer_symlinks],
        "skipped": list(result.skipped),
        "cache_regenerated": list(result.cache_regenerated),
        "reload_failures": list(result.reload_failures),
    }
    try:
        converge_report = _run_converge()
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("reconcile succeeded, converge failed: %s", exc)
        renderer.error(
            ErrorView(
                kind=type(exc).__name__,
                message=f"reconcile succeeded, converge failed: {exc}",
            )
        )
        raise typer.Exit(code=1) from None
    except Exception:
        logger.exception("reconcile converge failed unexpectedly")
        renderer.error(
            ErrorView(
                kind="UnexpectedError",
                message="reconcile converge failed unexpectedly; see logs",
            )
        )
        raise typer.Exit(code=1) from None
    if converge_report is not None:
        summary, obj = _render_converge(summary, obj, converge_report)
    renderer.custom(
        CustomView(
            plain=summary,
            object=obj,
            rich=summary,
        )
    )


def _render_converge(
    summary: str, obj: dict[str, object], report: ConvergenceReport
) -> tuple[str, dict[str, object]]:
    """Append declarative convergence lines (Story 4.5, AC 3)."""
    deleted_total = sum(len(hashes) for hashes in report.deleted.values())
    if report.wallpaper_set is None and not deleted_total and not report.pins_pending:
        return summary + "\ndeclarative state: already converged", {
            **obj,
            "converge": "already-converged",
        }
    if report.wallpaper_set is not None:
        summary += f"\nconverged wallpaper: {report.wallpaper_set}"
    if deleted_total:
        summary += f"\npruned under AD-30 floor: {deleted_total}"
        for layer in sorted(report.deleted):
            if report.deleted[layer]:
                summary += f"\n  {layer}: {len(report.deleted[layer])}"
    if report.pins_pending:
        summary += f"\npins pending (manual): {len(report.pins_pending)}"
    if report.keep_target is not None:
        summary += f"\nkeep policy: {report.keep_target} (in force)"
    return summary, {
        **obj,
        "converge_wallpaper": report.wallpaper_set,
        "converge_deleted": {layer: list(hashes) for layer, hashes in report.deleted.items()},
        "converge_pins_pending": list(report.pins_pending),
        "converge_keep_target": report.keep_target,
    }


def _run_converge() -> ConvergenceReport | None:
    """Execute the declarative gap for plain ``reconcile`` (Story 4.5, AC 3).

    Returns ``None`` when no ``desired.json`` is present (imperative mode
    untouched). Malformed file → ``ValueError`` (loud, AC 4). No prompt
    (B2): wallpaper target converges through the existing
    ``_run_wallpaper_set`` pipeline, then AD-30 deletes run through the
    ``remove_entry`` seam. ``pins_absent`` never triggers removal.
    """
    from pathlib import Path

    from runtime.adapters.desired_state_reader import read_desired_state
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.prune_source import entries_for, remove_entry, seed_pins
    from runtime.application.actual_state import build_actual_state
    from runtime.application.diff import diff_states
    from runtime.application.planner import ConvergeUseCase
    from runtime.domain.models import ActualState

    state_root = _resolve_state_root()
    desired = read_desired_state(state_root)
    if desired is None:
        return None
    state_repo = JsonStateRepository(state_root=state_root)

    def refresh_actual() -> ActualState:
        return build_actual_state(
            state_repo.load_current(),
            lambda layer: entries_for(state_root, layer),
            lambda: seed_pins(state_root),
            desired.keep,
            False,
        )

    changeset = diff_states(desired, refresh_actual(), 5)

    def set_wallpaper(target: str) -> object:
        res = _run_wallpaper_set(Path(target))
        if res.reconcile.reload_failures:
            raise RuntimeError(f"reload failed for: {', '.join(res.reconcile.reload_failures)}")
        return res

    return ConvergeUseCase(
        set_wallpaper,
        lambda layer, entry_hash: remove_entry(state_root, layer, entry_hash),
        refresh_actual,
    ).run(changeset)


def _run_inspect_status() -> InspectStatusResult:
    """Compose and run InspectStateUseCase (inspect status command).

    Mirrors ``_run_reconcile``'s wiring: resolve state_root (absolute),
    construct the JSON state repository, and inject both into the
    read-only use case. No seeder, mutex, derivation adapters, or
    reloaders — inspection mutates nothing (AC 4) and consumes no tools.
    """
    state_root = _resolve_state_root()

    from runtime.adapters.consumer_path_spec import StaticConsumerPathSpec
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.application.inspect import InspectStateUseCase

    use_case = InspectStateUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
        install_spine=_resolve_install_spine(),
        consumer_spec=StaticConsumerPathSpec(),
    )
    return use_case.run()


@inspect_app.command("status")
def inspect_status(
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
) -> None:
    """Show the current desktop state (wallpaper/palette/effects/icons).

    Read-only inspection (FR-7, CAP-7, NFR-3): projects ``current.json``
    and reflects the live ``current/`` consumer symlinks, flagging each
    as ok/missing/diverged/dangling (filesystem is the authority).
    Mutates nothing — no cache population, no symlink repoint, no
    history append, no seed side-effects. Exits non-zero when no state
    has been recorded yet (run ``dotfiles-provision apply`` or
    ``dotfiles-runtime wallpaper set <img>`` to seed).
    """
    renderer = create_renderer(output_format)
    try:
        result = _run_inspect_status()
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("inspect status failed: %s", exc)
        renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
        raise typer.Exit(code=1) from None
    except Exception:
        logger.exception("inspect status failed unexpectedly")
        renderer.error(
            ErrorView(
                kind="UnexpectedError",
                message="inspect status failed unexpectedly; see logs",
            )
        )
        raise typer.Exit(code=1) from None

    palette_desc = result.palette[:12] if result.palette is not None else "absent"
    effects_desc = result.effects[:12] if result.effects is not None else "absent"
    icons_desc = result.icons[:12] if result.icons is not None else "absent"
    summary = (
        f"desktop state: wallpaper {result.wallpaper[:12]}"
        f" (palette {palette_desc}, effects {effects_desc}, icons {icons_desc})"
        f", {len(result.current_symlinks)} symlink(s) checked"
    )
    renderer.custom(
        CustomView(
            plain=summary,
            object={
                "wallpaper": result.wallpaper,
                "wallpaper_source_path": result.wallpaper_source_path,
                "monitors": result.monitors,
                "palette": result.palette,
                "effects": result.effects,
                "icons": result.icons,
                "applied_at": result.applied_at,
                "current_symlinks": {
                    name: {"status": status.status, "target": status.target}
                    for name, status in result.current_symlinks.items()
                },
                "consumer_pointers": {
                    path: {"status": status.status, "target": status.target}
                    for path, status in result.consumer_pointers.items()
                },
            },
            rich=summary,
        )
    )


def _run_inspect_history(limit: int) -> tuple[list[HistoryRecord], int]:
    """Compose and run InspectHistoryUseCase (inspect history command).

    Mirrors ``_run_inspect_status``'s wiring: resolve state_root (absolute)
    and inject it into the read-only use case. No seeder, mutex, derivation
    adapters, reloaders, or state_repo — inspection mutates nothing (AC 4).

    Returns:
        A ``(entries, total)`` pair: ``entries`` newest-first bounded by
        ``limit`` (``0`` = all), ``total`` the parseable on-disk line count
        (torn trailing line excluded).
    """
    state_root = _resolve_state_root()

    from runtime.application.inspect import InspectHistoryUseCase

    use_case = InspectHistoryUseCase(state_root=state_root)
    # Single full scan: avoids 2x I/O and the inter-read TOCTOU where a
    # concurrent append makes entries/total inconsistent.
    all_entries = use_case.run(limit=0)
    total = len(all_entries)
    entries = all_entries if limit == 0 else all_entries[:limit]
    return entries, total


@inspect_app.command("history")
def inspect_history(
    limit: int = _HISTORY_LIMIT_OPTION,
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
) -> None:
    """Show the append-only desktop history, newest first (FR-7, CAP-7).

    Read-only inspection: reads ``history.jsonl`` (oldest-first on disk)
    and prints the transition log newest-first. Mutates nothing — no
    history append, no current.json write, no current/ repoint, no seed
    side-effects. An absent or empty history is clean (exit 0 with
    "no history recorded yet"), NOT an error.
    """
    renderer = create_renderer(output_format)
    try:
        entries, total = _run_inspect_history(limit)
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("inspect history failed: %s", exc)
        renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
        raise typer.Exit(code=1) from None
    except Exception:
        logger.exception("inspect history failed unexpectedly")
        renderer.error(
            ErrorView(
                kind="UnexpectedError",
                message="inspect history failed unexpectedly; see logs",
            )
        )
        raise typer.Exit(code=1) from None

    count = len(entries)
    truncated = total > count
    if count == 0:
        plain = "no history recorded yet"
    else:
        lines: list[str] = []
        for record in entries:
            palette_desc = record.palette[:12] if record.palette is not None else "absent"
            effects_desc = record.effects[:12] if record.effects is not None else "absent"
            icons_desc = record.icons[:12] if record.icons is not None else "absent"
            source_suffix = f" {record.source_path}" if record.source_path else ""
            lines.append(
                f"{record.ts} {record.trigger} {record.wallpaper[:12]}"
                f" palette {palette_desc} effects {effects_desc} icons {icons_desc}"
                f"{source_suffix}"
            )
        plain = "\n".join(lines)
    renderer.custom(
        CustomView(
            plain=plain,
            object={
                "entries": [record.to_dict() for record in entries],
                "count": count,
                "total": total,
                "truncated": truncated,
                "limit": limit,
            },
            rich=plain,
        )
    )


def _run_inspect_cache_list() -> InspectCacheResult:
    """Compose and run InspectCacheUseCase (inspect cache list command).

    Mirrors ``_run_inspect_status``'s wiring: resolve state_root (absolute)
    and inject it into the read-only use case. No seeder, mutex, derivation
    adapters, reloaders, or state_repo — inspection mutates nothing (AC 4).

    Returns:
        The single-scan ``InspectCacheResult`` (layers/counts/total from
        one directory walk, so ``total`` never needs a second read).
    """
    state_root = _resolve_state_root()

    from runtime.application.inspect import InspectCacheUseCase

    use_case = InspectCacheUseCase(state_root=state_root)
    return use_case.run()


def _run_verify_cache() -> VerifyCacheResult:
    """Compose and run VerifyCacheUseCase (inspect cache list --verify).

    Single-walk verify (Story 3.1): the lister does one directory scan; the
    ``verify_entry`` adapter seam (p3-3-1) is injected with ``annotate=True``
    so a legacy entry is lazily annotated on read (AD-27). Read-only except
    that sanctioned annotation.
    """
    state_root = _resolve_state_root()

    from runtime.adapters.cache import verify_entry
    from runtime.application.inspect import InspectCacheUseCase
    from runtime.application.verify_cache import VerifyCacheUseCase

    use_case = VerifyCacheUseCase(
        state_root=state_root,
        lister=InspectCacheUseCase(state_root=state_root),
        verify_entry=lambda entry_dir: verify_entry(entry_dir, annotate=True),
    )
    return use_case.run()


@cache_app.command("list")
def inspect_cache_list(
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
    verify: bool = typer.Option(
        False,
        "--verify",
        help="Re-hash each entry (ok/corrupt/missing); annotate legacy entries",
    ),
) -> None:
    """List cached derived artifacts per layer, by hash (FR-7, CAP-7).

    Read-only inspection: reads ``cache/<layer>/<hash>/`` dir names and
    prints each layer's entries in canonical pipeline order
    (wallpapers → palettes → effects → icons) with hashes sorted per
    layer. Mutates nothing — no cache population, no current.json write,
    no current/ repoint, no history append, no seed side-effects. An
    absent or empty cache is clean (exit 0 with
    "no cache entries recorded yet"), NOT an error. List-only: no
    eviction surface (AC 2).

    With ``--verify`` (Story 3.1): each entry carries an ``ok/corrupt/missing``
    verdict from the same single walk; legacy entries are annotated (AD-27);
    exits 1 when any entry is unhealthy.
    """
    renderer = create_renderer(output_format)
    if verify:
        try:
            verify_result = _run_verify_cache()
        except (ValueError, RuntimeError, OSError) as exc:
            logger.error("inspect cache list --verify failed: %s", exc)
            renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
            raise typer.Exit(code=1) from None
        except Exception:
            logger.exception("inspect cache list --verify failed unexpectedly")
            renderer.error(
                ErrorView(
                    kind="UnexpectedError",
                    message="inspect cache list --verify failed unexpectedly; see logs",
                )
            )
            raise typer.Exit(code=1) from None

        from runtime.application.inspect import CACHE_LAYER_ORDER

        if verify_result.total == 0:
            plain = "no cache entries recorded yet"
        else:
            sections = []
            for layer in CACHE_LAYER_ORDER:
                items = verify_result.layers[layer]
                lines = [f"{layer} ({len(items)}):"]
                lines.extend(
                    f"  {entry_hash[:12]} [{health.status}]" for entry_hash, health in items
                )
                sections.append("\n".join(lines))
            plain = "\n".join(sections)
            if verify_result.unhealthy:
                plain += f"\nunhealthy: {verify_result.unhealthy}"
        renderer.custom(
            CustomView(
                plain=plain,
                object={
                    "layers": {
                        layer: [
                            {
                                "hash": entry_hash,
                                "status": health.status,
                                "detail": health.detail,
                                "annotated": health.annotated,
                            }
                            for entry_hash, health in verify_result.layers[layer]
                        ]
                        for layer in CACHE_LAYER_ORDER
                    },
                    "counts": verify_result.counts,
                    "total": verify_result.total,
                    "unhealthy": verify_result.unhealthy,
                },
                rich=plain,
            )
        )
        if verify_result.unhealthy:
            raise typer.Exit(code=1) from None
        return
    try:
        result = _run_inspect_cache_list()
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("inspect cache list failed: %s", exc)
        renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
        raise typer.Exit(code=1) from None
    except Exception:
        logger.exception("inspect cache list failed unexpectedly")
        renderer.error(
            ErrorView(
                kind="UnexpectedError",
                message="inspect cache list failed unexpectedly; see logs",
            )
        )
        raise typer.Exit(code=1) from None

    from runtime.application.inspect import CACHE_LAYER_ORDER

    if result.total == 0:
        plain = "no cache entries recorded yet"
    else:
        sections: list[str] = []
        for layer in CACHE_LAYER_ORDER:
            entries = result.layers[layer]
            lines = [f"{layer} ({len(entries)}):"]
            lines.extend(f"  {entry_hash[:12]}" for entry_hash in entries)
            sections.append("\n".join(lines))
        plain = "\n".join(sections)
    renderer.custom(
        CustomView(
            plain=plain,
            object={
                "layers": {layer: list(result.layers[layer]) for layer in CACHE_LAYER_ORDER},
                "counts": {layer: result.counts[layer] for layer in CACHE_LAYER_ORDER},
                "total": result.total,
            },
            rich=plain,
        )
    )


def _run_prune(
    dry_run: bool, keep: int, prune_pinned: bool
) -> tuple[PrunePlan, int, list[tuple[str, str]]]:
    """Compose + run prune (Story 3.3).

    Fails closed when ``current.json`` is absent (refuse to prune without the
    active set). Real removal holds the seed mutex across plan + deletions so
    a concurrent ``wallpaper set``/reconcile cannot activate an entry
    mid-prune; dry-run is read-only and takes no lock. Returns the plan, the
    number actually removed (0 for dry-run), and the removed ``(layer, hash)``
    pairs.
    """
    state_root = _resolve_state_root()

    from runtime.adapters.flock_seed_mutex import FlockSeedMutex
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.prune_source import entries_for, remove_entry, seed_pins
    from runtime.application.prune import PruneUseCase

    state_repo = JsonStateRepository(state_root=state_root)
    state = state_repo.load_current()
    if state is None:
        raise ValueError("no runtime state recorded (missing current.json); refusing to prune")

    def _plan() -> PrunePlan:
        return PruneUseCase(
            state_repo=state_repo,
            entries_for=lambda layer: entries_for(state_root, layer),
            seed_pins=lambda: seed_pins(state_root),
            keep=keep,
        ).run(prune_pinned=prune_pinned)

    if dry_run:
        return _plan(), 0, []

    mutex = FlockSeedMutex(state_root / ".seed.lock")
    removed: list[tuple[str, str]] = []
    failures: list[str] = []
    with mutex.hold(blocking=True):
        plan = _plan()
        for layer, hashes in plan.removals.items():
            for entry_hash in hashes:
                try:
                    if remove_entry(state_root, layer, entry_hash):
                        removed.append((layer, entry_hash))
                except (OSError, ValueError) as exc:
                    failures.append(f"{layer}/{entry_hash}")
                    logger.error("prune: failed cache/%s/%s: %s", layer, entry_hash, exc)
    # One audit line per real execution (AD-30 / R-1), after the seed mutex is
    # released so no two flock files are held at once. An append failure must
    # not mask a removal failure: report both.
    append_error: str | None = None
    try:
        _append_prune_history(state_root, state, removed, failures)
    except (ValueError, RuntimeError, OSError) as exc:
        append_error = str(exc)
        logger.error("prune: audit line not written: %s", exc)
    if failures or append_error:
        parts = []
        if failures:
            parts.append(
                f"prune removed {len(removed)}, failed {len(failures)}: {', '.join(failures)}"
            )
        if append_error:
            parts.append(f"audit line not written: {append_error}")
        raise RuntimeError("; ".join(parts))
    return plan, len(removed), removed


def _append_prune_history(
    state_root: Path,
    state: DesktopState,
    removed: list[tuple[str, str]],
    failures: list[str],
) -> None:
    """Append the single prune audit line (AD-30, R-1). Uses the locked writer."""
    from runtime.adapters.seeder import CacheSeeder
    from runtime.application.prune import LAYERS

    layers: dict[str, object] = {}
    for layer in LAYERS:
        count = sum(1 for removed_layer, _ in removed if removed_layer == layer)
        if count:
            layers[layer] = count
    details: dict[str, object] = {"removed": len(removed), "layers": layers}
    if failures:
        details["failed"] = len(failures)
    CacheSeeder(state_root).append_history(
        trigger="prune",
        wallpaper_hash=state.wallpaper.content_hash,
        palette_hash=state.palette.entry_hash if state.palette else None,
        effects_hash=state.effects.entry_hash if state.effects else None,
        icons_hash=state.icons.entry_hash if state.icons else None,
        source_path=state.wallpaper.source_path,
        details=details,
    )


@cache_app.command("prune")
def inspect_cache_prune(
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report reclaimable entries without removing them"
    ),
    keep: int = typer.Option(
        5, "--keep", min=0, help="Keep the N most recent entries per layer (default 5)"
    ),
    prune_pinned: bool = typer.Option(
        False, "--prune-pinned", help="Also prune seed-pinned entries (default: protected)"
    ),
) -> None:
    """Prune unreferenced cache entries outside the keep-policy (FR-6, AD-24).

    Without ``--dry-run`` this is a MUTATING command: it deletes exactly the
    planned entries (active, last-N, seed-pinned, and undated are protected).
    ``--dry-run`` is the safe path — it reports reclaimable entries and removes
    nothing. Idempotent: a second run finds nothing to remove and exits 0.
    """
    renderer = create_renderer(output_format)
    try:
        plan, removed, removed_pairs = _run_prune(dry_run, keep, prune_pinned)
    except (ValueError, RuntimeError, OSError) as exc:
        logger.error("inspect cache prune failed: %s", exc)
        renderer.error(ErrorView(kind=type(exc).__name__, message=str(exc)))
        raise typer.Exit(code=1) from None
    except Exception:
        logger.exception("inspect cache prune failed unexpectedly")
        renderer.error(
            ErrorView(
                kind="UnexpectedError",
                message="inspect cache prune failed unexpectedly; see logs",
            )
        )
        raise typer.Exit(code=1) from None

    from runtime.application.prune import LAYERS

    if plan.total_removable == 0:
        plain = "nothing to prune"
    elif dry_run:
        lines = [f"reclaimable: {plan.total_removable}"]
        for layer in LAYERS:
            if plan.removals[layer]:
                lines.append(f"{layer} ({len(plan.removals[layer])}):")
                lines.extend(f"  {h[:12]}" for h in plan.removals[layer])
        plain = "\n".join(lines)
    else:
        lines = [f"reclaimed: {removed}"]
        lines.extend(f"  {layer}/{h[:12]}" for layer, h in removed_pairs)
        plain = "\n".join(lines)
    renderer.custom(
        CustomView(
            plain=plain,
            object={
                "dry_run": dry_run,
                "keep": keep,
                "prune_pinned": prune_pinned,
                "kept": plan.kept,
                "removals": {layer: list(plan.removals[layer]) for layer in LAYERS},
                "total_removable": plan.total_removable,
                "removed": removed,
                "removed_count": removed,
                "removed_hashes": [{"layer": layer, "hash": h} for layer, h in removed_pairs],
            },
            rich=plain,
        )
    )


if __name__ == "__main__":
    app()
