"""Replay the selector against real runs and measure what it would have caught and saved.

Two modes:

* default: the most recent PR runs (non-main, ``changed_files_known``), using their recorded
  diffs. ``--failing`` adds every PR run that had failures.
* ``--replay-main``: treat each main run as a PR whose diff is the previous main run's
  commit ... this commit (fetched from the GitHub compare API and cached).

For each evaluated run R:

1. Rebuild the selector's history *as of just before R*: only runs strictly earlier than R by
   ``COALESCE(started_at, created_at)`` (ties broken by id). ``test_stats`` is not used for
   selection, because it reflects every run including later ones; broken-on-main is
   recomputed from raw results instead.
2. Optionally compute the Go import graph *at R's commit* (``--kubo-checkout`` + ``--go``):
   check out the commit and run ``go list -deps -test -json ./...``, cached per commit. Without
   a checkout, dependents are not selected and the report says so.
3. Run ``select_tests`` with the repo config from ``--sieve-toml``.
4. Compare with the tests that actually failed in R (final attempt failed or error). A failure
   counts as caught if the selection's commands would run it: full mode runs everything; a
   whole-run Go package runs every test in it; ``-run`` selects by top-level test.
5. Report tests and estimated runtime skipped. Runtime uses each top-level test's average
   duration from test_stats (all history); this only sizes the savings and never affects
   selection.

    uv run python scripts/eval_kubo.py --failing --sieve-toml scripts/eval/kubo.sieve.toml
    uv run python scripts/eval_kubo.py --replay-main --kubo-checkout ../kubo --go go

Reads SIEVE_DATABASE_URL (or .env), and GITHUB_TOKEN for --replay-main. Read-only.
"""

from __future__ import annotations

import itertools
import json
import os
import posixpath
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import httpx
import typer
from sqlalchemy import text
from sqlalchemy.orm import Session

from sieve.cli.gograph import GoGraph, build_graph, parse_go_list
from sieve.cli.repoconfig import load_repo_config
from sieve.core.selector import (
    FailedRun,
    KnownTest,
    Mode,
    RepoHistory,
    Runner,
    Selection,
    SelectorConfig,
    build_file_scope,
    is_build_file,
    is_ignorable,
    select_tests,
)
from sieve.db import get_sessionmaker

FAILED = ("failed", "error")
COMPARE_FILE_CAP = 300


@dataclass(frozen=True)
class TargetRun:
    run_id: int
    ci_run_id: str | None
    commit_sha: str
    branch: str
    run_at: datetime
    # None: unknown diff. For PR mode this comes from the DB; for --replay-main from GitHub.
    changed_files: tuple[str, ...] | None = None
    diff_label: str = ""
    # Every stored run (one per matrix variant) of this CI run attempt; run_id is the first.
    run_ids: tuple[int, ...] = ()


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
    graph_note: str
    tests_skipped_pct: float
    time_skipped_pct: float
    counterfactual: Counterfactual | None = None
    signal_counts: dict[str, int] = field(default_factory=dict)


# --- history as of a point in time --------------------------------------------------------

