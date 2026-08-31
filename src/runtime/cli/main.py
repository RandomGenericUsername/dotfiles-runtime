"""Typer CLI — the composition root (outer shell) of the runtime hexagon."""

from __future__ import annotations

import logging
import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from pathlib import Path

import typer
from cli_output.adapters.factory import create_renderer
from cli_output.domain.enums import OutputFormat
from cli_output.domain.views import CustomView, ErrorView

app = typer.Typer(
    name="dotfiles-runtime",
    help=(
        "Dotfiles runtime engine.\n\n"
        "Manages wallpaper, state, history, and cache for the dotfiles system.\n\n"
        "Commands:\n"
        "  version  Show the installed package version"
    ),
)

logger = logging.getLogger(__name__)


def _resolve_install_spine() -> Path:
    """Resolve provisioning install spine path (read-only).

    Resolution order (AD-15):
    1. Check ``$DOTFILES_INSTALL_SPINE`` env var (explicit override)
    2. Fallback: ``$XDG_DATA_HOME/dotfiles`` (default ``~/.local/share/dotfiles``)
    """
    if explicit := os.environ.get("DOTFILES_INSTALL_SPINE"):
        return Path(explicit)
    xdg_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return xdg_data / "dotfiles"


def _resolve_state_root() -> Path:
    """Resolve runtime state root path.

    Uses ``$XDG_STATE_HOME/dotfiles`` (default ``~/.local/state/dotfiles``).
    """
    xdg_state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return xdg_state / "dotfiles"


def _run_seed_if_needed() -> None:
    """Run first-run seeding if current.json is absent.

    Instantiates all adapters and the SeedCacheUseCase, then calls run().
    Errors are logged but do not prevent CLI commands from executing.
    """
    try:
        from runtime.adapters.csg_adapter import CsgAdapter
        from runtime.adapters.itr_adapter import ItrAdapter
        from runtime.adapters.json_state_repository import JsonStateRepository
        from runtime.adapters.weg_adapter import WegAdapter
        from runtime.application.seed_cache import SeedCacheUseCase

        state_root = _resolve_state_root()
        install_spine = _resolve_install_spine()

        state_repo = JsonStateRepository(state_root=state_root)
        csg = CsgAdapter()
        weg = WegAdapter()
        itr = ItrAdapter()

        # Factory is not used by SeedCacheUseCase (wallpaper backend deferred to Epic 2)
        # Provide a no-op factory placeholder
        from runtime.ports.wallpaper_backend_factory import IWallpaperBackendFactory

        class _NoOpFactory(IWallpaperBackendFactory):
            def create_static(self, backend_type: object) -> object:  # type: ignore[override]
                raise NotImplementedError("wallpaper backend deferred to Epic 2")

            def create_video(self, backend_type: object) -> object:  # type: ignore[override]
                raise NotImplementedError("wallpaper backend deferred to Epic 2")

            def auto_detect(self, source_path: str) -> None:  # type: ignore[override]
                return None

        use_case = SeedCacheUseCase(
            state_repo=state_repo,
            csg=csg,
            weg=weg,
            itr=itr,
            factory=_NoOpFactory(),
            install_spine=install_spine,
            state_root=state_root,
        )
        use_case.run()
    except Exception as exc:
        # Seeding failure must not block CLI commands
        logger.debug("seed skipped or failed: %s", exc)


@app.callback()
def main_callback(
    output_format: OutputFormat = typer.Option(
        OutputFormat.PLAIN,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    _run_seed_if_needed()


@app.command(help="Show the installed package version")
def version(output_format: OutputFormat = typer.Option(OutputFormat.PLAIN, "--format", "-f")) -> None:
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


if __name__ == "__main__":
    app()
