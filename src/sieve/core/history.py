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
_ROLLUP_SQL = text(
    """
WITH affected AS (
    SELECT DISTINCT test_id FROM test_results WHERE run_id = :run_id
),
results AS (
    SELECT tr.id, tr.test_id, tr.run_id, tr.status, tr.attempt, tr.duration_ms,
           ru.commit_sha, ru.is_main, COALESCE(ru.started_at, ru.created_at) AS run_at
    FROM test_results tr
    JOIN affected USING (test_id)
    JOIN runs ru ON ru.id = tr.run_id
    WHERE ru.repo_id = :repo_id
),
outcomes AS (  -- one row per (test, run): the final attempt
    SELECT DISTINCT ON (test_id, run_id) test_id, run_id, status, commit_sha, is_main, run_at
    FROM results
    ORDER BY test_id, run_id, attempt DESC, id DESC
),
run_stats AS (
    SELECT test_id,
           count(*) FILTER (WHERE status <> 'skipped') AS runs,
           count(*) FILTER (WHERE status IN ('failed', 'error')) AS failures,
           max(run_at) FILTER (WHERE status IN ('failed', 'error')) AS last_failed_at
    FROM outcomes
    GROUP BY test_id
),
durations AS (
    SELECT test_id, avg(duration_ms) FILTER (WHERE status <> 'skipped')::float8 AS avg_ms
    FROM results
    GROUP BY test_id
),
per_commit AS (
    SELECT test_id, commit_sha,
           bool_or(status IN ('failed', 'error')) AND bool_or(status = 'passed') AS flaky
    FROM results
    WHERE status <> 'skipped'
    GROUP BY test_id, commit_sha
),
flakiness AS (
    SELECT test_id, avg(CASE WHEN flaky THEN 1.0 ELSE 0.0 END)::float8 AS score
    FROM per_commit
    GROUP BY test_id
),
main_outcomes AS (  -- rn = 1 is the latest non-skipped outcome on main
    SELECT test_id, commit_sha, status,
           row_number() OVER (PARTITION BY test_id ORDER BY run_at DESC, run_id DESC) AS rn
    FROM outcomes
    WHERE is_main AND status <> 'skipped'
),
latest_main_pass AS (
    SELECT test_id, min(rn) AS rn FROM main_outcomes WHERE status = 'passed' GROUP BY test_id
),
broken AS (  -- everything newer than the latest pass is a failure; take the oldest of those
    SELECT DISTINCT ON (m.test_id) m.test_id, m.commit_sha
    FROM main_outcomes m
    LEFT JOIN latest_main_pass p USING (test_id)
    WHERE p.rn IS NULL OR m.rn < p.rn
    ORDER BY m.test_id, m.rn DESC
)
INSERT INTO test_stats AS ts (
    repo_id, test_id, runs, failures, last_failed_at, flaky_score, avg_duration_ms,
    broken_on_main_since_sha
)
SELECT :repo_id, s.test_id, s.runs, s.failures, s.last_failed_at,
       COALESCE(f.score, 0), d.avg_ms, b.commit_sha
FROM run_stats s
LEFT JOIN flakiness f USING (test_id)
LEFT JOIN durations d USING (test_id)
LEFT JOIN broken b USING (test_id)
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
