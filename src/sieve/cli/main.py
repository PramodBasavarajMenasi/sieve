"""`sieve` command-line interface."""

import typer

from sieve import __version__

app = typer.Typer(help="Sieve: test impact analysis for CI.", no_args_is_help=True)


@app.callback()
def main() -> None:
    """Keep `sieve` a command group even while it has a single subcommand."""


@app.command()
def version() -> None:
    """Print the sieve version."""
    typer.echo(__version__)