# Runs of the repo strictly before (:at, :run_id). Every history query starts from this.
_PRIOR = """
prior AS (
    SELECT id, commit_sha, is_main, changed_files_known, variant,
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
    """Same definition as core/history.py, computed from main runs before the cutoff only.

    Per variant: a test is broken if any variant's latest main outcome is a failure; the sha
    is the earliest streak start across broken variants.
    """
    rows = session.execute(
        text(
            f"""
            WITH {_PRIOR}
            SELECT DISTINCT ON (tr.test_id, prior.id)
                   tr.test_id, COALESCE(prior.variant, ''), prior.id, prior.run_at,
                   prior.commit_sha, tr.status
            FROM prior JOIN test_results tr ON tr.run_id = prior.id
            WHERE prior.is_main
            ORDER BY tr.test_id, prior.id, tr.attempt DESC, tr.id DESC
            """
        ),
        params,
    ).all()
    outcomes: dict[tuple[str, str], list[tuple[datetime, int, str, str]]] = defaultdict(list)
    for test_id, variant, run_id, run_at, sha, status in rows:
        if status != "skipped":
            outcomes[(test_id, variant)].append((run_at, run_id, sha, status))
    starts: dict[str, tuple[datetime, int, str]] = {}
    for (test_id, _), history in outcomes.items():
        history.sort(reverse=True)  # newest first
        streak_start = None
        for run_at, run_id, sha, status in history:
            if status not in FAILED:
                break
            streak_start = (run_at, run_id, sha)
        if streak_start is not None and (test_id not in starts or streak_start < starts[test_id]):
            starts[test_id] = streak_start
    return {test_id: start[2] for test_id, start in starts.items()}


def changed_files_of(session: Session, run_ids: list[int]) -> dict[int, set[str]]:
    changed: dict[int, set[str]] = defaultdict(set)
    if run_ids:
        for run_id, path in session.execute(
            text("SELECT run_id, path FROM changed_files WHERE run_id = ANY(:ids)"),
            {"ids": run_ids},
        ).all():
            changed[run_id].add(path)
    return changed


def actual_failures(session: Session, run_ids: list[int]) -> list[str]:
    """Tests whose final attempt failed in any of these runs (any variant of a CI run)."""
    rows = session.execute(
        text(
            """
            SELECT DISTINCT test_id FROM (
                SELECT DISTINCT ON (run_id, test_id) test_id, status FROM test_results
                WHERE run_id = ANY(:run_ids) ORDER BY run_id, test_id, attempt DESC, id DESC
            ) final WHERE status IN ('failed', 'error') ORDER BY test_id
            """
        ),
        {"run_ids": run_ids},
    ).all()
    return [row[0] for row in rows]


def top_level_durations(session: Session, repo_id: int) -> dict[str, float]:
    """Average duration of each top-level test (Go subtests are inside their parent's time)."""
    rows = session.execute(
        text(
            "SELECT test_id, avg_duration_ms FROM test_stats "
            "WHERE repo_id = :repo_id AND avg_duration_ms IS NOT NULL"
        ),
        {"repo_id": repo_id},
    ).all()
    return {t: d for t, d in rows if "/" not in t.partition("::")[2]}


# --- targets ------------------------------------------------------------------------------


# One row per CI run attempt: its variants (matrix legs) are grouped together. Runs without
# a CI id stand alone. run_at/id are the earliest, so history cutoffs exclude every variant.
_CI_RUNS = """
SELECT min(r.id) AS id, r.ci_run_id, r.commit_sha, r.branch,
       min(COALESCE(r.started_at, r.created_at)) AS run_at,
       array_agg(r.id ORDER BY r.id) AS run_ids
FROM runs r
WHERE r.repo_id = :repo_id AND {where}
GROUP BY COALESCE(r.ci_run_id, r.id::text), r.run_attempt, r.ci_run_id, r.commit_sha, r.branch
{having}
"""


def pr_targets(session: Session, repo_id: int, limit: int, failing: bool) -> list[TargetRun]:
    query = _CI_RUNS.format(
        where="NOT r.is_main AND r.changed_files_known",
        having="""HAVING NOT :failing OR bool_or(EXISTS (
            SELECT 1 FROM test_results tr
            WHERE tr.run_id = r.id AND tr.status IN ('failed', 'error')))""",
    )
    rows = session.execute(
        text(f"{query} ORDER BY run_at DESC, id DESC LIMIT :limit"),
        {"repo_id": repo_id, "limit": limit, "failing": failing},
    ).all()
    changed = changed_files_of(session, [row[0] for row in rows])
    return [
        TargetRun(
            run_id, ci_run_id, sha, branch, run_at,
            changed_files=tuple(sorted(changed.get(run_id, ()))),
            diff_label="PR diff",
            run_ids=tuple(run_ids),
        )
        for run_id, ci_run_id, sha, branch, run_at, run_ids in rows
    ]  # fmt: skip


def main_replay_targets(
    session: Session, repo_id: int, repo: str, compare: GitHubCompare
) -> list[TargetRun]:
    """Every main run but the first, as a PR from the previous main run's commit."""
    query = _CI_RUNS.format(where="r.is_main", having="")
    rows = session.execute(text(f"{query} ORDER BY run_at, id"), {"repo_id": repo_id}).all()
    targets = []
    for prev, (run_id, ci_run_id, sha, branch, run_at, run_ids) in itertools.pairwise(rows):
        prev_sha = prev[2]
        if prev_sha == sha:
            continue  # a re-run of the same commit: nothing changed
        files = compare.files(repo, prev_sha, sha)
        label = f"{prev_sha[:7]}...{sha[:7]}"
        targets.append(
            TargetRun(
                run_id,
                ci_run_id,
                sha,
                branch,
                run_at,
                changed_files=files,
                diff_label=label,
                run_ids=tuple(run_ids),
            )
        )
    return targets


class GitHubCompare:
    """Changed files between two commits, from the GitHub compare API, cached on disk."""

    def __init__(self, token: str, cache_dir: Path) -> None:
        self._cache_dir = cache_dir
        self._http = httpx.Client(
            base_url="https://api.github.com",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=60,
        )

    def files(self, repo: str, base: str, head: str) -> tuple[str, ...] | None:
        cache = self._cache_dir / f"compare-{base}-{head}.json"
        if cache.exists():
            data = json.loads(cache.read_text())
        else:
            response = self._http.get(f"/repos/{repo}/compare/{base}...{head}")
            if response.status_code != httpx.codes.OK:
                return None  # not cached: a later run may succeed
            data = response.json()
            cache.write_text(json.dumps({"files": data.get("files", [])}))
        entries = data.get("files", [])
        if len(entries) >= COMPARE_FILE_CAP:
            return None  # truncated
        paths: list[str] = []
        for entry in entries:
            paths.append(entry["filename"])
            if entry.get("previous_filename"):
                paths.append(entry["previous_filename"])
        return tuple(sorted(set(paths)))


class GraphProvider:
    """The Go import graph at a commit, via `go list` in a checkout, cached per commit."""

    def __init__(self, checkout: Path | None, go: str, cache_dir: Path) -> None:
        self.checkout = checkout
        self.go = go
        self.cache_dir = cache_dir

    def graph_at(self, sha: str) -> tuple[GoGraph | None, str]:
        cache = self.cache_dir / f"gograph-{sha}.json"
        if cache.exists():
            data = json.loads(cache.read_text())
            if "error" in data:
                return None, f"go list failed at {sha[:7]}: {data['error']}"
            return GoGraph.from_json(data), f"import graph at {sha[:7]}"
        if self.checkout is None:
            return None, "no import graph (no --kubo-checkout)"
        if not self._git("cat-file", "-e", f"{sha}^{{commit}}"):
            self._git("fetch", "-q", "origin", sha)
        if not self._git("checkout", "-q", "-f", "--detach", sha):
            return None, f"could not check out {sha[:7]}"
        proc = subprocess.run(
            [self.go, "list", "-deps", "-test", "-json", "./..."],
            cwd=self.checkout,
            capture_output=True,
            timeout=1800,
            check=False,
        )
        if proc.returncode != 0:
            error = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or ["?"]
            cache.write_text(json.dumps({"error": error[0][:300]}))
            return None, f"go list failed at {sha[:7]}: {error[0][:120]}"
        graph = build_graph(parse_go_list(proc.stdout.decode(errors="replace")), self.checkout)
        cache.write_text(json.dumps(graph.to_json()))
        return graph, f"import graph at {sha[:7]}"

    def _git(self, *args: str) -> bool:
        assert self.checkout is not None
        proc = subprocess.run(
            ["git", "-C", str(self.checkout), *args], capture_output=True, timeout=600, check=False
        )
        return proc.returncode == 0


# --- evaluation ---------------------------------------------------------------------------


def would_run(selection: Selection, test_id: str) -> bool:
    """Whether the selection's commands would execute ``test_id``."""
    if selection.mode is Mode.FULL:
        return True
    if any(t.test_id == test_id for t in selection.tests):
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
    test_id: str,
    history: RepoHistory,
    changed_files: list[str],
    go_module: str | None,
    graph: GoGraph | None,
) -> Missed:
    package, _, name = test_id.partition("::")
    rel = (
        package[len(go_module) :].lstrip("/")
        if go_module and package.startswith(go_module)
        else package
    )
    changed_dirs = sorted({posixpath.dirname(p) for p in changed_files if p.endswith(".go")})
    known_before = test_id in history.tests
    top = name.split("/", 1)[0]
    top_level_known = any(
        t.partition("::")[0] == package and t.partition("::")[2].split("/", 1)[0] == top
        for t in history.tests
    )
    if not known_before and not top_level_known:
        if rel in changed_dirs:
            why = f"new test in changed package ./{rel} (should run whole: check selection)"
        else:
            why = f"new test; ./{rel} is not in this run's diff"
        return Missed(test_id, known_before, why)
    dirs = ", ".join(f"./{d}" for d in changed_dirs) or "(no Go files changed)"
    why = f"./{rel} has no changed files; changed Go packages: {dirs}."
    if graph is None:
        why += " No import graph for this commit."
    else:
        why += " Not reachable through Go imports."
    if rel.startswith("test/cli"):
        why += " test/cli runs the ipfs binary: needs a declared dependency."
    return Missed(test_id, known_before, why)


