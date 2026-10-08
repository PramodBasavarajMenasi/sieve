"""Replay the selector against real PR runs and measure what it would have caught.

For each evaluated run R (a non-main run with ``changed_files_known``):

1. Rebuild the selector's history *as of just before R*: only runs strictly earlier than R by
   ``COALESCE(started_at, created_at)`` (ties broken by id). ``test_stats`` is not used,
   because it reflects every run including later ones. Broken-on-main is recomputed from raw
   results instead.
2. Run ``select_tests`` on R's changed files.
3. Compare with the tests that actually failed in R (final attempt failed or error). A failure
   counts as caught if the selection's commands would run it: full mode runs everything; Go
   ``-run`` selects by top-level test per package, so a failing subtest is caught when its
   top-level test is selected; other runners need the exact test.
4. For each missed failure, explain which signal would have been needed.

    uv run python scripts/eval_kubo.py                 # 10 most recent PR runs
    uv run python scripts/eval_kubo.py --failing       # plus every PR run that had failures

Reads SIEVE_DATABASE_URL (or .env); read-only.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

import typer
from sqlalchemy import text
from sqlalchemy.orm import Session

from sieve.core.selector import (
    FailedRun,
    KnownTest,
    Mode,
    RepoHistory,
    Runner,
    Selection,
    SelectorConfig,
    is_build_file,
    is_ignorable,
    select_tests,
)
from sieve.db import get_sessionmaker

FAILED = ("failed", "error")


@dataclass(frozen=True)
class TargetRun:
    run_id: int
    ci_run_id: str | None
    commit_sha: str
    branch: str
    run_at: datetime


@dataclass(frozen=True)
class Missed:
    test_id: str
    known_before: bool
    why: str


@dataclass(frozen=True)
class Counterfactual:
    """The selection if the full-suite fallbacks had not applied."""

    mapped_files: list[str]
    selection: Selection | None  # None: no changed file maps to tests on its own
    caught: list[str]
    missed: list[Missed]


@dataclass(frozen=True)
class Result:
    run: TargetRun
    changed_files: list[str]
    selection: Selection
    failures: list[str]
    caught: list[str]
    missed: list[Missed]
    counterfactual: Counterfactual | None = None


# --- history as of a point in time --------------------------------------------------------

# Runs of the repo strictly before (:at, :run_id). Every history query starts from this.
_PRIOR = """
prior AS (
    SELECT id, commit_sha, is_main, changed_files_known,
           COALESCE(started_at, created_at) AS run_at
    FROM runs
    WHERE repo_id = :repo_id
      AND (COALESCE(started_at, created_at), id) < (CAST(:at AS timestamptz), :run_id)
)
"""


def history_before(
    session: Session, repo_id: int, run: TargetRun, config: SelectorConfig
) -> RepoHistory:
    """What ``load_history`` would have returned just before ``run`` was ingested."""
    params: dict[str, Any] = {"repo_id": repo_id, "at": run.run_at, "run_id": run.run_id}

    known = session.execute(
        text(
            f"""
            WITH {_PRIOR},
            recent AS (SELECT id FROM prior ORDER BY run_at DESC, id DESC LIMIT :n)
            SELECT DISTINCT ON (tr.test_id) tr.test_id, tr.file_path
            FROM test_results tr JOIN recent ON recent.id = tr.run_id
            ORDER BY tr.test_id, tr.file_path IS NULL, tr.run_id DESC
            """
        ),
        {**params, "n": config.known_test_runs},
    ).all()

    recent_failures = session.execute(
        text(
            f"""
            WITH {_PRIOR},
            recent AS (
                SELECT id, commit_sha, run_at FROM prior WHERE is_main
                ORDER BY run_at DESC, id DESC LIMIT :n
            )
            SELECT DISTINCT ON (tr.test_id) tr.test_id, recent.id, recent.commit_sha
            FROM recent JOIN test_results tr ON tr.run_id = recent.id
            WHERE tr.status IN ('failed', 'error')
            ORDER BY tr.test_id, recent.run_at DESC, recent.id DESC
            """
        ),
        {**params, "n": config.recent_main_runs},
    ).all()

    failed = session.execute(
        text(
            f"""
            WITH {_PRIOR},
            recent AS (
                SELECT id, commit_sha, run_at FROM prior WHERE changed_files_known
                ORDER BY run_at DESC, id DESC LIMIT :n
            )
            SELECT recent.id, recent.commit_sha, array_agg(DISTINCT tr.test_id)
            FROM recent JOIN test_results tr ON tr.run_id = recent.id
            WHERE tr.status IN ('failed', 'error')
            GROUP BY recent.id, recent.commit_sha, recent.run_at
            ORDER BY recent.run_at DESC, recent.id DESC
            """
        ),
        {**params, "n": config.co_change_runs},
    ).all()
    changed = changed_files_of(session, [row[0] for row in failed])

    return RepoHistory(
        tests={test_id: KnownTest(test_id, file_path) for test_id, file_path in known},
        broken_on_main=broken_on_main_before(session, params),
        recent_main_failures={test_id: (rid, sha) for test_id, rid, sha in recent_failures},
        failed_runs=tuple(
            FailedRun(rid, sha, frozenset(changed.get(rid, ())), frozenset(test_ids))
            for rid, sha, test_ids in failed
        ),
    )


def broken_on_main_before(session: Session, params: dict[str, Any]) -> dict[str, str]:
    """Same definition as core/history.py, computed from main runs before the cutoff only."""
    rows = session.execute(
        text(
            f"""
            WITH {_PRIOR}
            SELECT DISTINCT ON (tr.test_id, prior.id)
                   tr.test_id, prior.id, prior.run_at, prior.commit_sha, tr.status
            FROM prior JOIN test_results tr ON tr.run_id = prior.id
            WHERE prior.is_main
            ORDER BY tr.test_id, prior.id, tr.attempt DESC, tr.id DESC
            """
        ),
        params,
    ).all()
    outcomes: dict[str, list[tuple[datetime, int, str, str]]] = defaultdict(list)
    for test_id, run_id, run_at, sha, status in rows:
        if status != "skipped":
            outcomes[test_id].append((run_at, run_id, sha, status))
    broken: dict[str, str] = {}
    for test_id, history in outcomes.items():
        history.sort(reverse=True)  # newest first
        streak_start = None
        for _, _, sha, status in history:
            if status not in FAILED:
                break
            streak_start = sha
        if streak_start is not None:
            broken[test_id] = streak_start
    return broken


def changed_files_of(session: Session, run_ids: list[int]) -> dict[int, set[str]]:
    changed: dict[int, set[str]] = defaultdict(set)
    if run_ids:
        for run_id, path in session.execute(
            text("SELECT run_id, path FROM changed_files WHERE run_id = ANY(:ids)"),
            {"ids": run_ids},
        ).all():
            changed[run_id].add(path)
    return changed


# --- evaluation ---------------------------------------------------------------------------


def target_runs(session: Session, repo_id: int, limit: int, failing: bool) -> list[TargetRun]:
    rows = session.execute(
        text(
            """
            SELECT r.id, r.ci_run_id, r.commit_sha, r.branch,
                   COALESCE(r.started_at, r.created_at) AS run_at
            FROM runs r
            WHERE r.repo_id = :repo_id AND NOT r.is_main AND r.changed_files_known
              AND (NOT :failing OR EXISTS (
                  SELECT 1 FROM test_results tr
                  WHERE tr.run_id = r.id AND tr.status IN ('failed', 'error')))
            ORDER BY run_at DESC, r.id DESC
            LIMIT :limit
            """
        ),
        {"repo_id": repo_id, "limit": limit, "failing": failing},
    ).all()
    return [TargetRun(*row) for row in rows]


def actual_failures(session: Session, run_id: int) -> list[str]:
    rows = session.execute(
        text(
            """
            SELECT test_id FROM (
                SELECT DISTINCT ON (test_id) test_id, status FROM test_results
                WHERE run_id = :run_id ORDER BY test_id, attempt DESC, id DESC
            ) final WHERE status IN ('failed', 'error') ORDER BY test_id
            """
        ),
        {"run_id": run_id},
    ).all()
    return [row[0] for row in rows]


def would_run(selection: Selection, test_id: str) -> bool:
    """Whether the selection's commands would execute ``test_id``."""
    if selection.mode is Mode.FULL:
        return True
    selected = {t.test_id: t for t in selection.tests}
    if test_id in selected:
        return True
    package, _, name = test_id.partition("::")
    if package in selection.go_packages_run_whole:
        return True  # `go test ./pkg` with no -run: every test in the package, new ones too
    top_level = name.split("/", 1)[0]
    # go test -run '^(TestX)$' runs TestX with all of its subtests.
    return any(
        t.runner is Runner.GO
        and t.test_id.partition("::")[0] == package
        and t.test_id.partition("::")[2].split("/", 1)[0] == top_level
        for t in selection.tests
    )


