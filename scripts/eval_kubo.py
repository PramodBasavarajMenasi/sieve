"""ipfs/kubo preset for ``scripts/eval/run_eval.py`` (kept so existing kubo commands work).

    uv run python scripts/eval_kubo.py --failing --siftwise-toml scripts/eval/kubo.siftwise.toml
    uv run python scripts/eval_kubo.py --replay-main --kubo-checkout ../kubo --go go

Equivalent to ``python -m scripts.eval.run_eval --repo ipfs/kubo --go-module
github.com/ipfs/kubo --checkout <kubo checkout> ...``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer

if not __package__:  # run as a file path: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.eval import run_eval


def main(
    runs: Annotated[int, typer.Option(min=1, help="Most recent PR runs to evaluate.")] = 10,
    failing: Annotated[
        bool, typer.Option("--failing", help="Also evaluate every PR run that had failures.")
    ] = False,
    replay_main: Annotated[
        bool, typer.Option("--replay-main", help="Treat each main run as a PR.")
    ] = False,
    go_module: Annotated[str, typer.Option(help="Go module path.")] = "github.com/ipfs/kubo",
    siftwise_toml: Annotated[
        Path | None, typer.Option(help="Repo config with [[depends]] / always_run.")
    ] = None,
    kubo_checkout: Annotated[
        Path | None, typer.Option(help="Git clone of kubo, for the import graph.")
    ] = None,
    go: Annotated[str, typer.Option(help="go binary for `go list`.")] = "go",
    cache_dir: Annotated[Path, typer.Option(help="Cache for compares and graphs.")] = Path(
        ".eval-cache"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", help="Details for every run.")] = False,
) -> None:
    run_eval.main(
        repo="ipfs/kubo",
        runs=runs,
        failing=failing,
        replay_main=replay_main,
        language="go",
        go_module=go_module,
        siftwise_toml=siftwise_toml,
        checkout=kubo_checkout,
        go=go,
        cache_dir=cache_dir,
        verbose=verbose,
    )


if __name__ == "__main__":
    typer.run(main)