def counterfactual(
    history: RepoHistory,
    changed: list[str],
    failures: list[str],
    config: SelectorConfig,
    affected: dict[str, list[str]],
    graph: GoGraph | None,
) -> Counterfactual:
    """Select from only the changed files that are covered by a signal on their own."""
    mapped = [
        p
        for p in changed
        if (not is_build_file(p) or build_file_scope(p) is not None)
        and not is_ignorable(p)
        and select_tests(history, [p], True, config, affected).mode is Mode.SELECTIVE
    ]
    if not mapped:
        return Counterfactual([], None, [], [])
    selection = select_tests(history, mapped, True, config, affected)
    caught = [f for f in failures if would_run(selection, f)]
    missed = [
        explain_miss(f, history, changed, config.go_module, graph)
        for f in failures
        if f not in caught
    ]
    return Counterfactual(mapped, selection, caught, missed)


def skipped_pcts(
    selection: Selection, history: RepoHistory, durations: dict[str, float]
) -> tuple[float, float]:
    """(% of known tests skipped, % of estimated top-level runtime skipped)."""
    if selection.mode is Mode.FULL or not history.tests:
        return 0.0, 0.0
    tests_pct = 100 * (1 - selection.selected_count / len(history.tests))
    top_level = [t for t in history.tests if t in durations]
    total = sum(durations[t] for t in top_level)
    run = sum(durations[t] for t in top_level if would_run(selection, t))
    time_pct = 100 * (1 - run / total) if total else 0.0
    return tests_pct, time_pct