def explain_miss(
    test_id: str, history: RepoHistory, changed_files: list[str], go_module: str | None
) -> Missed:
    package, _, name = test_id.partition("::")
    rel = (
        package[len(go_module) :].lstrip("/")
        if go_module and package.startswith(go_module)
        else package
    )
    changed_dirs = sorted({posixpath.dirname(p) for p in changed_files if p.endswith(".go")})
    known_before = test_id in history.tests
    top_level_known = any(
        t.partition("::")[0] == package
        and t.partition("::")[2].split("/", 1)[0] == name.split("/", 1)[0]
        for t in history.tests
    )
    if not known_before and not top_level_known:
        if rel in changed_dirs:
            why = (
                f"new test in changed package ./{rel}: history can't name it, and -run only "
                "lists known tests. Running changed packages without -run would catch it."
            )
        else:
            why = (
                f"new test; ./{rel} is not in this run's recorded diff (for multi-commit PRs "
                "backfill records only the head commit's diff, not the whole PR)"
            )
    else:
        dirs = ", ".join(f"./{d}" for d in changed_dirs) or "(no Go files changed)"
        why = f"dependent: ./{rel} has no changed files; changed Go packages: {dirs}."
        if rel.startswith("test/cli"):
            why += (
                " test/cli runs the built ipfs binary, so it depends on every package linked "
                "into ./cmd/ipfs."
            )
        else:
            why += f" An import graph would select it if ./{rel} imports a changed package."
    return Missed(test_id, known_before, why)


