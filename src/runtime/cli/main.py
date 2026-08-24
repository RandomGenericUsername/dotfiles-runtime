"""Typer CLI — the composition root (outer shell) of the runtime hexagon."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

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


@app.callback()
def main_callback(
    output_format: OutputFormat = typer.Option(
        OutputFormat.PLAIN,
        "--format",
        "-f",
        help="Output format",
    ),
) -> None:
    pass


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
