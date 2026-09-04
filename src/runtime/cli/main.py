"""Typer CLI — the composition root (outer shell) of the runtime hexagon."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import TYPE_CHECKING

import typer
from cli_output.adapters.factory import create_renderer
from cli_output.domain.enums import OutputFormat
from cli_output.domain.views import CustomView, ErrorView

if TYPE_CHECKING:
    from runtime.application.apply_wallpaper import ApplyWallpaperResult
    from runtime.application.inspect import (
        HistoryRecord,
        InspectCacheResult,
        InspectStatusResult,
    )
    from runtime.application.reconcile import ReconcileResult
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
        seeder = CacheSeeder(state_root)
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
    output_format: OutputFormat = typer.Option(
        OutputFormat.PLAIN,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    # P3 — do not auto-seed before reconcile (reconcile must fail loud on
    # absent state). Same for the read-only inspect commands (Story 3.2,
    # AC 3): auto-seeding would mask the absent-state error on provisioned
    # machines and invoke csg/weg/itr for free behind a read command.
    if "reconcile" in sys.argv or "inspect" in sys.argv:
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
    ``current.json``), terminal palette (OSC from ``current/colors.yaml``).
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
        seeder=CacheSeeder(state_root),
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
        seeder=CacheSeeder(state_root),
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
    )
    renderer.custom(
        CustomView(
            plain=summary,
            object={
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
                "skipped": list(result.reconcile.skipped),
                "cache_regenerated": list(result.reconcile.cache_regenerated),
                "reload_failures": list(result.reconcile.reload_failures),
            },
            rich=summary,
        )
    )


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
        seeder=CacheSeeder(state_root),
        mutex=FlockSeedMutex(state_root / ".seed.lock"),
        reloaders=_build_reloaders(state_root),
    )
    return use_case.run()


@app.command(help="Repoint current/ symlinks to converge the desktop with current.json")
def reconcile(
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
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
    try:
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
    )
    renderer.custom(
        CustomView(
            plain=summary,
            object={
                "repointed": [str(p) for p in result.repointed],
                "skipped": list(result.skipped),
                "cache_regenerated": list(result.cache_regenerated),
                "reload_failures": list(result.reload_failures),
            },
            rich=summary,
        )
    )


def _run_inspect_status() -> InspectStatusResult:
    """Compose and run InspectStateUseCase (inspect status command).

    Mirrors ``_run_reconcile``'s wiring: resolve state_root (absolute),
    construct the JSON state repository, and inject both into the
    read-only use case. No seeder, mutex, derivation adapters, or
    reloaders — inspection mutates nothing (AC 4) and consumes no tools.
    """
    state_root = _resolve_state_root()

    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.application.inspect import InspectStateUseCase

    use_case = InspectStateUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        state_root=state_root,
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


@cache_app.command("list")
def inspect_cache_list(
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
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
    """
    renderer = create_renderer(output_format)
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


if __name__ == "__main__":
    app()
