"""Typer CLI — the composition root (outer shell) of the runtime hexagon."""

from __future__ import annotations

import logging
import os
import sys
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
    from runtime.application.reconcile import ReconcileResult

app = typer.Typer(
    name="dotfiles-runtime",
    help=(
        "Dotfiles runtime engine.\n\n"
        "Manages wallpaper, state, history, and cache for the dotfiles system.\n\n"
        "Commands:\n"
        "  version  Show the installed package version\n"
        "  wallpaper set  Derive, cache, and persist state for a wallpaper"
    ),
)

wallpaper_app = typer.Typer(help="Wallpaper commands")
app.add_typer(wallpaper_app, name="wallpaper")

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
    default_png = install_spine / "generated" / "default.png"
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
    # P3 — do not auto-seed before reconcile (reconcile must fail loud on absent state)
    if "reconcile" in sys.argv:
        return
    _run_seed_if_needed()


def _run_wallpaper_set(image_path: Path) -> ApplyWallpaperResult:
    """Compose and run ApplyWallpaperUseCase (wallpaper set command).

    Mirrors ``_run_seed_if_needed``'s wiring: resolve state_root /
    install_spine (both absolute), construct the JSON state repository,
    the CSG/WEG/ITR adapters, the ``CacheSeeder``, and the state mutex,
    then inject all of them into ``ApplyWallpaperUseCase``. The mutex is
    the same flock file the seeder uses: apply holds it blocking around
    its read-modify-write of current.json, so a concurrent first-run seed
    can never be overtaken (Story 1.13 review, D1 decision).
    """
    state_root = _resolve_state_root()
    install_spine = _resolve_install_spine()

    from runtime.adapters.csg_adapter import CsgAdapter
    from runtime.adapters.flock_seed_mutex import FlockSeedMutex
    from runtime.adapters.itr_adapter import ItrAdapter
    from runtime.adapters.json_state_repository import JsonStateRepository
    from runtime.adapters.seeder import CacheSeeder
    from runtime.adapters.weg_adapter import WegAdapter
    from runtime.application.apply_wallpaper import ApplyWallpaperUseCase

    use_case = ApplyWallpaperUseCase(
        state_repo=JsonStateRepository(state_root=state_root),
        csg=CsgAdapter(),
        weg=WegAdapter(),
        itr=ItrAdapter(),
        install_spine=install_spine,
        state_root=state_root,
        seeder=CacheSeeder(state_root),
        mutex=FlockSeedMutex(state_root / ".seed.lock"),
    )
    return use_case.run(image_path)


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


@wallpaper_app.command("set")
def wallpaper_set(
    image_path: Path = _IMAGE_PATH_ARG,
    output_format: OutputFormat = _OUTPUT_FORMAT_OPTION,
) -> None:
    """Derive, cache, and persist the desktop state for a wallpaper.

    The root callback has already run first-run seeding; the apply
    ensures cache entries for the derived layers (zero tool invocations
    on cache hits) and writes current.json. Symlink repoint, history,
    and desktop reload are Epic 2 scope (ReconcileDesktopStateUseCase).
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

    state = result.state
    palette_desc = "cache hit" if result.cache_hit_palette else "generated"
    effects_desc = (
        "cache hit"
        if result.cache_hit_effects
        else ("generated" if result.effects else "unavailable")
    )
    icons_desc = (
        "cache hit" if result.cache_hit_icons else ("generated" if result.icons else "unavailable")
    )
    summary = (
        f"wallpaper applied: {result.wallpaper_hash[:12]}"
        f" (palette {palette_desc}, effects {effects_desc}, icons {icons_desc})"
    )
    renderer.custom(
        CustomView(
            plain=summary,
            object={
                "wallpaper": result.wallpaper_hash,
                "palette": state.palette.entry_hash if state.palette else None,
                "effects": state.effects.entry_hash if state.effects else None,
                "icons": state.icons.entry_hash if state.icons else None,
                "cache_hits": {
                    "palette": result.cache_hit_palette,
                    "effects": result.cache_hit_effects,
                    "icons": result.cache_hit_icons,
                },
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

    Desktop reload (contract step 5) is Stories 2.3-2.6.
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

    # Forward-looking placeholder for reload adapters (Stories 2.3-2.6):
    # ReconcileResult.reload_failures is [] until adapters land; when populated
    # we surface the failure and exit non-zero per AC 4 (no daemon retry).
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
    # Structural placeholder: reload adapters will populate skipped with
    # entries containing "reload" or "consumer" — CLI already renders skipped.

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


if __name__ == "__main__":
    app()