def counterfactual(
    history: RepoHistory, changed: list[str], failures: list[str], config: SelectorConfig
) -> Counterfactual:
    """Select from only the changed files that map to tests on their own.

    Shows what path mapping and the other signals catch once the fallbacks no longer apply,
    e.g. after someone adds a test to a package that has none today.
    """
    mapped = [
        p
        for p in changed
        if not is_build_file(p)
        and not is_ignorable(p)
        and select_tests(history, [p], True, config).mode is Mode.SELECTIVE
    ]
    if not mapped:
        return Counterfactual([], None, [], [])
    selection = select_tests(history, mapped, True, config)
    caught = [f for f in failures if would_run(selection, f)]
    missed = [
        explain_miss(f, history, changed, config.go_module) for f in failures if f not in caught
    ]
    return Counterfactual(mapped, selection, caught, missed)


def evaluate(session: Session, repo_id: int, run: TargetRun, config: SelectorConfig) -> Result:
    history = history_before(session, repo_id, run, config)
    changed = sorted(changed_files_of(session, [run.run_id]).get(run.run_id, set()))
    selection = select_tests(history, changed, True, config)
    failures = actual_failures(session, run.run_id)
    caught = [f for f in failures if would_run(selection, f)]
    missed = [
        explain_miss(f, history, changed, config.go_module) for f in failures if f not in caught
    ]
    cf = (
        counterfactual(history, changed, failures, config)
        if failures and selection.mode is Mode.FULL
        else None
    )
    return Result(run, changed, selection, failures, caught, missed, cf)


# --- report -------------------------------------------------------------------------------


def print_result(r: Result) -> None:
    s = r.selection
    skipped = 100 * (1 - s.selected_count / s.total_known) if s.total_known else 0.0
    typer.echo(
        f"\nrun {r.run.run_id} (CI {r.run.ci_run_id}) {r.run.commit_sha[:7]} "
        f"{r.run.branch} @ {r.run.run_at:%Y-%m-%d %H:%M}"
    )
    files = ", ".join(r.changed_files[:6]) + (
        f" (+{len(r.changed_files) - 6})" if len(r.changed_files) > 6 else ""
    )
    typer.echo(f"  changed ({len(r.changed_files)}): {files or '(none)'}")
    typer.echo(f"  {s.mode.value}: {s.reason}")
    typer.echo(f"  selected {s.selected_count} / {s.total_known} known ({skipped:.1f}% skipped)")
    if s.mode is Mode.SELECTIVE and s.commands:
        typer.echo(f"  $ {s.command[:160]}{'...' if len(s.command) > 160 else ''}")
    if not r.failures:
        typer.echo("  failures: none")
        return
    typer.echo(f"  failures: {len(r.failures)} ({len(r.caught)} caught, {len(r.missed)} missed)")
    for test_id in r.caught:
        typer.echo(f"    CAUGHT  {test_id}")
    for m in r.missed:
        typer.echo(f"    MISSED  {m.test_id}{'' if m.known_before else '  [new test]'}")
        typer.echo(f"            {m.why}")
    cf = r.counterfactual
    if cf is None:
        return
    if cf.selection is None:
        typer.echo(
            "  without fallbacks: no changed file maps to tests on its own, so only the "
            "fallback protects this run"
        )
        return
    typer.echo(
        f"  without fallbacks (mapped: {', '.join(cf.mapped_files)}): "
        f"{cf.selection.selected_count} tests, would catch {len(cf.caught)}/{len(r.failures)}"
    )
    for m in cf.missed:
        typer.echo(f"    WOULD MISS  {m.test_id}")
        typer.echo(f"                {m.why}")


