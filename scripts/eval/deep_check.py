"""Deeper evidence for shortlisted eval repos (second pass, after ``find_candidates``).

For each repo: which workflow uploads the JUnit artifacts, how far back live artifacts go,
how many of that workflow's runs failed in the window and whether their JUnit shows real test
failures, and whether recent human PRs actually change Python source.

    uv run python -m scripts.eval.deep_check RDFLib/rdflib chunkhound/chunkhound --out deep.json

Needs GITHUB_TOKEN (env or .env).
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Annotated

import httpx
import typer

from scripts.eval._github import (
    Json,
    client,
    github_token,
    is_bot,
    is_junit_artifact,
    junit_sample,
    paged,
    since,
    ts,
)


def run_test_failures(gh: httpx.Client, repo: str, run_id: int) -> tuple[int, int] | None:
    """(tests, failing tests) across up to 3 JUnit artifacts of a run; None if it has none."""
    response = gh.get(f"/repos/{repo}/actions/runs/{run_id}/artifacts")
    artifacts = [a for a in response.json().get("artifacts", []) if is_junit_artifact(a)]
    if not artifacts:
        return None
    tests = failures = 0
    for artifact in artifacts[:3]:
        sample = junit_sample(gh, artifact)
        if sample:
            tests += sample.tests
            failures += sample.failures
    return tests, failures


def check(gh: httpx.Client, repo: str, days: int, failed_runs: int, pr_sample: int) -> Json:
    cutoff = since(days)
    meta = gh.get(f"/repos/{repo}").json()
    junit = [
        a
        for a in paged(gh, f"/repos/{repo}/actions/artifacts", "artifacts", 5)
        if is_junit_artifact(a)
    ]
    workflows: Counter[int] = Counter()
    for artifact in junit[:30]:
        run = gh.get(f"/repos/{repo}/actions/runs/{artifact['workflow_run']['id']}").json()
        if "workflow_id" in run:
            workflows[run["workflow_id"]] += 1
    if not workflows:
        return {"error": "no workflow with live JUnit artifacts"}
    workflow_id = workflows.most_common(1)[0][0]
    workflow = gh.get(f"/repos/{repo}/actions/workflows/{workflow_id}").json()

    runs = [
        r
        for r in paged(
            gh,
            f"/repos/{repo}/actions/workflows/{workflow_id}/runs",
            "workflow_runs",
            5,
            status="completed",
        )
        if ts(r["created_at"]) >= cutoff
    ]
    failed = [r for r in runs if r["conclusion"] == "failure"]
    checked = []
    for run in failed[:failed_runs]:
        result = run_test_failures(gh, repo, run["id"])
        if result is not None:
            checked.append(
                {
                    "run": run["id"],
                    "event": run["event"],
                    "tests": result[0],
                    "failing_tests": result[1],
                }
            )

    prs = [
        p
        for p in paged(
            gh, f"/repos/{repo}/pulls", None, 3, state="all", sort="created", direction="desc"
        )
        if ts(p["created_at"]) >= cutoff
    ]
    human = [p for p in prs if not is_bot(p)]
    sample = []
    for pr in human[:pr_sample]:
        names = [
            f["filename"] for f in paged(gh, f"/repos/{repo}/pulls/{pr['number']}/files", None, 1)
        ]
        sample.append(
            {
                "number": pr["number"],
                "files": len(names),
                "python_source": sum(n.endswith(".py") and "test" not in n.lower() for n in names),
                "python_tests": sum(n.endswith(".py") and "test" in n.lower() for n in names),
            }
        )

    return {
        "stars": meta["stargazers_count"],
        "workflow": workflow.get("path"),
        "junit_artifact_names": sorted({a["name"] for a in junit})[:3],
        # Lower bound: only the 500 most recent artifacts are listed.
        "oldest_live_junit_artifact": min((a["created_at"] for a in junit), default=None),
        "workflow_runs": len(runs),
        "events": dict(Counter(r["event"] for r in runs)),
        "failed_runs": len(failed),
        "failed_runs_checked": checked,
        "prs": len(prs),
        "human_prs": len(human),
        "distinct_human_authors": len({p["user"]["login"] for p in human}),
        "top_authors": Counter(p["user"]["login"] for p in human).most_common(3),
        "human_pr_sample": sample,
    }


def main(
    repos: Annotated[list[str], typer.Argument(help="owner/name ...")],
    out: Annotated[Path, typer.Option(help="Where to write the JSON results.")] = Path(
        "eval-deep-check.json"
    ),
    days: Annotated[int, typer.Option(help="Activity window.")] = 90,
    failed_runs: Annotated[int, typer.Option(help="Failed runs to inspect per repo.")] = 12,
    pr_sample: Annotated[int, typer.Option(help="Human PRs to inspect per repo.")] = 15,
) -> None:
    gh = client(github_token())
    report: dict[str, Json] = {}
    for repo in repos:
        report[repo] = check(gh, repo, days, failed_runs, pr_sample)
        typer.echo(f"{repo}: done", err=True)
    out.write_text(json.dumps(report, indent=2))
    typer.echo(f"written to {out}", err=True)


if __name__ == "__main__":
    typer.run(main)
