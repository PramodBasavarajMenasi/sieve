"""Replay the selector against a repo's real runs: savings, and failures caught or missed.

Two modes:

* default: the most recent PR runs (non-main, ``changed_files_known``), using their recorded
  diffs. ``--failing`` adds every PR run that had failures.
* ``--replay-main``: treat each main run as a PR whose diff is the previous main run's
  commit ... this commit (fetched from the GitHub compare API and cached).

A target is one CI run attempt: its matrix variants are grouped, and a failure in any variant
counts. For each target R:

1. Rebuild the selector's history *as of just before R* (no peeking): only runs strictly
   earlier than R by ``COALESCE(started_at, created_at)``. ``test_stats`` is not used for
   selection; broken-on-main is recomputed per variant from raw results. The repo's raw
   results are read once into memory (``HistoryIndex``), so each target is cheap.
2. With ``--checkout``, compute the import graph at R's commit (cached per commit) and pass it
   to the selector, as ``siftwise select`` does: for Go ``go list -deps -test -json ./...``
   (needs ``--go``), for Python the static graph from ``siftwise.cli.pygraph``.
3. Run ``select_tests`` with the repo config from ``--siftwise-toml``.
4. Compare with the tests that actually failed in R. A failure is caught if the selection's
   commands would run it (including new tests in a Python file or Go package run whole).
5. Explain each miss with the signal that would have caught it.
6. Report tests and estimated runtime skipped (each top-level test's average duration from
   test_stats; sizes the savings only, never affects selection).

    uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --checkout ../rdflib
    uv run python -m scripts.eval.run_eval --repo RDFLib/rdflib --replay-main --checkout ../rdflib
    uv run python -m scripts.eval.run_eval --repo ipfs/kubo --failing \
        --siftwise-toml scripts/eval/kubo.siftwise.toml --checkout ../kubo --go go

Reads SIFTWISE_DATABASE_URL (or .env), and GITHUB_TOKEN for --replay-main. Read-only.
"""

from __future__ import annotations

import bisect
import itertools
import json
import os
import posixpath
import subprocess
import sys
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from sqlalchemy import text
from sqlalchemy.orm import Session

if not __package__:  # run as a file path: make the repo root importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from siftwise.cli.gograph import GoGraph, build_graph, parse_go_list
from siftwise.cli.pygraph import PyGraph, build_py_graph, module_of_test
from siftwise.cli.repoconfig import load_repo_config, read_go_module
from siftwise.core.selector import (
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
    pytest_file,
    select_tests,
)
from siftwise.db import get_sessionmaker

FAILED = ("failed", "error")
# Main CI runs read to recompute broken-on-main per target (see broken_on_main_before).
BROKEN_LOOKBACK_CI_RUNS = 20
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
    # The signal that would have caught it (or why none would): grouped in the summary.
    signal: str = "none"


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


@dataclass(frozen=True)
class IndexedRun:
    id: int
    ci_key: str  # COALESCE(ci_run_id, 'run:' || id), as in selector.recent_ci_runs_sql
    run_attempt: int
    run_at: datetime
    is_main: bool
    changed_files_known: bool
    commit_sha: str
    variant: str  # '' = no variant

    @property
    def order(self) -> tuple[datetime, int]:
        return (self.run_at, self.id)


