"""Backfill sieve history from a GitHub repo's past Actions runs.

For each completed workflow run, downloads the JUnit artifacts, extracts the ``*.xml`` files and
uploads them to ``POST /runs``. Safe to re-run: runs sieve already has (``GET /runs/lookup``)
are skipped before anything is downloaded.

    GITHUB_TOKEN=... SIEVE_API_TOKEN=... uv run python scripts/backfill.py --repo acme/shop
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from types import TracebackType
from typing import Annotated, Any, Self

import httpx
import typer

GITHUB_API = "https://api.github.com"
PER_PAGE = 100
# The compare API lists at most 300 files; a list that long is probably truncated.
COMPARE_FILE_CAP = 300
# Matches the server's default upload limit; also guards against zip bombs.
MAX_XML_BYTES = 50 * 1024 * 1024
PULL_REQUEST_EVENTS = frozenset({"pull_request", "pull_request_target"})

Json = dict[str, Any]
Echo = Callable[[str], None]


class BackfillError(Exception):
    """A run could not be backfilled; reported and counted as an error."""


class Outcome(StrEnum):
    NEW = "new"
    SKIPPED = "skipped"
    NO_ARTIFACTS = "no artifacts"
    EXPIRED = "expired"
    ERROR = "errors"


@dataclass
class Summary:
    counts: dict[Outcome, int] = field(default_factory=lambda: dict.fromkeys(Outcome, 0))

    def add(self, outcome: Outcome) -> None:
        self.counts[outcome] += 1

    def __str__(self) -> str:
        return ", ".join(f"{count} {outcome.value}" for outcome, count in self.counts.items())


def _warn(message: str) -> None:
    typer.echo(f"  warning: {message}", err=True)


# --- GitHub -------------------------------------------------------------------------------


class GitHubClient:
    """Minimal GitHub REST client that waits out rate limits and retries transient errors.

    * A 403/429 with ``Retry-After`` waits that long (secondary rate limit).
    * A 403/429 with ``X-RateLimit-Remaining: 0`` waits until ``X-RateLimit-Reset``.
    * A success with ``X-RateLimit-Remaining: 0`` makes the *next* request wait for the reset.
    * 5xx, other 429s and connection errors back off exponentially.
    * A plain 403 (no rate-limit headers) is a permission error and is not retried.
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = GITHUB_API,
        max_retries: int = 5,
        backoff_seconds: float = 2.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "sieve-backfill",
            },
            # Artifact downloads redirect to blob storage. httpx drops the Authorization
            # header on cross-origin redirects, so the token is not sent there.
            follow_redirects=True,
            timeout=60,
        )
        self._max_retries = max_retries
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        self._clock = clock
        self._reset_at: float | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._http.close()

    def get(self, url: str, **params: Any) -> httpx.Response:
        """GET with rate-limit handling. Returns the final response, whatever its status."""
        attempt = 0
        while True:
            self._wait_for_reset()
            try:
                response = self._http.get(url, params=params or None)
            except httpx.TransportError:
                if attempt >= self._max_retries:
                    raise
                self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            delay = self._retry_delay(response, attempt)
            if delay is None or attempt >= self._max_retries:
                if response.is_success:
                    self._remember_exhaustion(response)
                return response
            typer.echo(f"  GitHub returned {response.status_code}; retrying in {delay:.0f}s")
            self._sleep(delay)
            attempt += 1

    def get_json(self, url: str, **params: Any) -> Json:
        response = self.get(url, **params)
        response.raise_for_status()
        data: Json = response.json()
        return data

    # Repository data ---------------------------------------------------------------------

    def default_branch(self, repo: str) -> str:
        return str(self.get_json(f"/repos/{repo}")["default_branch"])

    def iter_completed_runs(self, repo: str, workflow: str | None, max_runs: int) -> Iterator[Json]:
        """Completed workflow runs, newest first, at most ``max_runs``."""
        path = (
            f"/repos/{repo}/actions/workflows/{workflow}/runs"
            if workflow
            else f"/repos/{repo}/actions/runs"
        )
        yielded, page = 0, 1
        while yielded < max_runs:
            runs = self.get_json(path, status="completed", per_page=PER_PAGE, page=page).get(
                "workflow_runs", []
            )
            for run in runs[: max_runs - yielded]:
                yield run
                yielded += 1
            if len(runs) < PER_PAGE:
                return
            page += 1

    def list_artifacts(self, repo: str, run_id: int) -> list[Json]:
        artifacts: list[Json] = self.get_json(
            f"/repos/{repo}/actions/runs/{run_id}/artifacts", per_page=PER_PAGE
        ).get("artifacts", [])
        return artifacts

    def download_artifact(self, url: str) -> bytes | None:
        """The artifact's zip, or None if GitHub says it has expired (410 Gone)."""
        response = self.get(url)
        if response.status_code == httpx.codes.GONE:
            return None
        response.raise_for_status()
        return response.content

    def changed_files(self, repo: str, run: Json) -> tuple[list[str], bool]:
        """``(paths, known)``: the files this run's change touched.

        The range matches what ``sieve select`` diffs for the same change:

        * ``pull_request``: the PR's base sha ... head sha (the whole PR, not just its last
          commit).
        * ``push``: the push's ``before`` ... ``after`` (every pushed commit), falling back to
          the head commit's first parent when there is no usable ``before`` (new branch).
        * anything else (schedule, workflow_dispatch, pull_request_target, ...): unknown.

        ``known`` is False (with no paths) whenever the range can't be determined or the diff
        is incomplete.
        """
        head = run["head_sha"]
        event = run.get("event")
        if event == "pull_request":
            base = self._pull_request_base(repo, run)
            if base is None:
                _warn(f"run {run['id']}: no pull request found for {head[:7]}; diff unknown")
                return [], False
            return self._compare(repo, base, head)
        if event == "push":
            before = self._push_before(repo, run)
            if before is not None:
                return self._compare(repo, before, head)
            return self._first_parent_diff(repo, head)
        _warn(f"run {run['id']}: no diff range for {event!r} runs; changed files unknown")
        return [], False

    def _pull_request_base(self, repo: str, run: Json) -> str | None:
        """The base sha of the PR this run tested, or None if no PR can be identified.

        Sources, in order: the run's ``pull_requests`` (empty for fork PRs), the
        commit-to-PR lookup (only finds open PRs for commits not on the default branch),
        then the repo's PR list filtered by head owner and branch, including closed PRs.
        """
        head = run["head_sha"]
        # GitHub links these PRs to the run or commit, so any of them will do as a fallback.
        linked: list[Json] = run.get("pull_requests") or []
        if not linked:
            linked = self._list_json(f"/repos/{repo}/commits/{head}/pulls") or []
        if linked:
            pr: Json | None = _pick_pull_request(linked, run) or linked[0]
        else:
            # Same branch name can belong to unrelated PRs: only accept a clear match.
            pr = _pick_pull_request(self._pull_requests_for_branch(repo, run), run)
        sha = pr.get("base", {}).get("sha") if pr else None
        return str(sha) if sha else None

    def _pull_requests_for_branch(self, repo: str, run: Json) -> list[Json]:
        owner = (run.get("head_repository") or {}).get("owner", {}).get("login")
        branch = run.get("head_branch")
        if not owner or not branch:
            return []
        return (
            self._list_json(
                f"/repos/{repo}/pulls", state="all", head=f"{owner}:{branch}", per_page=PER_PAGE
            )
            or []
        )

    def _list_json(self, url: str, **params: Any) -> list[Json] | None:
        """A JSON array endpoint's objects, or None (with a warning) if the call fails."""
        try:
            response = self.get(url, **params)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            _warn(f"could not get {url}: {exc}")
            return None
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else None

    def _push_before(self, repo: str, run: Json) -> str | None:
        """The push's ``before`` sha (from the run's check suite), if it is a usable base."""
        suite_id = run.get("check_suite_id")
        if not suite_id:
            return None
        try:
            before = self.get_json(f"/repos/{repo}/check-suites/{suite_id}").get("before")
        except (httpx.HTTPError, ValueError) as exc:
            _warn(f"could not get check suite {suite_id}: {exc}")
            return None
        # All zeros: the push created the branch, so there is no previous tip.
        if not before or set(before) == {"0"}:
            return None
        return str(before)

    def _first_parent_diff(self, repo: str, sha: str) -> tuple[list[str], bool]:
        try:
            parents = self.get_json(f"/repos/{repo}/commits/{sha}")["parents"]
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            _warn(f"could not get commit {sha[:7]}: {exc}")
            return [], False
        if not parents:
            _warn(f"{sha[:7]} is a root commit; changed files unknown")
            return [], False
        return self._compare(repo, parents[0]["sha"], sha)

    def _compare(self, repo: str, base: str, head: str) -> tuple[list[str], bool]:
        try:
            files: list[Json] = self.get_json(f"/repos/{repo}/compare/{base}...{head}").get(
                "files", []
            )
        except (httpx.HTTPError, ValueError) as exc:
            _warn(f"could not compare {base[:7]}...{head[:7]}: {exc}")
            return [], False
        if len(files) >= COMPARE_FILE_CAP:
            _warn(
                f"{base[:7]}...{head[:7]} changes {len(files)}+ files (list truncated); "
                "changed files unknown"
            )
            return [], False
        paths: list[str] = []
        for entry in files:
            paths.append(entry["filename"])
            if entry.get("previous_filename"):  # renames affect the old path too
                paths.append(entry["previous_filename"])
        return paths, True

    # Rate limiting -----------------------------------------------------------------------

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float | None:
        status, headers = response.status_code, response.headers
        if status in (403, 429):
            retry_after = headers.get("retry-after", "")
            if retry_after.isdigit():
                return float(retry_after)
            if headers.get("x-ratelimit-remaining") == "0":
                return self._seconds_until(headers.get("x-ratelimit-reset"))
            # GitHub: without headers, wait at least a minute after a secondary rate limit.
            return max(60.0, self._backoff(attempt)) if status == 429 else None
        if status >= 500:
            return self._backoff(attempt)
        return None

    def _remember_exhaustion(self, response: httpx.Response) -> None:
        if response.headers.get("x-ratelimit-remaining") == "0":
            self._reset_at = self._clock() + self._seconds_until(
                response.headers.get("x-ratelimit-reset")
            )

    def _wait_for_reset(self) -> None:
        if self._reset_at is None:
            return
        wait = self._reset_at - self._clock()
        self._reset_at = None
        if wait > 0:
            typer.echo(f"  GitHub rate limit exhausted; waiting {wait:.0f}s for reset")
            self._sleep(wait)

    def _seconds_until(self, reset: str | None) -> float:
        # +1s margin: the reset timestamp has one-second resolution.
        if reset is None or not reset.isdigit():
            return 60.0
        return max(int(reset) - self._clock(), 0) + 1

    def _backoff(self, attempt: int) -> float:
        return float(self._backoff_seconds * 2**attempt)