def evaluate(
    session: Session,
    repo_id: int,
    run: TargetRun,
    config: SelectorConfig,
    graphs: GraphProvider,
    durations: dict[str, float],
) -> Result:
    history = history_before(session, repo_id, run, config)
    known = run.changed_files is not None
    changed = list(run.changed_files or ())
    graph, graph_note = graphs.graph_at(run.commit_sha) if known else (None, "diff unknown")
    affected = graph.affected(changed) if graph else {}
    selection = select_tests(history, changed, known, config, affected)
    failures = actual_failures(session, list(run.run_ids or (run.run_id,)))
    caught = [f for f in failures if would_run(selection, f)]
    missed = [
        explain_miss(f, history, changed, config.go_module, graph)
        for f in failures
        if f not in caught
    ]
    cf = (
        counterfactual(history, changed, failures, config, affected, graph)
        if failures and selection.mode is Mode.FULL and known
        else None
    )
    signals: dict[str, int] = defaultdict(int)
    for t in selection.tests:
        for reason in t.reasons:
            signals[_signal(reason)] += 1
    tests_pct, time_pct = skipped_pcts(selection, history, durations)
    return Result(
        run, changed, selection, failures, caught, missed, graph_note,
        tests_pct, time_pct, cf, dict(signals),
    )  # fmt: skip


def _signal(reason: str) -> str:
    for prefix, name in (
        ("imports changed package", "go imports"),
        ("declared dependency", "declared"),
        ("co-change", "co-change"),
        ("failed on main", "recently failed"),
        ("broken on main", "recently failed"),
        ("always-run", "always-run"),
    ):
        if reason.startswith(prefix):
            return name
    return "path mapping"


# --- report -------------------------------------------------------------------------------