def print_summary(title: str, results: list[Result]) -> None:
    if not results:
        return
    full = [r for r in results if r.selection.mode is Mode.FULL]
    selective = [r for r in results if r.selection.mode is Mode.SELECTIVE]
    skipped = [
        100 * (1 - r.selection.selected_count / r.selection.total_known)
        for r in selective
        if r.selection.total_known
    ]
    failures = sum(len(r.failures) for r in results)
    caught = sum(len(r.caught) for r in results)
    typer.echo(f"\n=== {title}: {len(results)} runs ===")
    typer.echo(
        f"  full: {len(full)} ({100 * len(full) / len(results):.0f}%), "
        f"selective: {len(selective)} ({100 * len(selective) / len(results):.0f}%)"
    )
    reasons: dict[str, int] = defaultdict(int)
    for r in full:
        reasons[r.selection.reason.split(":")[0]] += 1
    for reason, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        typer.echo(f"    full because {reason}: {n}")
    if skipped:
        typer.echo(
            f"  tests skipped in selective runs: avg {sum(skipped) / len(skipped):.1f}% "
            f"(min {min(skipped):.1f}%, max {max(skipped):.1f}%)"
        )
    typer.echo(
        f"  failures: {failures} total, {caught} caught, {failures - caught} missed"
        + (f" ({100 * caught / failures:.0f}% recall)" if failures else "")
    )
    full_runs_with_failures = sum(1 for r in full if r.failures)
    if full_runs_with_failures:
        typer.echo(f"    ({full_runs_with_failures} of the runs with failures went full)")
    cfs = [r for r in results if r.counterfactual is not None]
    if cfs:
        mapped = [r for r in cfs if r.counterfactual and r.counterfactual.selection]
        cf_failures = sum(len(r.failures) for r in mapped)
        cf_caught = sum(len(r.counterfactual.caught) for r in mapped if r.counterfactual)
        unprotected = sum(
            len(r.failures) for r in cfs if r.counterfactual and not r.counterfactual.selection
        )
        typer.echo(
            f"  without fallbacks: {cf_caught}/{cf_failures} failures caught in "
            f"{len(mapped)} runs with mappable files; {unprotected} more failures in "
            f"{len(cfs) - len(mapped)} runs where only a fallback applied"
        )


def main(
    repo: Annotated[str, typer.Option(help="Repository name in sieve.")] = "ipfs/kubo",
    runs: Annotated[int, typer.Option(min=1, help="Most recent PR runs to evaluate.")] = 10,
    failing: Annotated[
        bool, typer.Option("--failing", help="Also evaluate every PR run that had failures.")
    ] = False,
    go_module: Annotated[str, typer.Option(help="Go module path.")] = "github.com/ipfs/kubo",
) -> None:
    config = SelectorConfig(go_module=go_module or None)
    with get_sessionmaker()() as session:
        repo_id = session.execute(
            text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
        ).scalar_one_or_none()
        if repo_id is None:
            typer.echo(f"error: unknown repo {repo!r}", err=True)
            raise typer.Exit(2)

        typer.echo(f"# {runs} most recent PR runs of {repo} (changed_files_known)")
        recent = [
            evaluate(session, repo_id, r, config)
            for r in target_runs(session, repo_id, runs, False)
        ]
        for result in recent:
            print_result(result)

        failing_results: list[Result] = []
        if failing:
            typer.echo(f"\n# every PR run of {repo} with failures")
            seen = {r.run.run_id for r in recent}
            for target in target_runs(session, repo_id, 10_000, True):
                result = next(
                    (r for r in recent if r.run.run_id == target.run_id), None
                ) or evaluate(session, repo_id, target, config)
                failing_results.append(result)
                if target.run_id not in seen:
                    print_result(result)
                else:
                    typer.echo(f"\nrun {target.run_id}: shown above")

    print_summary("most recent PR runs", recent)
    print_summary("PR runs with failures", failing_results)


if __name__ == "__main__":
    typer.run(main)
