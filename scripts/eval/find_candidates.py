"""Find public repos that could serve as sieve eval repos (first pass, broad and cheap).

Code-searches workflows that upload JUnit XML, then keeps repos that are active, written in
``--language``, have live JUnit artifacts, enough human PRs and enough tests. For each it
samples one JUnit report and the failed runs among those artifacts.

    uv run python -m scripts.eval.find_candidates --language Python --out candidates.json

Needs GITHUB_TOKEN (env or .env). Code search allows ~10 requests/minute; the whole pass
takes several minutes. Follow up with ``scripts.eval.deep_check`` on the shortlist.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import httpx
import typer

from scripts.eval._github import (
    BUMP_PREFIXES,
    Json,
    client,
    github_token,
    is_bot,
    is_junit_artifact,
    junit_sample,
    since,
    ts,
)

QUERIES = (
    'path:.github/workflows "--junitxml" "upload-artifact" language:YAML',
    'path:.github/workflows "junit-xml" "pytest" "upload-artifact"',
    'path:.github/workflows "--junit-xml" "upload-artifact"',
    'path:.github/workflows "junitxml" "actions/upload-artifact@v4"',
)


def search(gh: httpx.Client, query: str) -> list[str]:
    response = gh.get("/search/code", params={"q": query, "per_page": 100})
    if response.status_code != httpx.codes.OK:
        typer.echo(f"search failed ({response.status_code}): {query}", err=True)
        return []
    return [item["repository"]["full_name"] for item in response.json()["items"]]


def pr_activity(gh: httpx.Client, repo: str, days: int) -> dict[str, int]:
    """PRs opened in the last ``days``: human vs bot, and human dependency bumps."""
    cutoff = since(days)
    counts = {"human": 0, "bot": 0, "human_bumps": 0}
    for page in range(1, 6):
        response = gh.get(
            f"/repos/{repo}/pulls",
            params={
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        if response.status_code != httpx.codes.OK:
            break
        prs: list[Json] = response.json()
        for pr in prs:
            if ts(pr["created_at"]) < cutoff:
                return counts
            if is_bot(pr):
                counts["bot"] += 1
            else:
                counts["human"] += 1
                counts["human_bumps"] += pr["title"].lower().startswith(BUMP_PREFIXES)
        if len(prs) < 100:
            break
    return counts


def failed_runs_with_junit(
    gh: httpx.Client, repo: str, artifacts: list[Json], days: int
) -> dict[str, int]:
    """Among runs (last ``days``) with JUnit artifacts: failed runs, and how many had failing
    tests in their JUnit (vs failing for lint, mypy, infrastructure...)."""
    cutoff = since(days)
    run_ids = sorted({a["workflow_run"]["id"] for a in artifacts if ts(a["created_at"]) >= cutoff})
    failed = []
    for run_id in run_ids[:60]:
        response = gh.get(f"/repos/{repo}/actions/runs/{run_id}")
        if (
            response.status_code == httpx.codes.OK
            and response.json().get("conclusion") == "failure"
        ):
            failed.append(run_id)
    with_test_failures = failing_tests = 0
    for run_id in failed[:5]:
        for artifact in [a for a in artifacts if a["workflow_run"]["id"] == run_id][:2]:
            sample = junit_sample(gh, artifact, max_bytes=30_000_000)
            if sample and sample.failures:
                with_test_failures += 1
                failing_tests += sample.failures
                break
    return {
        "runs_with_junit": len(run_ids),
        "failed_runs": len(failed),
        "failed_runs_checked": min(len(failed), 5),
        "failed_runs_with_test_failures": with_test_failures,
        "failing_tests_in_sample": failing_tests,
    }


def main(
    out: Annotated[Path, typer.Option(help="Where to write the JSON results.")] = Path(
        "eval-candidates.json"
    ),
    language: Annotated[str, typer.Option(help="GitHub's primary-language label.")] = "Python",
    days: Annotated[int, typer.Option(help="Activity window.")] = 90,
    min_human_prs: Annotated[int, typer.Option(help="Non-bump human PRs in the window.")] = 15,
    min_tests: Annotated[int, typer.Option(help="Tests in one sampled JUnit report.")] = 100,
) -> None:
    gh = client(github_token())
    candidates: list[str] = []
    for query in QUERIES:
        candidates.extend(name for name in search(gh, query) if name not in candidates)
    typer.echo(f"{len(candidates)} repos from code search", err=True)

    results: list[Json] = []
    for repo in candidates:
        info = gh.get(f"/repos/{repo}")
        if info.status_code != httpx.codes.OK:
            continue
        meta: Json = info.json()
        if meta.get("archived") or meta.get("fork") or meta.get("language") != language:
            continue
        if ts(meta["pushed_at"]) < since(days):
            continue
        artifacts_response = gh.get(f"/repos/{repo}/actions/artifacts", params={"per_page": 100})
        if artifacts_response.status_code != httpx.codes.OK:
            continue
        artifacts = [
            a for a in artifacts_response.json().get("artifacts", []) if is_junit_artifact(a)
        ]
        if len(artifacts) < 5:
            continue
        prs = pr_activity(gh, repo, days)
        if prs["human"] - prs["human_bumps"] < min_human_prs:
            continue
        sample = junit_sample(gh, artifacts[0], max_bytes=30_000_000)
        if sample is None or sample.tests < min_tests:
            continue
        row: Json = {
            "repo": repo,
            "stars": meta["stargazers_count"],
            "junit_artifacts_live": len(artifacts),
            "artifact_names": sorted({a["name"] for a in artifacts})[:4],
            "sample": {
                "tests": sample.tests,
                "median_s": sample.median_s,
                "integration_pct": sample.integration_pct,
            },
            "prs": prs,
            "failures": failed_runs_with_junit(gh, repo, artifacts, days),
        }
        results.append(row)
        typer.echo(json.dumps(row), err=True)

    out.write_text(json.dumps(results, indent=2))
    typer.echo(f"{len(results)} repos passed the filters; written to {out}", err=True)


if __name__ == "__main__":
    typer.run(main)
