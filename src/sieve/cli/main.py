"""`sieve` command-line interface."""

import json
import os
import subprocess
from typing import Annotated, Any

import httpx
import typer

from sieve import __version__

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

    changed = git_changed_files(base, head)
    body = {
        "repo": repo,
        "changed_files": changed or [],
        "changed_files_known": changed is not None,
    }
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


def git_changed_files(base: str, head: str) -> list[str] | None:
    """Files changed in ``base...head`` (renames give both paths), or None if git fails."""
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
    return parse_name_status(proc.stdout)


def parse_name_status(output: bytes) -> list[str]:
    """Paths from ``git diff --name-status -z``, de-duplicated in order.

    Renames contribute both the old and the new path; copies only the new one (the source
    is unchanged).
    """
    fields = output.decode("utf-8", errors="replace").split("\0")
    paths: list[str] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        count = 2 if status[0] in "RC" else 1
        entry = fields[i + 1 : i + 1 + count]
        paths.extend(entry[1:] if status[0] == "C" else entry)
        i += 1 + count
    return list(dict.fromkeys(paths))


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json()["detail"])
    except (ValueError, KeyError, TypeError):
        return response.text[:300]


def _err(message: str) -> None:
    typer.echo(message, err=True)