# --- sieve --------------------------------------------------------------------------------


class SieveClient:
    def __init__(self, server: str, token: str) -> None:
        self._http = httpx.Client(
            base_url=server, headers={"Authorization": f"Bearer {token}"}, timeout=300
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._http.close()

    def lookup(
        self, repo: str, ci_run_id: str, run_attempt: int, variant: str | None = None
    ) -> Json | None:
        """The stored run for this CI run attempt and variant, or None if sieve lacks it."""
        params: dict[str, str | int] = {
            "repo": repo,
            "ci_run_id": ci_run_id,
            "run_attempt": run_attempt,
        }
        if variant is not None:
            params["variant"] = variant
        response = self._http.get("/runs/lookup", params=params)
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        found: Json = response.json()
        return found

    def upload(self, files: list[tuple[str, bytes]], metadata: Json) -> httpx.Response:
        return self._http.post(
            "/runs",
            files=[("files", (name, content, "application/xml")) for name, content in files],
            data={"metadata": json.dumps(metadata)},
        )


# --- backfill -----------------------------------------------------------------------------


def artifact_matches(name: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(name.lower(), pattern.lower())


def extract_xml(
    archive: bytes, prefix: str, budget: int = MAX_XML_BYTES
) -> list[tuple[str, bytes]]:
    """``(prefix/path, content)`` for every ``*.xml`` member of a zip archive.

    Sizes are checked from the zip directory before anything is decompressed.
    """
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        members = [
            info
            for info in zf.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".xml")
        ]
        total = sum(info.file_size for info in members)
        if total > budget:
            raise BackfillError(f"artifact {prefix!r} has {total} bytes of XML (limit {budget})")
        return [(f"{prefix}/{info.filename}", zf.read(info)) for info in members]


def run_metadata(
    repo: str,
    run: Json,
    default_branch: str,
    changed_files: list[str],
    changed_files_known: bool,
) -> Json:
    branch = run.get("head_branch") or "unknown"
    return {
        "repo": repo,
        "commit_sha": run["head_sha"],
        "branch": branch,
        # A pull_request run can report the default branch name (e.g. from a fork's main);
        # only non-PR runs on the default branch are "main".
        "is_main": branch == default_branch and run.get("event") not in PULL_REQUEST_EVENTS,
        "ci_run_id": str(run["id"]),
        "run_attempt": _attempt(run),
        "started_at": run.get("run_started_at") or run.get("created_at"),
        "changed_files": changed_files,
        "changed_files_known": changed_files_known,
    }


def _pick_pull_request(candidates: list[Json], run: Json) -> Json | None:
    """The PR a run tested: the one whose head is the run's commit, else the one that was
    open when the run started (a branch name like a fork's ``master`` can be reused by many
    PRs over time). None if neither applies."""
    head = run["head_sha"]
    for pr in candidates:
        if pr.get("head", {}).get("sha") == head:
            return pr
    started = run.get("run_started_at") or run.get("created_at")
    if not started:
        return None
    for pr in candidates:
        created, closed = pr.get("created_at"), pr.get("closed_at")
        # ISO-8601 UTC timestamps from the same API compare correctly as strings.
        if created and created <= started and (closed is None or started <= closed):
            return pr
    return None


def _attempt(run: Json) -> int:
    return int(run.get("run_attempt") or 1)


def artifact_variants(names: list[str], pattern: str) -> dict[str, str]:
    """Variant label per artifact name: the name minus the pattern's literal parts.

    ``3.12-ubuntu-latest-pytest-junit-xml`` with ``*-pytest-junit-xml`` -> ``3.12-ubuntu-latest``.
    Falls back to the full name when stripping leaves nothing or two artifacts of the run
    would get the same label (they'd otherwise dedupe into one run).
    """
    literals = [part for part in re.split(r"[*?]|\[[^\]]*\]", pattern.lower()) if part]
    labels: dict[str, str] = {}
    for name in names:
        label = name
        for literal in literals:
            i = label.lower().find(literal)
            if i >= 0:
                label = label[:i] + "-" + label[i + len(literal) :]
        label = re.sub(r"[-_. ]{2,}", "-", label).strip("-_. ")
        labels[name] = label or name
    if len(set(labels.values())) < len(labels):
        return {name: name for name in names}
    return labels


def backfill_run(
    github: GitHubClient,
    sieve: SieveClient,
    repo: str,
    run: Json,
    default_branch: str,
    pattern: str,
) -> list[tuple[str | None, Outcome, str]]:
    """Backfill one CI run: each matching artifact is uploaded as its own run (variant).

    Returns ``(variant, outcome, detail)`` per artifact, or one ``(None, ...)`` entry when
    the run has no matching artifact.
    """
    artifacts = [
        a for a in github.list_artifacts(repo, run["id"]) if artifact_matches(a["name"], pattern)
    ]
    if not artifacts:
        return [(None, Outcome.NO_ARTIFACTS, f"no artifact matches {pattern!r}")]

    variants = artifact_variants([a["name"] for a in artifacts], pattern)
    diff: list[tuple[list[str], bool]] = []  # fetched once, only if something is uploaded

    def changed_files() -> tuple[list[str], bool]:
        if not diff:
            diff.append(github.changed_files(repo, run))
        return diff[0]

    results: list[tuple[str | None, Outcome, str]] = []
    for artifact in artifacts:
        variant = variants[artifact["name"]]
        try:
            outcome, detail = _backfill_artifact(
                github, sieve, repo, run, default_branch, artifact, variant, changed_files
            )
        except (httpx.HTTPError, zipfile.BadZipFile, BackfillError) as exc:
            outcome, detail = Outcome.ERROR, f"{type(exc).__name__}: {exc}"
        results.append((variant, outcome, detail))
    return results


def _backfill_artifact(
    github: GitHubClient,
    sieve: SieveClient,
    repo: str,
    run: Json,
    default_branch: str,
    artifact: Json,
    variant: str,
    changed_files: Callable[[], tuple[list[str], bool]],
) -> tuple[Outcome, str]:
    # Ask sieve first, so re-runs skip ingested variants without downloading anything.
    stored = sieve.lookup(repo, str(run["id"]), _attempt(run), variant)
    if stored is not None:
        if stored.get("commit_sha") != run["head_sha"]:
            return Outcome.ERROR, (
                f"sieve has this run for commit {stored.get('commit_sha')}, "
                f"GitHub says {run['head_sha']}"
            )
        return Outcome.SKIPPED, "already ingested"

    archive = (
        None
        if artifact.get("expired")
        else github.download_artifact(artifact["archive_download_url"])
    )
    if archive is None:
        return Outcome.EXPIRED, f"artifact expired: {artifact['name']}"
    files = extract_xml(archive, artifact["name"])
    if not files:
        return Outcome.NO_ARTIFACTS, f"artifact {artifact['name']} contains no .xml files"

    changed, known = changed_files()
    metadata = {**run_metadata(repo, run, default_branch, changed, known), "variant": variant}
    response = sieve.upload(files, metadata)
    if response.status_code == httpx.codes.CREATED:
        return Outcome.NEW, f"{_result_total(response)} results from {len(files)} file(s)"
    if response.status_code == httpx.codes.OK:  # ingested concurrently since the lookup
        return Outcome.SKIPPED, "already ingested"
    return Outcome.ERROR, f"sieve returned {response.status_code}: {response.text[:300]}"


def _result_total(response: httpx.Response) -> str:
    try:
        return str(response.json()["counts"]["total"])
    except (ValueError, KeyError, TypeError):
        return "?"


def backfill(
    github: GitHubClient,
    sieve: SieveClient,
    repo: str,
    *,
    workflow: str | None = None,
    pattern: str = "*junit*",
    max_runs: int = 200,
    echo: Echo = typer.echo,
) -> Summary:
    """Backfill up to ``max_runs`` completed runs. Per-run failures are counted, not raised."""
    default_branch = github.default_branch(repo)
    summary = Summary()
    for run in github.iter_completed_runs(repo, workflow, max_runs):
        try:
            results = backfill_run(github, sieve, repo, run, default_branch, pattern)
        except (httpx.HTTPError, zipfile.BadZipFile, BackfillError) as exc:
            results = [(None, Outcome.ERROR, f"{type(exc).__name__}: {exc}")]
        label = (
            f"run {run['id']} #{_attempt(run)} "
            f"{str(run.get('head_sha', ''))[:7]} {run.get('head_branch')}"
        )
        for variant, outcome, detail in results:
            summary.add(outcome)
            echo(f"{label}{f' [{variant}]' if variant else ''}: {outcome.value} ({detail})")
    return summary


# --- CLI ----------------------------------------------------------------------------------

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    repo: Annotated[str, typer.Option(help="GitHub repository, owner/name.")],
    workflow: Annotated[
        str | None, typer.Option(help="Only runs of this workflow (file name like ci.yml, or id).")
    ] = None,
    artifact_pattern: Annotated[
        str, typer.Option(help="Glob for JUnit artifact names (case-insensitive).")
    ] = "*junit*",
    max_runs: Annotated[
        int, typer.Option(min=1, help="Most recent completed runs to process.")
    ] = 200,
    server: Annotated[str, typer.Option(help="sieve server URL.")] = "http://localhost:8000",
) -> None:
    """Backfill sieve from past GitHub Actions runs. Tokens: GITHUB_TOKEN, SIEVE_API_TOKEN."""
    github_token = os.environ.get("GITHUB_TOKEN")
    sieve_token = os.environ.get("SIEVE_API_TOKEN")
    if not github_token or not sieve_token:
        missing = [
            name
            for name, value in (("GITHUB_TOKEN", github_token), ("SIEVE_API_TOKEN", sieve_token))
            if not value
        ]
        typer.echo(f"error: set {' and '.join(missing)}", err=True)
        raise typer.Exit(2)
    if repo.count("/") != 1 or not all(repo.split("/")):
        typer.echo(f"error: --repo must be owner/name, got {repo!r}", err=True)
        raise typer.Exit(2)

    with GitHubClient(github_token) as github, SieveClient(server, sieve_token) as sieve:
        try:
            summary = backfill(
                github,
                sieve,
                repo,
                workflow=workflow,
                pattern=artifact_pattern,
                max_runs=max_runs,
            )
        except httpx.HTTPError as exc:
            typer.echo(f"error: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(2) from exc

    typer.echo(f"\nsummary: {summary}")
    if summary.counts[Outcome.ERROR]:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
