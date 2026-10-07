import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from sieve.core.junit import Status
from sieve.core.models import (
    MESSAGE_MAX_BYTES,
    TRUNCATION_MARKER,
    Base,
    ChangedFile,
    Repo,
    Run,
    TestResult,
    TestStats,
    truncate_utf8,
)


def add_run(session: Session, repo_name: str = "acme/shop") -> Run:
    repo = Repo(name=repo_name)
    session.add(repo)
    session.flush()
    run = Run(repo_id=repo.id, commit_sha="a" * 40, branch="main", is_main=True)
    session.add(run)
    session.flush()
    return run


def test_migrations_match_models(db_engine: Engine) -> None:
    with db_engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_round_trip_and_defaults(db_session: Session) -> None:
    run = add_run(db_session)
    db_session.add_all(
        [
            ChangedFile(run_id=run.id, path="src/cart.py"),
            TestResult(run_id=run.id, test_id="tests.test_cart::test_total", status=Status.FAILED),
            TestStats(repo_id=run.repo_id, test_id="tests.test_cart::test_total"),
        ]
    )
    db_session.commit()
    db_session.expire_all()

    result = db_session.scalars(select(TestResult)).one()
    assert result.status is Status.FAILED
    assert result.attempt == 1
    assert db_session.scalar(text("SELECT status FROM test_results")) == "failed"

    stats = db_session.scalars(select(TestStats)).one()
    assert (stats.runs, stats.failures, stats.flaky_score) == (0, 0, 0.0)
    fetched = db_session.get(Run, run.id)
    assert fetched is not None
    assert fetched.created_at is not None


def test_status_check_constraint(db_session: Session) -> None:
    run = add_run(db_session)
    with pytest.raises(IntegrityError, match="ck_test_results_test_status"):
        db_session.execute(
            text("INSERT INTO test_results (run_id, test_id, status) VALUES (:r, 't', 'bogus')"),
            {"r": run.id},
        )


def test_repo_name_is_unique(db_session: Session) -> None:
    add_run(db_session, "acme/shop")
    with pytest.raises(IntegrityError, match="uq_repos_name"):
        add_run(db_session, "acme/shop")


def test_run_attempt_defaults_to_one(db_session: Session) -> None:
    run = add_run(db_session)
    db_session.expire_all()
    fetched = db_session.get(Run, run.id)
    assert fetched is not None
    assert fetched.run_attempt == 1


def test_ci_run_attempt_is_unique_per_repo(db_session: Session) -> None:
    run = add_run(db_session)

    def add(repo_id: int, ci_run_id: str | None, run_attempt: int) -> None:
        db_session.add(
            Run(
                repo_id=repo_id,
                commit_sha="b" * 40,
                branch="main",
                is_main=True,
                ci_run_id=ci_run_id,
                run_attempt=run_attempt,
            )
        )
        db_session.flush()

    add(run.repo_id, "123", 1)
    add(run.repo_id, "123", 2)  # GitHub "re-run jobs" is a new attempt, not a duplicate
    add(run.repo_id, None, 1)
    add(run.repo_id, None, 1)  # runs without a CI id are never deduplicated
    other = add_run(db_session, "acme/other")
    add(other.repo_id, "123", 1)  # same CI id in another repo is fine

    with pytest.raises(IntegrityError, match="uq_runs_repo_id_ci_run_id_run_attempt"):
        add(run.repo_id, "123", 1)


@pytest.mark.parametrize(
    ("value", "max_bytes", "expected"),
    [
        # TRUNCATION_MARKER is 14 bytes, leaving 6 for content when max_bytes=20.
        ("short", 20, "short"),
        ("a" * 20, 20, "a" * 20),
        ("a" * 21, 20, "aaaaaa" + TRUNCATION_MARKER),
        # "é" is 2 bytes; a cut through its middle drops the partial character.
        ("a" + "é" * 10, 20, "aéé" + TRUNCATION_MARKER),
    ],
)
def test_truncate_utf8(value: str, max_bytes: int, expected: str) -> None:
    truncated = truncate_utf8(value, max_bytes)
    assert truncated == expected
    assert len(truncated.encode()) <= max_bytes


def test_message_is_truncated_on_orm_and_bulk_insert(db_session: Session) -> None:
    run = add_run(db_session)
    long_message = "€" * 2000  # 3 bytes each = 6000 bytes
    db_session.add(
        TestResult(run_id=run.id, test_id="t::orm", status=Status.FAILED, message=long_message)
    )
    db_session.execute(
        insert(TestResult),
        [
            {
                "run_id": run.id,
                "test_id": "t::bulk",
                "status": Status.FAILED,
                "message": long_message,
            },
            {"run_id": run.id, "test_id": "t::none", "status": Status.PASSED, "message": None},
        ],
    )
    db_session.commit()

    # Read back with raw SQL so we see what Postgres stored, not the ORM's view of it.
    rows = db_session.execute(text("SELECT test_id, message FROM test_results"))
    stored: dict[str, str | None] = {test_id: message for test_id, message in rows}
    for test_id in ("t::orm", "t::bulk"):
        message = stored[test_id]
        assert message is not None
        assert len(message.encode()) <= MESSAGE_MAX_BYTES
        budget = MESSAGE_MAX_BYTES - len(TRUNCATION_MARKER.encode())
        assert message == "€" * (budget // 3) + TRUNCATION_MARKER
    assert stored["t::none"] is None


def test_deleting_repo_cascades(db_session: Session) -> None:
    run = add_run(db_session)
    db_session.add(TestResult(run_id=run.id, test_id="t::x", status=Status.PASSED))
    db_session.add(ChangedFile(run_id=run.id, path="a.py"))
    db_session.flush()

    db_session.execute(text("DELETE FROM repos"))
    for model in (Run, TestResult, ChangedFile):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0
