"""Per-test history rollup (``test_stats``) and history queries.

``test_stats`` is derived data: each row is recomputed from raw results (never incrementally
adjusted), so the rollup is independent of ingest order: backfilling old runs after newer ones
gives the same stats.

The rollup reads a **window**: runs from the ``SIFTWISE_STATS_WINDOW_DAYS`` days (default 90; 0 =
all history) up to the repo's most recent run. Anchoring on the newest run rather than "now"
keeps backfilled history meaningful. Reading is driven by the window's runs (results are
fetched by ``run_id``), so ingest cost depends on how much happened in the window, not on how
much history exists.

Definitions (all counts are within the window):

* Run time is ``COALESCE(runs.started_at, runs.created_at)`` (``Run.occurred_at``). Anything
  "latest" is ordered by it, tie-broken by run id.
* A test's *outcome* in a run is the status of its final attempt (highest ``attempt``).
  ``error`` counts as a failure everywhere.
* A *run* is one variant (matrix leg, e.g. ``ubuntu-py3.12``) of a CI run attempt; each
  variant is uploaded as its own run. Results are compared only within the same variant.
* ``runs``: runs whose outcome is not ``skipped``.
* ``failures``: runs whose outcome is ``failed``/``error``; ``last_failed_at`` is the latest
  such run's time.
* ``avg_duration_ms``: mean over all non-skipped attempts that report a duration.
* ``flaky_score``: of the (commit, variant) pairs where the test ran (non-skipped), the
  fraction where it had both a failed/error and a passed result, across all attempts and
  runs of that commit and variant. Passing on one OS and failing on another is not flaky.
* Broken on main is tracked per variant: a variant is broken if its latest non-skipped
  outcome on main is a failure, starting at the oldest run of that unbroken failing streak.
  When a variant fails on main for the whole window, its older main results are read too, so
  the streak start is found even if it began before the window. ``broken_on_main_variants``
  lists every broken variant with its streak start (oldest first);
  ``broken_on_main_since_sha`` is the earliest of them. Both are ``NULL`` once every variant
  passes on main again.
* ``last_seen_at``: the latest run (any status, skipped included) the test appears in.
  ``last_seen_run_id``: a run of the latest *CI run* the test appears in, with CI runs ordered
  as ``selector.recent_ci_runs_sql`` orders them (latest variant time, then highest run id).
  So "last seen in one of the latest N CI runs" is the same as "appeared in one of them",
  which is how the selector finds known tests without reading raw results.
* ``file_path``: the latest non-null file path reported for the test.

Batch ingest (``POST /runs?defer_rollup=true``) skips the rollup; ``recompute_repo_stats``
(``POST /repos/{repo}/rollup``) then recomputes every test in the window at once. Until it
runs, ``test_stats`` is stale.
"""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from siftwise.core.models import Repo, Run, TestResult, TestStats

# First key of the two-int advisory lock, so other advisory-lock users can't collide with it.
_ROLLUP_LOCK_NAMESPACE = 1

# 0 means "no window": use a span longer than any repo's history.
_ALL_HISTORY_DAYS = 365 * 100