def _bits(mask: int) -> Iterator[int]:
    """Positions of the set bits of ``mask``, lowest first."""
    for i, byte in enumerate(mask.to_bytes((mask.bit_length() + 7) // 8, "little")):
        while byte:
            low = byte & -byte
            yield i * 8 + low.bit_length() - 1
            byte ^= low


def _set_bit(bitmap: bytearray, bit: int) -> None:
    byte = bit >> 3
    if byte >= len(bitmap):
        bitmap.extend(bytes(byte - len(bitmap) + 256))
    bitmap[byte] |= 1 << (bit & 7)


class HistoryIndex:
    """A repo's raw history in memory, to rebuild the selector's history as of any run.

    ``load_history`` reads the *current* history (known tests from test_stats). The eval needs
    it as of each target, with no peeking, which from SQL means re-reading millions of raw
    results per target on a matrix repo. So every result is read once here: per run, a bitmask
    of the tests it reported, the tests that failed, and for main runs each test's final
    outcome. ``history_before`` then applies the selector's definitions in memory.
    """

    def __init__(self, session: Session, repo_id: int) -> None:
        params = {"repo_id": repo_id}
        stream = {"yield_per": 100_000}
        self.runs = sorted(
            (
                IndexedRun(*row)
                for row in session.execute(
                    text(
                        """
                        SELECT id, COALESCE(ci_run_id, 'run:' || id), run_attempt,
                               COALESCE(started_at, created_at), is_main, changed_files_known,
                               commit_sha, COALESCE(variant, '')
                        FROM runs WHERE repo_id = :repo_id
                        """
                    ),
                    params,
                )
            ),
            key=lambda r: r.order,
        )
        self._orders = [r.order for r in self.runs]
        position = {r.id: i for i, r in enumerate(self.runs)}
        self._tests: list[str] = []
        bit_of: dict[str, int] = {}
        seen: dict[int, bytearray] = defaultdict(bytearray)
        # test bit -> {file path: position of the first run that reported it}
        paths: dict[int, dict[str, int]] = defaultdict(dict)
        for run_id, test_id, file_path in session.execute(
            text(
                "SELECT tr.run_id, tr.test_id, tr.file_path FROM test_results tr "
                "JOIN runs ru ON ru.id = tr.run_id WHERE ru.repo_id = :repo_id"
            ),
            params,
            execution_options=stream,
        ):
            bit = bit_of.get(test_id)
            if bit is None:
                bit = bit_of[test_id] = len(self._tests)
                self._tests.append(test_id)
            _set_bit(seen[run_id], bit)
            if file_path:
                pos, first = position[run_id], paths[bit].get(file_path)
                if first is None or pos < first:
                    paths[bit][file_path] = pos
        self._seen = {run_id: int.from_bytes(b, "little") for run_id, b in seen.items()}
        self._paths = dict(paths)

        # Any failed/error result (all attempts), as the selector's failure queries read them.
        self._failed: dict[int, set[str]] = defaultdict(set)
        for run_id, test_id in session.execute(
            text(
                "SELECT tr.run_id, tr.test_id FROM test_results tr "
                "JOIN runs ru ON ru.id = tr.run_id "
                "WHERE ru.repo_id = :repo_id AND tr.status IN ('failed', 'error')"
            ),
            params,
        ):
            self._failed[run_id].add(test_id)

        # Main runs: each test's final outcome (non-skipped), for broken-on-main.
        main_failed: dict[int, bytearray] = defaultdict(bytearray)
        main_passed: dict[int, bytearray] = defaultdict(bytearray)
        for run_id, test_id, status in session.execute(
            text(
                """
                SELECT DISTINCT ON (tr.run_id, tr.test_id) tr.run_id, tr.test_id, tr.status
                FROM test_results tr JOIN runs ru ON ru.id = tr.run_id
                WHERE ru.repo_id = :repo_id AND ru.is_main
                ORDER BY tr.run_id, tr.test_id, tr.attempt DESC, tr.id DESC
                """
            ),
            params,
            execution_options=stream,
        ):
            if status in FAILED:
                _set_bit(main_failed[run_id], bit_of[test_id])
            elif status == "passed":
                _set_bit(main_passed[run_id], bit_of[test_id])
        self._main_failed = {r: int.from_bytes(b, "little") for r, b in main_failed.items()}
        self._main_passed = {r: int.from_bytes(b, "little") for r, b in main_passed.items()}

        self._changed = changed_files_of(session, [r.id for r in self.runs])

    def __str__(self) -> str:
        return f"{len(self.runs)} runs, {len(self._tests)} tests"

    def history_before(self, run: TargetRun, config: SelectorConfig) -> RepoHistory:
        """What ``load_history`` would have returned just before ``run`` was ingested.

        Only runs strictly earlier than ``run`` by (run time, id) count. Known tests are those
        in any of the latest ``known_test_runs`` CI runs, which is what test_stats' last-seen
        run encodes for ``load_history``.
        """
        prior = self.runs[: bisect.bisect_left(self._orders, (run.run_at, run.run_id))]

        def recent(n: int, keep: Callable[[IndexedRun], bool]) -> list[IndexedRun]:
            """Runs of the latest ``n`` CI runs among ``prior``, newest first."""
            groups: dict[tuple[str, int], list[IndexedRun]] = defaultdict(list)
            for r in prior:
                if keep(r):
                    groups[(r.ci_key, r.run_attempt)].append(r)
            latest = sorted(
                groups.values(),
                key=lambda g: (max(r.run_at for r in g), max(r.id for r in g)),
                reverse=True,
            )[:n]
            return sorted((r for g in latest for r in g), key=lambda r: r.order, reverse=True)

        mask = 0
        for r in recent(config.known_test_runs, lambda r: True):
            mask |= self._seen.get(r.id, 0)
        cutoff = len(prior)
        tests = {}
        for bit in _bits(mask):
            test_id = self._tests[bit]
            reported = [(pos, p) for p, pos in self._paths.get(bit, {}).items() if pos < cutoff]
            tests[test_id] = KnownTest(test_id, max(reported)[1] if reported else None)

        recent_main_failures: dict[str, tuple[int, str]] = {}
        for r in recent(config.recent_main_runs, lambda r: r.is_main):
            for test_id in self._failed.get(r.id, ()):
                recent_main_failures.setdefault(test_id, (r.id, r.commit_sha))

        failed_runs = tuple(
            FailedRun(r.id, r.commit_sha, frozenset(self._changed.get(r.id, ())),
                      frozenset(self._failed[r.id]))
            for r in recent(config.co_change_runs, lambda r: r.changed_files_known)
            if r.id in self._failed
        )  # fmt: skip

        return RepoHistory(
            tests=tests,
            broken_on_main=self._broken_on_main(
                recent(BROKEN_LOOKBACK_CI_RUNS, lambda r: r.is_main)
            ),
            recent_main_failures=recent_main_failures,
            failed_runs=failed_runs,
        )

    def _broken_on_main(self, main_runs: list[IndexedRun]) -> dict[str, str]:
        """Same definition as core/history.py, from ``main_runs`` (newest first) only.

        Per variant, a test is broken if its latest non-skipped outcome is a failure; its
        streak starts at the oldest failure before a pass. The sha is the earliest start
        across broken variants. Only the last ``BROKEN_LOOKBACK_CI_RUNS`` main CI runs are
        read: the selector only uses *whether* a test is broken, so a shorter lookback
        changes at most the streak sha in a reason, never which tests are selected.
        """
        by_variant: dict[str, list[IndexedRun]] = defaultdict(list)
        for r in main_runs:
            by_variant[r.variant].append(r)
        starts: dict[str, tuple[datetime, int, str]] = {}
        for runs in by_variant.values():
            decided = alive = 0  # tests with a newer outcome / still in a failing streak
            streak_start: dict[int, IndexedRun] = {}
            for r in runs:  # newest first
                failed = self._main_failed.get(r.id, 0)
                passed = self._main_passed.get(r.id, 0)
                alive |= failed & ~decided  # its latest outcome is a failure
                alive &= ~passed  # an older pass ends the streak
                for bit in _bits(failed & alive):
                    streak_start[bit] = r
                decided |= failed | passed
            for bit, r in streak_start.items():
                test_id, start = self._tests[bit], (r.run_at, r.id, r.commit_sha)
                if test_id not in starts or start < starts[test_id]:
                    starts[test_id] = start
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


def top_level_durations(session: Session, repo_id: int, go: bool) -> dict[str, float]:
    """Average duration of each top-level test. Go subtests (``TestX/sub``) are inside their
    parent's time, so they're dropped; for other languages every test is top-level."""
    rows = session.execute(
        text(
            "SELECT test_id, avg_duration_ms FROM test_stats "
            "WHERE repo_id = :repo_id AND avg_duration_ms IS NOT NULL"
        ),
        {"repo_id": repo_id},
    ).all()
    return {t: d for t, d in rows if not go or "/" not in t.partition("::")[2]}


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
    """Import graphs at a commit, from a checkout, cached per commit.

    Go: ``go list`` (fed to the selector). Python: ``pygraph`` (only to explain misses).
    """

    def __init__(self, checkout: Path | None, go: str, cache_dir: Path) -> None:
        self.checkout = checkout
        self.go = go
        self.cache_dir = cache_dir

    def go_graph_at(self, sha: str) -> tuple[GoGraph | None, str]:
        cache = self.cache_dir / f"gograph-{sha}.json"
        if cache.exists():
            data = json.loads(cache.read_text())
            if "error" in data:
                return None, f"go list failed at {sha[:7]}: {data['error']}"
            return GoGraph.from_json(data), f"import graph at {sha[:7]}"
        if self.checkout is None:
            return None, "no import graph (no --checkout)"
        if not self._checkout(sha):
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

    def has_py_graph(self, sha: str) -> bool:
        return (self.cache_dir / f"pygraph-{sha}.json").exists()

    def py_graph_at(self, sha: str) -> PyGraph | None:
        cache = self.cache_dir / f"pygraph-{sha}.json"
        if cache.exists():
            return PyGraph.from_json(json.loads(cache.read_text()))
        if self.checkout is None or not self._checkout(sha):
            return None
        graph = build_py_graph(self.checkout)
        cache.write_text(json.dumps(graph.to_json()))
        return graph

    def _checkout(self, sha: str) -> bool:
        if not self._git("cat-file", "-e", f"{sha}^{{commit}}"):
            self._git("fetch", "-q", "origin", sha)
        return self._git("checkout", "-q", "-f", "--detach", sha)

    def _git(self, *args: str) -> bool:
        assert self.checkout is not None
        proc = subprocess.run(
            ["git", "-C", str(self.checkout), *args], capture_output=True, timeout=600, check=False
        )
        return proc.returncode == 0


# --- evaluation ---------------------------------------------------------------------------


class WouldRun:
    """Whether a selection's commands would execute a test (precomputed for many lookups)."""

    def __init__(self, selection: Selection) -> None:
        self.full = selection.mode is Mode.FULL
        self.selected = {t.test_id for t in selection.tests}
        self.go_packages = set(selection.go_packages_run_whole)
        self.py_files = set(selection.python_files_run_whole)
        # go test -run '^(TestX)$' runs TestX with all of its subtests.
        self.go_top_level = {
            (package, name.split("/", 1)[0])
            for t in selection.tests
            if t.runner is Runner.GO
            for package, _, name in [t.test_id.partition("::")]
        }

    def __call__(self, test_id: str) -> bool:
        if self.full or test_id in self.selected:
            return True
        package, _, name = test_id.partition("::")
        if package in self.go_packages:
            return True  # `go test ./pkg` with no -run: every test in the package, new ones too
        if (package, name.split("/", 1)[0]) in self.go_top_level:
            return True
        # `pytest path/test_x.py`: every test in the file, new ones too.
        return bool(self.py_files) and pytest_file(test_id) in self.py_files


def would_run(selection: Selection, test_id: str) -> bool:
    """Whether the selection's commands would execute ``test_id``."""
    return WouldRun(selection)(test_id)


@dataclass(frozen=True)
class Explainer:
    """Explains misses for one target run (``go``: Go repo; else Python/pytest)."""

    history: RepoHistory
    changed_files: list[str]
    go_module: str | None
    go: bool
    go_graph: GoGraph | None
    graphs: GraphProvider
    sha: str

    def __call__(self, test_id: str) -> Missed:
        known_before = test_id in self.history.tests
        if not known_before and not self._top_level_known(test_id):
            return Missed(test_id, False, "new test: not in history, so no signal can name it",
                          "new test")  # fmt: skip
        return self._go(test_id) if self.go else self._python(test_id)

    def _top_level_known(self, test_id: str) -> bool:
        if not self.go:
            return False
        package, _, name = test_id.partition("::")
        top = name.split("/", 1)[0]
        return any(
            t.partition("::")[0] == package and t.partition("::")[2].split("/", 1)[0] == top
            for t in self.history.tests
        )

    def _go(self, test_id: str) -> Missed:
        package = test_id.partition("::")[0]
        rel = (
            package[len(self.go_module) :].lstrip("/")
            if self.go_module and package.startswith(self.go_module)
            else package
        )
        dirs = sorted({posixpath.dirname(p) for p in self.changed_files if p.endswith(".go")})
        changed = ", ".join(f"./{d}" for d in dirs) or "(no Go files changed)"
        if self.go_graph is None:
            return Missed(test_id, True, f"./{rel} unchanged ({changed}); no import graph",
                          "go imports (graph unavailable)")  # fmt: skip
        return self._recurring(test_id) or Missed(
            test_id, True,
            f"./{rel} has no changed files and doesn't import {changed}; needs a declared "
            "dependency (e.g. a suite that runs a built binary)",
            "declared dependency",
        )  # fmt: skip

    def _python(self, test_id: str) -> Missed:
        test_file = module_of_test(test_id)
        changed_py = {p for p in self.changed_files if p.endswith(".py")}
        graph = self.graphs.py_graph_at(self.sha) if test_file and changed_py else None
        if test_file and graph and test_file in graph.edges:
            chain = graph.import_chain(test_file, changed_py)
            if chain:
                hops = " -> ".join(chain[:5]) + (" -> ..." if len(chain) > 5 else "")
                return Missed(test_id, True, f"imports a changed module: {hops}",
                              "python imports")  # fmt: skip
        recurring = self._recurring(test_id)
        if recurring:
            return recurring
        if not changed_py:
            return Missed(test_id, True, "no Python file changed; failure looks unrelated",
                          "none (unrelated)")  # fmt: skip
        if graph is None or not test_file or test_file not in graph.edges:
            return Missed(test_id, True, f"no import graph for {test_file or test_id}",
                          "python imports (graph unavailable)")  # fmt: skip
        return Missed(
            test_id, True,
            f"{test_file} doesn't import any changed module "
            f"({', '.join(sorted(changed_py)[:3])}); failure looks unrelated",
            "none (unrelated)",
        )  # fmt: skip

    def _recurring(self, test_id: str) -> Missed | None:
        """A test that already failed in earlier runs with unrelated changes: likely flaky."""
        earlier = [run for run in self.history.failed_runs if test_id in run.failed_tests]
        if not earlier:
            return None
        return Missed(
            test_id, True,
            f"also failed in {len(earlier)} earlier run(s) (latest run {earlier[0].run_id}); "
            "likely flaky: needs flaky detection / recently-failed beyond main",
            "flaky / recurring",
        )  # fmt: skip


def counterfactual(
    history: RepoHistory,
    changed: list[str],
    failures: list[str],
    config: SelectorConfig,
    affected: dict[str, list[str]],
    affected_files: dict[str, list[str]],
    explain: Explainer,
) -> Counterfactual:
    """Select from only the changed files that are covered by a signal on their own."""

    def select(files: list[str]) -> Selection:
        return select_tests(history, files, True, config, affected, affected_files)

    mapped = [
        p
        for p in changed
        if (not is_build_file(p) or build_file_scope(p) is not None)
        and not is_ignorable(p)
        and select([p]).mode is Mode.SELECTIVE
    ]
    if not mapped:
        return Counterfactual([], None, [], [])
    selection = select(mapped)
    runs = WouldRun(selection)
    caught = [f for f in failures if runs(f)]
    missed = [explain(f) for f in failures if f not in caught]
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
    runs = WouldRun(selection)
    run = sum(durations[t] for t in top_level if runs(t))
    time_pct = 100 * (1 - run / total) if total else 0.0
    return tests_pct, time_pct


def evaluate(
    session: Session,
    index: HistoryIndex,
    run: TargetRun,
    config: SelectorConfig,
    graphs: GraphProvider,
    durations: dict[str, float],
    go: bool,
) -> Result:
    history = index.history_before(run, config)
    known = run.changed_files is not None
    changed = list(run.changed_files or ())
    graph: GoGraph | None = None
    affected_files: dict[str, list[str]] = {}
    if not known:
        graph_note = "diff unknown"
    elif go:
        graph, graph_note = graphs.go_graph_at(run.commit_sha)
    elif graphs.checkout is None and not graphs.has_py_graph(run.commit_sha):
        graph_note = "python: no --checkout, no import graph"
    elif not any(p.endswith(".py") for p in changed):
        graph_note = "python: no Python file changed"
    else:
        # As `siftwise select` does: the static import graph at the target's commit.
        py_graph = graphs.py_graph_at(run.commit_sha)
        if py_graph is None:
            graph_note = f"python: no import graph at {run.commit_sha[:7]}"
        else:
            affected_files = py_graph.affected(changed)
            graph_note = f"python imports at {run.commit_sha[:7]}"
    affected = graph.affected(changed) if graph else {}
    explain = Explainer(history, changed, config.go_module, go, graph, graphs, run.commit_sha)
    selection = select_tests(history, changed, known, config, affected, affected_files)
    failures = actual_failures(session, list(run.run_ids or (run.run_id,)))
    runs = WouldRun(selection)
    caught = [f for f in failures if runs(f)]
    missed = [explain(f) for f in failures if f not in caught]
    cf = (
        counterfactual(history, changed, failures, config, affected, affected_files, explain)
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
        ("imports changed module", "python imports"),
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
        typer.echo(f"    MISSED  {m.test_id}  [{m.signal}]")
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
                typer.echo(f"    WOULD MISS  {m.test_id}  [{m.signal}]\n                {m.why}")


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
    _print_miss_signals("missed", [m for r in results for m in r.missed])
    _print_miss_signals(
        "without fallbacks, would miss",
        [m for r in results if r.counterfactual for m in r.counterfactual.missed],
    )
    notes: dict[str, int] = defaultdict(int)
    for r in results:
        notes[r.graph_note.split(" at ")[0]] += 1
    typer.echo("  import graph: " + ", ".join(f"{k} ({v})" for k, v in notes.items()))


def _print_miss_signals(label: str, missed: list[Missed]) -> None:
    if not missed:
        return
    by_signal: dict[str, int] = defaultdict(int)
    for m in missed:
        by_signal[m.signal] += 1
    typer.echo(
        f"  {label}, by the signal that would have caught it: "
        + ", ".join(f"{k} {v}" for k, v in sorted(by_signal.items(), key=lambda kv: -kv[1]))
    )


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
    repo: Annotated[str, typer.Option(help="Repository name in siftwise, e.g. RDFLib/rdflib.")],
    runs: Annotated[int, typer.Option(min=1, help="Most recent PR runs to evaluate.")] = 10,
    failing: Annotated[
        bool, typer.Option("--failing", help="Also evaluate every PR run that had failures.")
    ] = False,
    replay_main: Annotated[
        bool, typer.Option("--replay-main", help="Treat each main run as a PR.")
    ] = False,
    language: Annotated[
        str, typer.Option(help="auto, go or python (auto: go if the checkout has go.mod).")
    ] = "auto",
    go_module: Annotated[
        str | None, typer.Option(help="Go module path (default: from the checkout's go.mod).")
    ] = None,
    siftwise_toml: Annotated[
        Path | None, typer.Option(help="Repo config with [[depends]] / always_run.")
    ] = None,
    checkout: Annotated[
        Path | None, typer.Option(help="Git clone of the repo, for import graphs.")
    ] = None,
    go: Annotated[str, typer.Option(help="go binary for `go list`.")] = "go",
    cache_dir: Annotated[Path, typer.Option(help="Cache for compares and graphs.")] = Path(
        ".eval-cache"
    ),
    verbose: Annotated[bool, typer.Option("--verbose", help="Details for every run.")] = False,
) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    if checkout and not go_module:
        go_module = read_go_module(checkout)
    is_go = language == "go" or (language == "auto" and go_module is not None)
    repo_config = load_repo_config(siftwise_toml) if siftwise_toml else None
    config = SelectorConfig(
        go_module=go_module,
        depends=repo_config.depends if repo_config else (),
        always_run=repo_config.always_run if repo_config else (),
    )
    graphs = GraphProvider(checkout, go, cache_dir)
    with get_sessionmaker()() as session:
        repo_id = session.execute(
            text("SELECT id FROM repos WHERE name = :name"), {"name": repo}
        ).scalar_one_or_none()
        if repo_id is None:
            typer.echo(f"error: unknown repo {repo!r}", err=True)
            raise typer.Exit(2)
        durations = top_level_durations(session, repo_id, go=is_go)
        rules = f"{len(config.depends)} declared rule(s)" if config.depends else "no declared rules"
        graph = ("go list" if is_go else "python imports (misses only)") if checkout else "none"
        typer.echo(f"# {repo} ({'go' if is_go else 'python'}): {rules}; import graph: {graph}")
        started = time.perf_counter()
        index = HistoryIndex(session, repo_id)
        typer.echo(f"history index: {index} in {time.perf_counter() - started:.0f}s", err=True)

        def run_all(title: str, targets: list[TargetRun]) -> list[Result]:
            typer.echo(f"\n# {title}")
            results = []
            for target in targets:
                result = evaluate(session, index, target, config, graphs, durations, is_go)
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