def print_result(r: Result, verbose: bool) -> None:
    s = r.selection
    head = (
        f"run {r.run.run_id} {r.run.commit_sha[:7]} {r.run.branch[:40]} "
        f"@ {r.run.run_at:%Y-%m-%d} [{r.run.diff_label}]: {s.mode.value}, "
        f"{s.selected_count}/{s.total_known} tests, "
        f"skips {r.tests_skipped_pct:.0f}% tests / {r.time_skipped_pct:.0f}% time"
    )
    interesting = r.failures or verbose
    typer.echo(head + ("" if interesting else f" | {s.reason[:90]}"))
    if not interesting:
        return
    files = ", ".join(r.changed_files[:5]) + (
        f" (+{len(r.changed_files) - 5})" if len(r.changed_files) > 5 else ""
    )
    typer.echo(f"  changed ({len(r.changed_files)}): {files or '(none or unknown)'}")
    typer.echo(f"  {s.reason}")
    typer.echo(f"  {r.graph_note}")
    if r.signal_counts:
        typer.echo(
            "  signals: " + ", ".join(f"{k} {v}" for k, v in sorted(r.signal_counts.items()))
        )
    if s.mode is Mode.SELECTIVE and s.commands:
        typer.echo(f"  $ {s.command[:200]}{'...' if len(s.command) > 200 else ''}")
    if r.failures:
        typer.echo(
            f"  failures: {len(r.failures)} ({len(r.caught)} caught, {len(r.missed)} missed)"
        )
    for test_id in r.caught:
        typer.echo(f"    CAUGHT  {test_id}")
    for m in r.missed:
        typer.echo(f"    MISSED  {m.test_id}{'' if m.known_before else '  [new test]'}")
        typer.echo(f"            {m.why}")
    cf = r.counterfactual
    if cf is not None:
        if cf.selection is None:
            typer.echo("  without fallbacks: nothing covered; only the fallback protects this run")
        else:
            typer.echo(
                f"  without fallbacks: {cf.selection.selected_count} tests, "
                f"would catch {len(cf.caught)}/{len(r.failures)}"
            )
            for m in cf.missed:
                typer.echo(f"    WOULD MISS  {m.test_id}\n                {m.why}")


def print_summary(title: str, results: list[Result]) -> None:
    if not results:
        return
    full = [r for r in results if r.selection.mode is Mode.FULL]
    selective = [r for r in results if r.selection.mode is Mode.SELECTIVE]
    n = len(results)
    typer.echo(f"\n=== {title}: {n} runs ===")
    typer.echo(
        f"  full: {len(full)} ({100 * len(full) / n:.0f}%), "
        f"selective: {len(selective)} ({100 * len(selective) / n:.0f}%)"
    )
    reasons: dict[str, int] = defaultdict(int)
    for r in full:
        reasons[r.selection.reason.split(":")[0]] += 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        typer.echo(f"    full because {reason}: {count}")
    if selective:
        avg_tests = sum(r.tests_skipped_pct for r in selective) / len(selective)
        avg_time = sum(r.time_skipped_pct for r in selective) / len(selective)
        typer.echo(
            f"  skipped in selective runs: avg {avg_tests:.1f}% of tests, {avg_time:.1f}% of time"
        )
    overall_time = sum(r.time_skipped_pct for r in results) / n
    typer.echo(f"  est. time skipped over all runs (full = 0%): {overall_time:.1f}%")
    signals: dict[str, int] = defaultdict(int)
    for r in selective:
        for name in r.signal_counts:
            signals[name] += 1
    if signals:
        typer.echo(
            "  selective runs using each signal: "
            + ", ".join(f"{k} {v}" for k, v in sorted(signals.items()))
        )
    failures = sum(len(r.failures) for r in results)
    caught = sum(len(r.caught) for r in results)
    typer.echo(
        f"  failures: {failures} total in {sum(1 for r in results if r.failures)} runs, "
        f"{caught} caught, {failures - caught} missed"
        + (f" ({100 * caught / failures:.0f}% recall)" if failures else "")
    )
    sel_failures = sum(len(r.failures) for r in selective)
    sel_caught = sum(len(r.caught) for r in selective)
    if sel_failures:
        typer.echo(f"    in selective runs: {sel_caught}/{sel_failures} caught")
    cfs = [r for r in results if r.counterfactual is not None]
    if cfs:
        covered = [r for r in cfs if r.counterfactual and r.counterfactual.selection]
        cf_failures = sum(len(r.failures) for r in covered)
        cf_caught = sum(len(r.counterfactual.caught) for r in covered if r.counterfactual)
        only_fallback = sum(
            len(r.failures) for r in cfs if r.counterfactual and not r.counterfactual.selection
        )
        typer.echo(
            f"  full runs, without fallbacks: {cf_caught}/{cf_failures} caught; "
            f"{only_fallback} more failures had nothing but a fallback"
        )
    notes: dict[str, int] = defaultdict(int)
    for r in results:
        notes[r.graph_note.split(" at ")[0]] += 1
    typer.echo("  import graph: " + ", ".join(f"{k} ({v})" for k, v in notes.items()))


