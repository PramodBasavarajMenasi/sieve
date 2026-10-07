"""Store an uploaded CI run and its parsed test results."""

from collections.abc import Iterable

from psycopg.errors import UniqueViolation
from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sieve.core.junit import ParsedTestResult
from sieve.core.models import ChangedFile, Repo, Run, TestResult
from sieve.core.schemas import RunMetadata, StatusCounts

RUN_UNIQUE_CONSTRAINT = "uq_runs_repo_id_ci_run_id_run_attempt"


def find_existing_run(session: Session, meta: RunMetadata) -> Run | None:
    """The run already stored for this CI run attempt, if any. Runs without a CI id never match."""
    if meta.ci_run_id is None:
        return None
    return session.scalars(
        select(Run)
        .join(Repo, Run.repo_id == Repo.id)
        .where(
            Repo.name == meta.repo,
            Run.ci_run_id == meta.ci_run_id,
            Run.run_attempt == meta.run_attempt,
        )
    ).one_or_none()


def create_run(
    session: Session, meta: RunMetadata, results: Iterable[ParsedTestResult]
) -> tuple[Run, bool]:
    """Insert the run, its changed files and results in one transaction.

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
            started_at=meta.started_at,
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
            session.execute(insert(TestResult), rows)
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if not _is_violation_of(exc, RUN_UNIQUE_CONSTRAINT):
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