# Recompute stats for the tests selected by {test_filter}, from the window of :repo_id.
#
# Deliberately a single pass: per-(test, run) rows, window functions for the per-commit and
# per-main-streak facts, then one GROUP BY test_id. An earlier version built four per-test
# CTEs and LEFT JOINed them; during a backfill the planner's row estimates lag far behind the
# table (estimating 1 row where there are thousands), it chose nested loops over those CTEs,
# and one ingest's rollup took ~16s instead of <1s. No CTE-to-CTE joins means no such plan.
#
# Window results are read through LATERAL lookups by run_id, starting from the window's runs.
# Each LATERAL subquery has OFFSET 0: without it Postgres flattens the subquery into a plain
# join, and with fresh table statistics it then hash-joins a sequential scan of *all*
# test_results, which is exactly the cost the window exists to avoid.
# test_history.py::test_rollup_reads_only_the_window_not_old_history checks this.
_ROLLUP_TEMPLATE = """
WITH {run_tests}bounds AS MATERIALIZED (
    SELECT max(COALESCE(started_at, created_at)) - make_interval(days => :window_days)
               AS window_start
    FROM runs WHERE repo_id = :repo_id
),
repo_runs AS (  -- with each run's CI run position, ordered like selector.recent_ci_runs_sql
    SELECT id, commit_sha, is_main, variant, COALESCE(started_at, created_at) AS run_at,
           max(COALESCE(started_at, created_at)) OVER ci AS ci_at, max(id) OVER ci AS ci_id
    FROM runs WHERE repo_id = :repo_id
    WINDOW ci AS (PARTITION BY COALESCE(ci_run_id, 'run:' || id), run_attempt)
),
win_runs AS MATERIALIZED (
    SELECT ru.id, ru.commit_sha, ru.is_main, ru.variant,
           COALESCE(ru.variant, '') AS variant_key, ru.run_at, ru.ci_at, ru.ci_id
    FROM repo_runs ru CROSS JOIN bounds
    WHERE ru.run_at >= bounds.window_start
),
win_results AS (
    SELECT tr.id, tr.test_id, tr.run_id, tr.status, tr.attempt, tr.duration_ms, tr.file_path,
           wr.commit_sha, wr.is_main, wr.variant, wr.variant_key, wr.run_at, wr.ci_at, wr.ci_id,
           true AS in_window
    FROM win_runs wr
    CROSS JOIN LATERAL (
        SELECT id, test_id, run_id, status, attempt, duration_ms, file_path
        FROM test_results WHERE run_id = wr.id
        OFFSET 0  -- fence: stops Postgres flattening this into a scan of all test_results
    ) tr
    {test_filter}
),
win_final AS (  -- final attempt per (test, run), for the edge check below
    SELECT DISTINCT ON (test_id, run_id) test_id, variant_key, is_main, status
    FROM win_results
    ORDER BY test_id, run_id, attempt DESC, id DESC
),
edge AS (  -- (test, variant) failing on main for the whole window: the streak may be older
    SELECT test_id, variant_key FROM win_final
    WHERE is_main AND status <> 'skipped'
    GROUP BY test_id, variant_key
    HAVING bool_and(status IN ('failed', 'error'))
),
old_results AS (  -- older main results, only for those, to find where the streak started
    SELECT tr.id, tr.test_id, tr.run_id, tr.status, tr.attempt, tr.duration_ms,
           NULL::text, ru.commit_sha, ru.is_main, ru.variant, edge.variant_key,
           COALESCE(ru.started_at, ru.created_at) AS run_at,
           NULL::timestamptz, NULL::bigint,  -- only window runs count as "seen"
           false AS in_window
    FROM edge
    CROSS JOIN LATERAL (
        SELECT id, test_id, run_id, status, attempt, duration_ms
        FROM test_results WHERE test_id = edge.test_id
        OFFSET 0  -- fence: read only these tests' history, by test_id
    ) tr
    JOIN runs ru ON ru.id = tr.run_id
    CROSS JOIN bounds
    WHERE ru.repo_id = :repo_id AND ru.is_main
      AND COALESCE(ru.variant, '') = edge.variant_key
      AND COALESCE(ru.started_at, ru.created_at) < bounds.window_start
),
results AS (
    SELECT * FROM win_results UNION ALL SELECT * FROM old_results
),
per_run AS (  -- one row per (test, run); each run is one variant of a CI run
    SELECT test_id, run_id, commit_sha, is_main, variant, variant_key, run_at, ci_at, ci_id,
           in_window,
           (array_agg(status ORDER BY attempt DESC, id DESC))[1] AS outcome,
           sum(duration_ms) FILTER (WHERE status <> 'skipped') AS duration_sum,
           count(duration_ms) FILTER (WHERE status <> 'skipped') AS duration_count,
           bool_or(status IN ('failed', 'error')) AS any_failed,
           bool_or(status = 'passed') AS any_passed,
           max(file_path) AS file_path  -- attempts of one test in one run share a file
    FROM results
    GROUP BY test_id, run_id, commit_sha, is_main, variant, variant_key, run_at, ci_at, ci_id,
             in_window
),
annotated AS (
    SELECT *,
           -- flaky: a failure and a pass for the same commit *and variant* (so passing on
           -- linux and failing on macos is a platform difference, not flakiness)
           bool_or(any_failed) OVER by_commit AND bool_or(any_passed) OVER by_commit
               AS commit_flaky,
           bool_or(any_failed OR any_passed) OVER by_commit AS commit_ran,
           row_number() OVER (
               PARTITION BY test_id, commit_sha, variant_key, in_window ORDER BY run_id
           ) AS commit_row,
           is_main AND outcome <> 'skipped' AS main_ran,
           -- passes at or after this run on main, per variant; 0 = in the failing streak
           count(*) FILTER (WHERE outcome = 'passed') OVER (
               PARTITION BY test_id, variant_key, is_main AND outcome <> 'skipped'
               ORDER BY run_at DESC, run_id DESC
           ) AS newer_main_passes
    FROM per_run
    WINDOW by_commit AS (PARTITION BY test_id, commit_sha, variant_key, in_window)
),
streaks AS (
    SELECT *,
           main_ran AND newer_main_passes = 0 AS in_streak,
           -- 1 = the oldest run of this variant's current failing streak on main
           row_number() OVER (
               PARTITION BY test_id, variant_key, main_ran AND newer_main_passes = 0
               ORDER BY run_at, run_id
           ) AS streak_pos
    FROM annotated
),
stats AS (
    SELECT :repo_id AS repo_id, test_id,
           count(*) FILTER (WHERE in_window AND outcome <> 'skipped') AS runs,
           count(*) FILTER (WHERE in_window AND outcome IN ('failed', 'error')) AS failures,
           max(run_at) FILTER (WHERE in_window AND outcome IN ('failed', 'error'))
               AS last_failed_at,
           COALESCE(
               avg(CASE WHEN commit_flaky THEN 1.0 ELSE 0.0 END)
                   FILTER (WHERE in_window AND commit_row = 1 AND commit_ran),
               0
           )::float8 AS flaky_score,
           (sum(duration_sum) FILTER (WHERE in_window)::numeric
               / NULLIF(sum(duration_count) FILTER (WHERE in_window), 0))::float8
               AS avg_duration_ms,
           (array_agg(commit_sha ORDER BY run_at, run_id)
               FILTER (WHERE in_streak AND streak_pos = 1))[1] AS broken_on_main_since_sha,
           jsonb_agg(
               jsonb_build_object('variant', variant, 'since_sha', commit_sha)
               ORDER BY run_at, run_id
           ) FILTER (WHERE in_streak AND streak_pos = 1) AS broken_on_main_variants,
           max(run_at) FILTER (WHERE in_window) AS last_seen_at,
           (array_agg(run_id ORDER BY ci_at DESC, ci_id DESC, run_id DESC)
               FILTER (WHERE in_window))[1] AS last_seen_run_id,
           (array_agg(file_path ORDER BY run_at DESC, run_id DESC)
               FILTER (WHERE in_window AND file_path IS NOT NULL))[1] AS file_path
    FROM streaks
    GROUP BY test_id
    HAVING bool_or(in_window)
)
INSERT INTO test_stats AS ts (
    repo_id, test_id, runs, failures, last_failed_at, flaky_score, avg_duration_ms,
    broken_on_main_since_sha, broken_on_main_variants, last_seen_at, last_seen_run_id, file_path
)
SELECT repo_id, test_id, runs, failures, last_failed_at, flaky_score, avg_duration_ms,
       broken_on_main_since_sha, broken_on_main_variants, last_seen_at, last_seen_run_id,
       file_path
FROM stats
ON CONFLICT (repo_id, test_id) DO UPDATE SET
    runs = EXCLUDED.runs,
    failures = EXCLUDED.failures,
    last_failed_at = EXCLUDED.last_failed_at,
    flaky_score = EXCLUDED.flaky_score,
    avg_duration_ms = EXCLUDED.avg_duration_ms,
    broken_on_main_since_sha = EXCLUDED.broken_on_main_since_sha,
    broken_on_main_variants = EXCLUDED.broken_on_main_variants,
    last_seen_at = EXCLUDED.last_seen_at,
    last_seen_run_id = EXCLUDED.last_seen_run_id,
    file_path = EXCLUDED.file_path
"""

