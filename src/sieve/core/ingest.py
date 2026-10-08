"""Store an uploaded CI run and its parsed test results."""

from collections.abc import Iterable

from psycopg.errors import UniqueViolation
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sieve.core.history import recompute_test_stats
from sieve.core.junit import ParsedTestResult
from sieve.core.models import RUN_UNIQUE_INDEX, ChangedFile, Repo, Run, TestResult
from sieve.core.schemas import RunMetadata, StatusCounts


def find_run(
    session: Session, repo: str, ci_run_id: str, run_attempt: int, variant: str | None = None
) -> Run | None:
    """The run stored for this CI run attempt and matrix variant, if any."""
    return session.scalars(
        select(Run)
        .join(Repo, Run.repo_id == Repo.id)
        .where(
            Repo.name == repo,
            Run.ci_run_id == ci_run_id,
            Run.run_attempt == run_attempt,
            Run.variant.is_(None) if variant is None else Run.variant == variant,
        )
    ).one_or_none()


def find_existing_run(session: Session, meta: RunMetadata) -> Run | None:
    """The run already stored for this upload, if any. Runs without a CI id never match."""
    if meta.ci_run_id is None:
        return None
    return find_run(session, meta.repo, meta.ci_run_id, meta.run_attempt, meta.variant)


def create_run(
    session: Session, meta: RunMetadata, results: Iterable[ParsedTestResult]
) -> tuple[Run, bool]:
    """Insert the run, its changed files and results, and roll up ``test_stats`` for the
    run's tests, all in one transaction.

    Returns ``(run, created)``. If a concurrent upload of the same CI run attempt wins the
    race, this transaction is rolled back and the winner's run is returned with
    ``created=False``.
    """
    try:
        repo_id = _get_or_create_repo(session, meta.repo)
        run = Run(
            repo_id=repo_id,
            commit_sha=meta.commit_sha,
            branch=meta.branch,
            is_main=meta.is_main,
            ci_run_id=meta.ci_run_id,
            run_attempt=meta.run_attempt,
            variant=meta.variant,
            started_at=meta.started_at,
            changed_files_known=meta.changed_files_known,
        )
        session.add(run)
        session.flush()

        if meta.changed_files:
            session.execute(
                insert(ChangedFile), [{"run_id": run.id, "path": p} for p in meta.changed_files]
            )
        rows = [
            {
                "run_id": run.id,
                "test_id": r.test_id,
                "file_path": r.file_path,
                "status": r.status,
                "duration_ms": r.duration_ms,
                "attempt": r.attempt,
                "message": r.message,
            }
            for r in results
        ]
        if rows:
            # render_nulls: by default the ORM drops None-valued keys and starts a new batch
            # whenever the key set changes, so rows alternating message=None / message="..."
            # went one or two per round trip (~20s for 4k rows over Docker networking).
            session.execute(insert(TestResult).execution_options(render_nulls=True), rows)
        # Same transaction: stats never reflect a run that failed to commit, or vice versa.
        recompute_test_stats(session, repo_id, run.id)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if not _is_violation_of(exc, RUN_UNIQUE_INDEX):
            raise
        existing = find_existing_run(session, meta)
        if existing is None:  # pragma: no cover - the winner's row must be visible after commit
            raise
        return existing, False
    return run, True


def count_results(session: Session, run_id: int) -> StatusCounts:
    rows = session.execute(
        select(TestResult.status, func.count())
        .where(TestResult.run_id == run_id)
        .group_by(TestResult.status)
    ).tuples()
    counts = {status.value: n for status, n in rows}
    return StatusCounts(**counts, total=sum(counts.values()))


def _get_or_create_repo(session: Session, name: str) -> int:
    # ON CONFLICT so concurrent first uploads for a new repo don't fail.
    session.execute(
        pg_insert(Repo).values(name=name).on_conflict_do_nothing(index_elements=[Repo.name])
    )
    return session.scalars(select(Repo.id).where(Repo.name == name)).one()


def _is_violation_of(exc: IntegrityError, constraint: str) -> bool:
    orig = exc.orig
    return isinstance(orig, UniqueViolation) and orig.diag.constraint_name == constraint