def _github_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    env = Path(".env")
    if env.exists():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "GITHUB_TOKEN":
                return value.strip().strip("\"'")
    return None


def main(
    repo: Annotated[str, typer.Option(help="Repository name in sieve.")] = "ipfs/kubo",
    runs: Annotated[int, typer.Option(min=1, help="Most recent PR runs to evaluate.")] = 10,
    failing: Annotated[
        bool, typer.Option("--failing", help="Also evaluate every PR run that had failures.")
    ] = False,
    replay_main: Annotated[
        bool, typer.Option("--replay-main", help="Treat each main run as a PR.")
    ] = False,
    go_module: Annotated[str, typer.Option(help="Go module path.")] = "github.com/ipfs/kubo",
    sieve_toml: Annotated[
        Path | None, typer.Option(help="Repo config with [[depends]] / always_run.")
    ] = None,
    kubo_checkout: Annotated[
        Path | None, typer.Option(help="Git clone of the repo, for the import graph.")
    ] = None,
    go: Annotated[str, typer.Option(help="go binary for `go list`.")] = "go",
    cache_dir: Annotated[Path, typer.Option(help="Cache for compares and graphs.")] = Path(
        ".eval-cache"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", help="Details for every run.")] = False,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    repo_config = load_repo_config(sieve_toml) if sieve_toml else None
    config = SelectorConfig(
        go_module=go_module or None,
        depends=repo_config.depends if repo_config else (),
        always_run=repo_config.always_run if repo_config else (),
    )
    graphs = GraphProvider(kubo_checkout, go, cache_dir)
    with get_sessionmaker()() as session:
        repo_id = session.execute(
            text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
        ).scalar_one_or_none()
        if repo_id is None:
            typer.echo(f"error: unknown repo {repo!r}", err=True)
            raise typer.Exit(2)
        durations = top_level_durations(session, repo_id)
        rules = f"{len(config.depends)} declared rule(s)" if config.depends else "no declared rules"
        typer.echo(f"# config: {rules}; graph: {'go list' if kubo_checkout else 'none'}")

        def run_all(title: str, targets: list[TargetRun]) -> list[Result]:
            typer.echo(f"\n# {title}")
            results = []
            for target in targets:
                result = evaluate(session, repo_id, target, config, graphs, durations)
                print_result(result, verbose)
                results.append(result)
            return results

        if replay_main:
            token = _github_token()
            if not token:
                typer.echo("error: --replay-main needs GITHUB_TOKEN", err=True)
                raise typer.Exit(2)
            compare = GitHubCompare(token, cache_dir)
            targets = main_replay_targets(session, repo_id, repo, compare)
            print_summary("main runs replayed as PRs", run_all("main replay", targets))
        else:
            recent = run_all("most recent PR runs", pr_targets(session, repo_id, runs, False))
            failing_results: list[Result] = []
            if failing:
                seen = {r.run.run_id for r in recent}
                extra = [
                    t for t in pr_targets(session, repo_id, 10_000, True) if t.run_id not in seen
                ]
                failing_results = [r for r in recent if r.failures] + run_all(
                    "other PR runs with failures", extra
                )
            print_summary("most recent PR runs", recent)
            print_summary("PR runs with failures", failing_results)


if __name__ == "__main__":
    typer.run(main)
