"""Per-test history rollup (``test_stats``) and history queries.

``test_stats`` is derived data: each row is recomputed from *all* raw results for that
``(repo, test_id)``, never incrementally adjusted. That makes the rollup independent of
ingest order, so backfilling old runs after newer ones gives the same stats.

Definitions:

* Run time is ``COALESCE(runs.started_at, runs.created_at)`` (``Run.occurred_at``). Anything
  "latest" is ordered by it, tie-broken by run id.
* A test's *outcome* in a run is the status of its final attempt (highest ``attempt``).
  ``error`` counts as a failure everywhere.
* ``runs``: runs whose outcome is not ``skipped``.
* ``failures``: runs whose outcome is ``failed``/``error``; ``last_failed_at`` is the latest
  such run's time.
* ``avg_duration_ms``: mean over all non-skipped attempts that report a duration.
* ``flaky_score``: of the commits where the test ran (non-skipped), the fraction where it
  had both a failed/error and a passed result, across all attempts and runs of that commit.
* ``broken_on_main_since_sha``: if the latest non-skipped outcome on main is a failure, the
  commit of the oldest run in that unbroken failing streak; ``NULL`` once it passes on main.
"""

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from sieve.core.models import Repo, Run, TestResult, TestStats

# First key of the two-int advisory lock, so other advisory-lock users can't collide with it.
_ROLLUP_LOCK_NAMESPACE = 1

# Recompute stats for every test in :run_id, from that test's full history in :repo_id.
#
# Deliberately a single pass: per-(test, run) rows, window functions for the per-commit and
# per-main-streak facts, then one GROUP BY test_id. An earlier version built four per-test
# CTEs and LEFT JOINed them; during a backfill the planner's row estimates lag far behind the
# table (estimating 1 row where there are thousands), it chose nested loops over those CTEs,
# and one ingest's rollup took ~16s instead of <1s. No CTE-to-CTE joins means no such plan.
_ROLLUP_SQL = text(
    """
WITH affected AS (
    SELECT DISTINCT test_id FROM test_results WHERE run_id = :run_id
),
results AS (
    SELECT tr.id, tr.test_id, tr.run_id, tr.status, tr.attempt, tr.duration_ms,
           ru.commit_sha, ru.is_main, COALESCE(ru.started_at, ru.created_at) AS run_at
    FROM affected
    JOIN test_results tr USING (test_id)
    JOIN runs ru ON ru.id = tr.run_id
    WHERE ru.repo_id = :repo_id
),
per_run AS (  -- one row per (test, run)
    SELECT test_id, run_id, commit_sha, is_main, run_at,
           (array_agg(status ORDER BY attempt DESC, id DESC))[1] AS outcome,
           sum(duration_ms) FILTER (WHERE status <> 'skipped') AS duration_sum,
           count(duration_ms) FILTER (WHERE status <> 'skipped') AS duration_count,
           bool_or(status IN ('failed', 'error')) AS any_failed,
           bool_or(status = 'passed') AS any_passed
    FROM results
    GROUP BY test_id, run_id, commit_sha, is_main, run_at
),
annotated AS (
    SELECT *,
           -- flakiness is per commit, across every run of that commit
           bool_or(any_failed) OVER by_commit AND bool_or(any_passed) OVER by_commit
               AS commit_flaky,
           bool_or(any_failed OR any_passed) OVER by_commit AS commit_ran,
           row_number() OVER (PARTITION BY test_id, commit_sha ORDER BY run_id) AS commit_row,
           is_main AND outcome <> 'skipped' AS main_ran,
           -- passes at or after this run on main; 0 = inside the current failing streak
           count(*) FILTER (WHERE outcome = 'passed') OVER (
               PARTITION BY test_id, is_main AND outcome <> 'skipped'
               ORDER BY run_at DESC, run_id DESC
           ) AS newer_main_passes
    FROM per_run
    WINDOW by_commit AS (PARTITION BY test_id, commit_sha)
),
stats AS (
    SELECT :repo_id AS repo_id, test_id,
           count(*) FILTER (WHERE outcome <> 'skipped') AS runs,
           count(*) FILTER (WHERE outcome IN ('failed', 'error')) AS failures,
           max(run_at) FILTER (WHERE outcome IN ('failed', 'error')) AS last_failed_at,
           COALESCE(
               avg(CASE WHEN commit_flaky THEN 1.0 ELSE 0.0 END)
                   FILTER (WHERE commit_row = 1 AND commit_ran),
               0
           )::float8 AS flaky_score,
           (sum(duration_sum)::numeric / NULLIF(sum(duration_count), 0))::float8
               AS avg_duration_ms,
           (array_agg(commit_sha ORDER BY run_at, run_id)
               FILTER (WHERE main_ran AND newer_main_passes = 0))[1] AS broken_on_main_since_sha
    FROM annotated
    GROUP BY test_id
)
INSERT INTO test_stats AS ts (
    repo_id, test_id, runs, failures, last_failed_at, flaky_score, avg_duration_ms,
    broken_on_main_since_sha
)
SELECT repo_id, test_id, runs, failures, last_failed_at, flaky_score, avg_duration_ms,
       broken_on_main_since_sha
FROM stats
ON CONFLICT (repo_id, test_id) DO UPDATE SET
    runs = EXCLUDED.runs,
    failures = EXCLUDED.failures,
    last_failed_at = EXCLUDED.last_failed_at,
    flaky_score = EXCLUDED.flaky_score,
    avg_duration_ms = EXCLUDED.avg_duration_ms,
    broken_on_main_since_sha = EXCLUDED.broken_on_main_since_sha
"""
)


def recompute_test_stats(session: Session, repo_id: int, run_id: int) -> None:
    """Recompute ``test_stats`` for the tests in ``run_id``. Does not commit.

    Takes a per-repo transaction advisory lock first. Without it, two concurrent ingests
    touching the same tests could each compute stats without seeing the other's
    uncommitted results, and the later upsert would overwrite with stale numbers. With
    the lock, the second rollup starts after the first commits and sees its rows.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _ROLLUP_LOCK_NAMESPACE, "key": repo_id},
    )
    session.execute(_ROLLUP_SQL, {"repo_id": repo_id, "run_id": run_id})


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