# After one ingest: only the tests in that run. Their list is materialized once: as a plain
# IN (subquery) the planner sometimes picked a nested loop that re-read the run's results for
# every result row in the window (18,000 loops, ~40 s, for a 150-test run with 120 uploads).
_RUN_ROLLUP_SQL = text(
    _ROLLUP_TEMPLATE.format(
        run_tests="run_tests AS MATERIALIZED ("
        "SELECT DISTINCT test_id FROM test_results WHERE run_id = :run_id),\n",
        test_filter="WHERE tr.test_id IN (SELECT test_id FROM run_tests)",
    )
)
# Batch: every test with results in the window.
_REPO_ROLLUP_SQL = text(_ROLLUP_TEMPLATE.format(run_tests="", test_filter=""))


def _window_days(window_days: int) -> int:
    return window_days if window_days > 0 else _ALL_HISTORY_DAYS


def _lock(session: Session, repo_id: int) -> None:
    """Per-repo transaction advisory lock.

    Without it, two concurrent ingests touching the same tests could each compute stats
    without seeing the other's uncommitted results, and the later upsert would overwrite with
    stale numbers. With the lock, the second rollup starts after the first commits.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _ROLLUP_LOCK_NAMESPACE, "key": repo_id},
    )


def recompute_test_stats(session: Session, repo_id: int, run_id: int, window_days: int) -> None:
    """Recompute ``test_stats`` for the tests in ``run_id`` over the window. Does not commit."""
    _lock(session, repo_id)
    session.execute(
        _RUN_ROLLUP_SQL,
        {"repo_id": repo_id, "run_id": run_id, "window_days": _window_days(window_days)},
    )


def recompute_repo_stats(session: Session, repo_id: int, window_days: int) -> int:
    """Recompute ``test_stats`` for every test with results in the window. Does not commit.

    Returns the number of tests updated. Used after a batch (``defer_rollup``) ingest.
    """
    _lock(session, repo_id)
    result = session.execute(
        _REPO_ROLLUP_SQL, {"repo_id": repo_id, "window_days": _window_days(window_days)}
    )
    return int(getattr(result, "rowcount", 0) or 0)


def get_test_stats(session: Session, repo: str, test_id: str) -> TestStats | None:
    return session.scalars(
        select(TestStats)
        .join(Repo, TestStats.repo_id == Repo.id)
        .where(Repo.name == repo, TestStats.test_id == test_id)
    ).one_or_none()


def get_recent_results(
    session: Session, repo_id: int, test_id: str, limit: int = 20
) -> list[tuple[TestResult, Run]]:
    """The test's latest result rows (every attempt), newest run first."""
    return list(
        session.execute(
            select(TestResult, Run)
            .join(Run, TestResult.run_id == Run.id)
            .where(Run.repo_id == repo_id, TestResult.test_id == test_id)
            .order_by(Run.occurred_at.desc(), Run.id.desc(), TestResult.attempt.desc())
            .limit(limit)
        )
        .tuples()
        .all()
    )
