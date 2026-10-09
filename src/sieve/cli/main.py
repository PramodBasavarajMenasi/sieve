"""`sieve` command-line interface."""

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer

from sieve import __version__
from sieve.cli.gograph import build_graph, run_go_list
from sieve.cli.pygraph import build_py_graph
from sieve.cli.repoconfig import (
    FILE_NAME,
    RepoConfig,
    RepoConfigError,
    load_repo_config,
    read_go_module,
)

app = typer.Typer(help="Sieve: test impact analysis for CI.", no_args_is_help=True)

# `sieve select` exit codes, besides 0 (success, including "no tests affected").
EXIT_API_ERROR = 1
EXIT_USAGE = 2
EXIT_FULL_SUITE_NO_COMMAND = 3


@app.callback()
def main() -> None:
    """Sieve: test impact analysis for CI."""


@app.command()
def version() -> None:
    """Print the sieve version."""
    typer.echo(__version__)


@app.command("select")
def select_command(
    repo: Annotated[str, typer.Option(help="Repository name in sieve, e.g. acme/shop.")],
    base: Annotated[str, typer.Option(help="Base ref to diff against.")] = "main",
    head: Annotated[str, typer.Option(help="Head ref.")] = "HEAD",
    server: Annotated[str, typer.Option(help="sieve server URL.")] = "http://localhost:8000",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full API response as JSON.")
    ] = False,
    go_list: Annotated[
        bool,
        typer.Option(help="For Go repos, run `go list` to select tests of dependent packages."),
    ] = True,
    py_imports: Annotated[
        bool,
        typer.Option(
            help="When Python files changed, build a static import graph to select test files "
            "that import changed modules."
        ),
    ] = True,
) -> None:
    """Print the test command for the changes in BASE...HEAD.

    The command goes to stdout (so `eval "$(sieve select ...)"` works); the mode and reason go
    to stderr. Needs SIEVE_API_TOKEN. Exit codes: 0 ok (stdout is empty if no tests are
    affected), 1 API error, 2 usage error, 3 full suite needed but no command is known.
    """
    token = os.environ.get("SIEVE_API_TOKEN")
    if not token:
        _err("error: set SIEVE_API_TOKEN")
        raise typer.Exit(EXIT_USAGE)

    root = git_toplevel() or Path.cwd()
    config_path = root / FILE_NAME
    try:
        config = load_repo_config(config_path) if config_path.is_file() else RepoConfig()
    except RepoConfigError as exc:
        _err(f"error: {exc}")
        raise typer.Exit(EXIT_USAGE) from exc

    diff = git_diff(base, head)
    changed = diff[0] if diff else None
    body: dict[str, Any] = {
        "repo": repo,
        "changed_files": changed or [],
        "changed_files_known": changed is not None,
        "depends": [{"tests": r.tests, "on": list(r.on)} for r in config.depends],
        "always_run": list(config.always_run),
    }
    if diff and diff[1]:
        body["deleted_files"] = diff[1]
    go_module = read_go_module(root)
    if go_module:
        body["go_module"] = go_module
        if go_list and changed:
            body["affected_packages"] = go_dependents(root, changed)
    if py_imports and changed and any(p.endswith(".py") for p in changed):
        body["affected_files"] = python_dependents(root, changed)
    try:
        response = httpx.post(
            f"{server.rstrip('/')}/select",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        _err(f"error: could not reach sieve at {server}: {exc}")
        raise typer.Exit(EXIT_API_ERROR) from exc
    if response.status_code != httpx.codes.OK:
        _err(f"error: sieve returned {response.status_code}: {_detail(response)}")
        raise typer.Exit(EXIT_API_ERROR)

    result: dict[str, Any] = response.json()
    if json_output:
        typer.echo(json.dumps(result, indent=2))
        return

    _err(
        f"sieve: {result['mode']} ({result['selected_count']} of {result['total_known']} "
        f"known tests): {result['reason']}"
    )
    if result["command"]:
        typer.echo(result["command"])
    elif result["mode"] == "full":
        # Never let "run nothing" be mistaken for "run everything".
        _err("error: the full suite is needed, but sieve knows no command for this repo's tests")
        raise typer.Exit(EXIT_FULL_SUITE_NO_COMMAND)


def git_toplevel() -> Path | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.decode(errors="replace").strip())


def go_dependents(root: Path, changed: list[str]) -> dict[str, list[str]]:
    """Packages whose tests import a changed package; empty (fallbacks apply) if go list fails."""
    packages = run_go_list(root)
    if packages is None:
        _err("warning: `go list -deps -test -json ./...` failed; not selecting dependents")
        return {}
    affected = build_graph(packages, root).affected(changed)
    if affected:
        _err(f"sieve: go list: {len(affected)} package(s) depend on changed Go code")
    return affected


def python_dependents(root: Path, changed: list[str]) -> dict[str, list[str]]:
    """Test files that import a changed module; empty (fallbacks apply) if the graph fails."""
    try:
        graph = build_py_graph(root)
    except (OSError, ValueError, RecursionError) as exc:
        _err(f"warning: could not build the Python import graph ({exc}); not selecting importers")
        return {}
    affected = graph.affected(changed)
    if affected:
        _err(f"sieve: python imports: {len(affected)} test file(s) import changed modules")
    return affected


def git_diff(base: str, head: str) -> tuple[list[str], list[str]] | None:
    """``(changed, deleted)`` files in ``base...head``, or None if git fails.

    Renames give both paths in ``changed``, and the old path in ``deleted``.
    """
    args = ["git", "diff", "--name-status", "-z", "-M", f"{base}...{head}"]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        _err(f"warning: could not run git diff ({exc}); asking for the full suite")
        return None
    if proc.returncode != 0:
        message = proc.stderr.decode(errors="replace").strip().splitlines()
        _err(
            f"warning: git diff {base}...{head} failed ({message[0] if message else 'no output'});"
            " asking for the full suite"
        )
        return None
    return parse_name_status(proc.stdout), parse_deleted(proc.stdout)


def _name_status_entries(output: bytes) -> list[tuple[str, list[str]]]:
    """``(status letter, paths)`` from ``git diff --name-status -z``."""
    fields = output.decode("utf-8", errors="replace").split("\0")
    entries = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        count = 2 if status[0] in "RC" else 1
        entries.append((status[0], fields[i + 1 : i + 1 + count]))
        i += 1 + count
    return entries


def parse_name_status(output: bytes) -> list[str]:
    """Paths from ``git diff --name-status -z``, de-duplicated in order.

    Renames contribute both the old and the new path; copies only the new one (the source
    is unchanged).
    """
    paths: list[str] = []
    for status, entry in _name_status_entries(output):
        paths.extend(entry[1:] if status == "C" else entry)
    return list(dict.fromkeys(paths))


def parse_deleted(output: bytes) -> list[str]:
    """Paths that no longer exist at head: deleted files and the old side of renames."""
    deleted = [entry[0] for status, entry in _name_status_entries(output) if status in "DR"]
    return list(dict.fromkeys(deleted))


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json()["detail"])
    except (ValueError, KeyError, TypeError):
        return response.text[:300]


def _err(message: str) -> None:
    typer.echo(message, err=True)
