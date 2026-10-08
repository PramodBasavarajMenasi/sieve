import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text, update
from sqlalchemy.orm import Session

from sieve.api.main import create_app
from sieve.config import Settings
from sieve.core import history
from sieve.core.history import get_test_stats
from sieve.core.ingest import create_run
from sieve.core.junit import ParsedTestResult, Status
from sieve.core.models import Run, TestResult, TestStats
from sieve.core.schemas import RunMetadata
from sieve.db import get_session

REPO = "acme/shop"
T0 = datetime(2026, 1, 1, tzinfo=UTC)
P, F, E, S = Status.PASSED, Status.FAILED, Status.ERROR, Status.SKIPPED


def sha(n: int) -> str:
    return f"{n:040x}"


def at(hour: int) -> datetime:
    return T0 + timedelta(hours=hour)


def ingest(
    session: Session,
    *,
    commit: int,
    hour: int | None,
    tests: dict[str, Status | Sequence[Status]],
    is_main: bool = True,
    ci_run_id: str | None = None,
    run_attempt: int = 1,
    variant: str | None = None,
    repo: str = REPO,
    duration_ms: int | None = 10,
    window_days: int = 90,
) -> Run:
    """Ingest one run. A sequence of statuses means successive attempts of that test."""
    results = []
    for test_id, statuses in tests.items():
        attempts = [statuses] if isinstance(statuses, Status) else statuses
        for attempt, status in enumerate(attempts, start=1):
            results.append(
                ParsedTestResult(
                    test_id=test_id,
                    classname=test_id.split("::")[0],
                    name=test_id.split("::")[-1],
                    file_path=None,
                    status=status,
                    duration_ms=0 if status is S else duration_ms,
                    attempt=attempt,
                )
            )
    meta = RunMetadata(
        repo=repo,
        commit_sha=sha(commit),
        branch="main" if is_main else "feature",
        is_main=is_main,
        ci_run_id=ci_run_id,
        run_attempt=run_attempt,
        variant=variant,
        started_at=None if hour is None else at(hour),
    )
    run, created = create_run(session, meta, results, window_days=window_days)
    assert created
    return run


def stats(session: Session, test_id: str = "t::a", repo: str = REPO) -> TestStats:
    found = get_test_stats(session, repo, test_id)
    assert found is not None, f"no stats for {test_id}"
    return found


# --- basic stats --------------------------------------------------------------------------


def test_basic_stats(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": P}, duration_ms=10)
    ingest(db_session, commit=2, hour=2, tests={"t::a": F}, duration_ms=30)
    ingest(db_session, commit=3, hour=3, tests={"t::a": S})  # skipped: not counted anywhere

    s = stats(db_session)
    assert (s.runs, s.failures) == (2, 1)
    assert s.last_failed_at == at(2)
    assert s.avg_duration_ms == 20.0
    assert s.flaky_score == 0.0


def test_final_attempt_decides_run_outcome(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": [F, P]})

    s = stats(db_session)
    assert (s.runs, s.failures, s.last_failed_at) == (1, 0, None)
    assert s.broken_on_main_since_sha is None
    assert s.flaky_score == 1.0  # ...but the retry is what flaky_score catches


def test_only_skipped_test_still_gets_a_stats_row(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": S})

    s = stats(db_session)
    assert (s.runs, s.failures, s.flaky_score, s.avg_duration_ms) == (0, 0, 0.0, None)


# --- flaky --------------------------------------------------------------------------------


def test_flaky_score_is_fraction_of_commits_with_both_outcomes(db_session: Session) -> None:
    # commit 1: failed then passed on retry within one run -> flaky
    ingest(db_session, commit=1, hour=1, tests={"t::a": [F, P]})
    # commit 2: failed in CI attempt 1, passed in "re-run jobs" attempt 2 -> flaky
    ingest(db_session, commit=2, hour=2, tests={"t::a": F}, ci_run_id="200", run_attempt=1)
    ingest(db_session, commit=2, hour=3, tests={"t::a": P}, ci_run_id="200", run_attempt=2)
    # commit 3: consistently failing -> not flaky; commit 4: passing -> not flaky
    ingest(db_session, commit=3, hour=4, tests={"t::a": [F, F]})
    ingest(db_session, commit=4, hour=5, tests={"t::a": P})
    # commit 5: only skipped -> not in the denominator
    ingest(db_session, commit=5, hour=6, tests={"t::a": S})

    assert stats(db_session).flaky_score == 2 / 4


def test_error_counts_as_failure_for_flakiness(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": [E, P]})
    assert stats(db_session).flaky_score == 1.0


# --- broken on main -----------------------------------------------------------------------


def test_broken_on_main_set_and_cleared(db_session: Session) -> None:
    def broken_since() -> str | None:
        return stats(db_session).broken_on_main_since_sha

    ingest(db_session, commit=1, hour=1, tests={"t::a": P})
    assert broken_since() is None

    ingest(db_session, commit=2, hour=2, tests={"t::a": F})
    assert broken_since() == sha(2)

    ingest(db_session, commit=3, hour=3, tests={"t::a": E})
    assert broken_since() == sha(2)  # still the first commit of the streak

    ingest(db_session, commit=4, hour=4, tests={"t::a": P}, is_main=False)
    assert broken_since() == sha(2)  # a pass on a branch doesn't fix main

    ingest(db_session, commit=5, hour=5, tests={"t::a": S})
    assert broken_since() == sha(2)  # skipped neither breaks nor continues the streak

    ingest(db_session, commit=6, hour=6, tests={"t::a": P})
    assert broken_since() is None  # cleared once it passes on main

    ingest(db_session, commit=7, hour=7, tests={"t::a": F})
    assert broken_since() == sha(7)  # a new streak starts fresh


def test_branch_failures_do_not_mark_main_broken(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": F}, is_main=False)

    s = stats(db_session)
    assert s.failures == 1
    assert s.broken_on_main_since_sha is None


def test_flaky_pass_on_main_is_not_broken(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": F})
    ingest(db_session, commit=2, hour=2, tests={"t::a": [F, P]})
    assert stats(db_session).broken_on_main_since_sha is None


# --- matrix variants ----------------------------------------------------------------------

LINUX, MACOS = "ubuntu-py3.12", "macos-py3.12"


def test_platform_difference_is_not_flaky(db_session: Session) -> None:
    # Same commit, same CI run: passes on Linux, fails on macOS. A real platform difference.
    ingest(db_session, commit=1, hour=1, tests={"t::a": P}, ci_run_id="9", variant=LINUX)
    ingest(db_session, commit=1, hour=1, tests={"t::a": F}, ci_run_id="9", variant=MACOS)

    s = stats(db_session)
    assert s.flaky_score == 0.0
    assert (s.runs, s.failures) == (2, 1)  # one run per variant


def test_fail_and_pass_within_one_variant_is_flaky(db_session: Session) -> None:
    # macOS fails, then passes on a re-run of the same commit; Linux always passes.
    ingest(db_session, commit=1, hour=1, tests={"t::a": P}, ci_run_id="9", variant=LINUX)
    ingest(db_session, commit=1, hour=1, tests={"t::a": F}, ci_run_id="9", variant=MACOS)
    ingest(db_session, commit=1, hour=2, tests={"t::a": P}, ci_run_id="9", run_attempt=2,
           variant=MACOS)  # fmt: skip

    # (commit 1, linux) clean, (commit 1, macos) flaky: 1 of 2 pairs.
    assert stats(db_session).flaky_score == 0.5


def test_broken_on_main_is_tracked_per_variant(db_session: Session) -> None:
    def broken() -> tuple[str | None, list[dict[str, str | None]] | None]:
        s = stats(db_session)
        return s.broken_on_main_since_sha, s.broken_on_main_variants

    for variant in (LINUX, MACOS):
        ingest(db_session, commit=1, hour=1, tests={"t::a": P}, ci_run_id="1", variant=variant)
    assert broken() == (None, None)

    # macOS starts failing on commit 2; Linux keeps passing. Broken on main (on macOS only).
    ingest(db_session, commit=2, hour=2, tests={"t::a": P}, ci_run_id="2", variant=LINUX)
    ingest(db_session, commit=2, hour=2, tests={"t::a": F}, ci_run_id="2", variant=MACOS)
    assert broken() == (sha(2), [{"variant": MACOS, "since_sha": sha(2)}])

    # Linux breaks too on commit 3: both listed, oldest streak first; since = earliest.
    ingest(db_session, commit=3, hour=3, tests={"t::a": F}, ci_run_id="3", variant=LINUX)
    ingest(db_session, commit=3, hour=3, tests={"t::a": F}, ci_run_id="3", variant=MACOS)
    assert broken() == (
        sha(2),
        [{"variant": MACOS, "since_sha": sha(2)}, {"variant": LINUX, "since_sha": sha(3)}],
    )

    # macOS is fixed; Linux still failing.
    ingest(db_session, commit=4, hour=4, tests={"t::a": F}, ci_run_id="4", variant=LINUX)
    ingest(db_session, commit=4, hour=4, tests={"t::a": P}, ci_run_id="4", variant=MACOS)
    assert broken() == (sha(3), [{"variant": LINUX, "since_sha": sha(3)}])

    # Both pass: cleared.
    for variant in (LINUX, MACOS):
        ingest(db_session, commit=5, hour=5, tests={"t::a": P}, ci_run_id="5", variant=variant)
    assert broken() == (None, None)


def test_runs_without_variant_are_one_variant(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": F})
    assert stats(db_session).broken_on_main_variants == [{"variant": None, "since_sha": sha(1)}]


def test_history_api_reports_variants(client: TestClient, db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": P}, ci_run_id="1", variant=LINUX)
    ingest(db_session, commit=1, hour=1, tests={"t::a": F}, ci_run_id="1", variant=MACOS)

    body = client.get(history_url("t::a"), params={"repo": REPO}, headers=AUTH).json()

    assert body["stats"]["broken_on_main_variants"] == [{"variant": MACOS, "since_sha": sha(1)}]
    assert body["stats"]["flaky_score"] == 0.0
    assert sorted((r["variant"], r["status"]) for r in body["results"]) == [
        (MACOS, "failed"),
        (LINUX, "passed"),
    ]


# --- stats window -------------------------------------------------------------------------

DAY = 24  # hours


def test_stats_only_count_the_window(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=0, tests={"t::a": F}, duration_ms=1000)
    ingest(db_session, commit=2, hour=100 * DAY, tests={"t::a": P}, duration_ms=10)

    # Window = 90 days up to the newest run (day 100): the day-0 failure is outside it.
    s = stats(db_session)
    assert (s.runs, s.failures, s.last_failed_at, s.avg_duration_ms) == (1, 0, None, 10.0)


def test_window_of_zero_means_all_history(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=0, tests={"t::a": F}, window_days=0)
    ingest(db_session, commit=2, hour=100 * DAY, tests={"t::a": P}, window_days=0)

    assert (stats(db_session).runs, stats(db_session).failures) == (2, 1)


def test_window_is_anchored_on_the_newest_run_not_today(db_session: Session) -> None:
    # T0 is 2026-01-01; a "now"-anchored 90-day window would see none of this.
    ingest(db_session, commit=1, hour=0, tests={"t::a": F})
    assert stats(db_session).runs == 1


def test_broken_on_main_streak_start_is_found_before_the_window(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=0, tests={"t::a": P})
    ingest(db_session, commit=2, hour=10 * DAY, tests={"t::a": F})  # streak starts here
    ingest(db_session, commit=3, hour=50 * DAY, tests={"t::a": F})
    ingest(db_session, commit=4, hour=140 * DAY, tests={"t::a": F})

    # The window (day 50..140) only holds failures; the streak began at day 10.
    s = stats(db_session)
    assert s.broken_on_main_since_sha == sha(2)
    assert s.broken_on_main_variants == [{"variant": None, "since_sha": sha(2)}]
    assert s.runs == 2  # counts stay inside the window


def test_broken_on_main_lookback_respects_variants(db_session: Session) -> None:
    linux, macos = "ubuntu", "macos"
    ingest(db_session, commit=1, hour=0, tests={"t::a": F}, ci_run_id="1", variant=linux)
    ingest(db_session, commit=1, hour=0, tests={"t::a": P}, ci_run_id="1", variant=macos)
    ingest(db_session, commit=2, hour=200 * DAY, tests={"t::a": F}, ci_run_id="2", variant=linux)
    ingest(db_session, commit=2, hour=200 * DAY, tests={"t::a": F}, ci_run_id="2", variant=macos)

    # Linux has failed since commit 1 (before the window); macOS only since commit 2.
    assert stats(db_session).broken_on_main_variants == [
        {"variant": linux, "since_sha": sha(1)},
        {"variant": macos, "since_sha": sha(2)},
    ]


# Backfill ingests old runs last, so they get the highest run IDs. The window must be cut by
# run time (Run.occurred_at), never by ID: ID is only the lookup key and an ordering tiebreak.


def test_window_is_by_run_time_when_old_runs_are_ingested_last(db_session: Session) -> None:
    newest = ingest(db_session, commit=3, hour=100 * DAY, tests={"t::a": P}, duration_ms=10)
    ingest(db_session, commit=2, hour=50 * DAY, tests={"t::a": F}, duration_ms=20)  # in window
    old = ingest(db_session, commit=1, hour=0, tests={"t::a": F}, duration_ms=1000)  # outside

    assert old.id > newest.id
    # The day-0 run has the highest ID but is 100 days before the newest run: not counted,
    # and it doesn't move the window's anchor either.
    s = stats(db_session)
    assert (s.runs, s.failures, s.last_failed_at, s.avg_duration_ms) == (2, 1, at(50 * DAY), 15.0)


def test_repo_rollup_window_is_by_run_time_when_old_runs_are_ingested_last(
    db_session: Session,
) -> None:
    # Batch backfill: deferred uploads arrive newest first, then one repo rollup.
    for commit, day, status in [(3, 100, P), (2, 50, F), (1, 0, F)]:
        meta = RunMetadata(
            repo=REPO, commit_sha=sha(commit), branch="main", is_main=True, started_at=at(day * DAY)
        )
        result = ParsedTestResult("t::a", "t", "a", None, status, 10, 1)
        run, _ = create_run(db_session, meta, [result], rollup=False)

    history.recompute_repo_stats(db_session, run.repo_id, 90)

    s = stats(db_session)
    assert (s.runs, s.failures, s.last_failed_at) == (2, 1, at(50 * DAY))


def test_broken_on_main_lookback_is_by_run_time_when_old_runs_are_ingested_last(
    db_session: Session,
) -> None:
    ingest(db_session, commit=4, hour=140 * DAY, tests={"t::a": F})
    ingest(db_session, commit=3, hour=50 * DAY, tests={"t::a": F})
    ingest(db_session, commit=2, hour=10 * DAY, tests={"t::a": F})  # streak starts here
    ingest(db_session, commit=1, hour=0, tests={"t::a": P})  # oldest, highest ID

    # The streak start is found by time before the window, even though the runs were
    # ingested newest first (so ID order is the reverse of time order).
    s = stats(db_session)
    assert s.broken_on_main_since_sha == sha(2)
    assert s.runs == 2  # day 50 and day 140; days 0 and 10 are outside the window


def _rows_read_from(plan: dict[str, Any], table: str) -> int:
    """Rows actually read from ``table`` across an EXPLAIN (ANALYZE, FORMAT JSON) plan."""
    rows = 0
    if plan.get("Relation Name") == table:
        rows += plan.get("Actual Rows", 0) * plan.get("Actual Loops", 0)
    for child in plan.get("Plans", []):
        rows += _rows_read_from(child, table)
    return rows


def test_rollup_reads_only_the_window_not_old_history(db_session: Session) -> None:
    tests = [f"t::case{i}" for i in range(20)]
    # 300 old runs (a year before the window) of the same 20 tests, inserted directly.
    repo_run = ingest(db_session, commit=1, hour=0, tests=dict.fromkeys(tests, P))
    repo_id = repo_run.repo_id
    db_session.execute(
        text(
            """
            WITH old AS (
                INSERT INTO runs (repo_id, commit_sha, branch, is_main, ci_run_id, started_at)
                SELECT :repo_id, lpad(to_hex(n), 40, '0'), 'main', true, 'old-' || n,
                       CAST(:t0 AS timestamptz) - make_interval(days => 400) + n * interval '1 hour'
                FROM generate_series(1, 300) n
                RETURNING id
            )
            INSERT INTO test_results (run_id, test_id, status, duration_ms, attempt)
            SELECT old.id, t, 'passed', 5, 1 FROM old CROSS JOIN unnest(CAST(:tests AS text[])) t
            """
        ),
        {"repo_id": repo_id, "t0": T0, "tests": tests},
    )
    newest = ingest(db_session, commit=2, hour=1, tests=dict.fromkeys(tests, F))
    in_window = 2 * len(tests)  # two runs inside the window
    # Fresh statistics, as autovacuum would produce: without the OFFSET 0 fences the planner
    # then flattens the LATERAL lookups and hash-joins a scan of all 6,000+ rows.
    db_session.execute(text("ANALYZE test_results"))
    db_session.execute(text("ANALYZE runs"))

    plan = db_session.execute(
        text("EXPLAIN (ANALYZE, FORMAT JSON) " + str(history._RUN_ROLLUP_SQL)),
        {"repo_id": repo_id, "run_id": newest.id, "window_days": 90},
    ).scalar_one()[0]["Plan"]

    read = _rows_read_from(plan, "test_results")
    assert in_window <= read <= in_window * 2  # window rows (+ the run's test list), not 6,000
    assert db_session.scalar(select(func.count()).select_from(TestResult)) == 300 * 20 + in_window
    assert stats(db_session, "t::case0").runs == 2


# --- out-of-order (backfill) ingest -------------------------------------------------------


def test_old_run_ingested_after_newer_one(db_session: Session) -> None:
    # The newest run arrives first; backfill then delivers older runs.
    ingest(db_session, commit=3, hour=3, tests={"t::a": F})
    assert stats(db_session).broken_on_main_since_sha == sha(3)

    ingest(db_session, commit=1, hour=1, tests={"t::a": P})  # older pass must not clear it
    assert stats(db_session).broken_on_main_since_sha == sha(3)

    ingest(db_session, commit=2, hour=2, tests={"t::a": F})  # older failure extends the streak
    s = stats(db_session)
    assert s.broken_on_main_since_sha == sha(2)
    assert s.last_failed_at == at(3)  # latest by run time, not by ingest order
    assert (s.runs, s.failures) == (3, 2)


def test_old_failure_ingested_after_newer_pass_is_not_broken(db_session: Session) -> None:
    ingest(db_session, commit=2, hour=2, tests={"t::a": P})
    ingest(db_session, commit=1, hour=1, tests={"t::a": F})  # backfilled, older

    s = stats(db_session)
    assert s.broken_on_main_since_sha is None
    assert s.last_failed_at == at(1)


def test_runs_without_started_at_fall_back_to_ingest_time(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=None, tests={"t::a": F})
    s = stats(db_session)
    assert s.broken_on_main_since_sha == sha(1)
    assert s.last_failed_at is not None


# --- rollup scope and transaction ---------------------------------------------------------


def test_stats_updated_on_each_new_run(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": P, "t::b": P})
    assert (stats(db_session, "t::a").runs, stats(db_session, "t::b").runs) == (1, 1)

    ingest(db_session, commit=2, hour=2, tests={"t::a": F, "t::b": P})
    a, b = stats(db_session, "t::a"), stats(db_session, "t::b")
    assert (a.runs, a.failures, a.broken_on_main_since_sha) == (2, 1, sha(2))
    assert (b.runs, b.failures) == (2, 0)


def test_only_tests_in_the_run_are_recomputed(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": P, "t::b": P})
    db_session.execute(update(TestStats).where(TestStats.test_id == "t::a").values(runs=999))
    db_session.commit()

    ingest(db_session, commit=2, hour=2, tests={"t::b": P})

    assert stats(db_session, "t::a").runs == 999  # untouched: not in run 2
    assert stats(db_session, "t::b").runs == 2


def test_stats_are_per_repo(db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": F}, repo="acme/one")
    ingest(db_session, commit=2, hour=2, tests={"t::a": P}, repo="acme/two")

    assert stats(db_session, repo="acme/one").failures == 1
    assert stats(db_session, repo="acme/two").failures == 0


def test_results_are_inserted_in_one_batch(db_session: Session) -> None:
    # Regression: the ORM dropped None-valued columns and started a new INSERT batch each time
    # the column set changed, so alternating message/file_path None-ness meant ~1 row per
    # round trip (kubo backfill: ~23s per 4k-result run). All rows must share one statement.
    results = [
        ParsedTestResult(
            test_id=f"t::case{i}",
            classname="t",
            name=f"case{i}",
            file_path="tests/t.py" if i % 2 else None,
            status=F if i % 3 == 0 else P,
            duration_ms=i,
            attempt=1,
            message="boom" if i % 3 == 0 else None,
        )
        for i in range(200)
    ]
    meta = RunMetadata(repo=REPO, commit_sha=sha(1), branch="main", is_main=True)

    inserts: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.startswith("INSERT INTO test_results"):
            inserts.append(statement)

    engine = db_session.get_bind().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        create_run(db_session, meta, results)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(inserts) == 1
    stored = db_session.execute(
        select(TestResult.file_path, TestResult.message).where(TestResult.test_id == "t::case3")
    ).one()
    assert tuple(stored) == ("tests/t.py", "boom")  # values still land in the right columns


def test_run_is_not_committed_if_rollup_fails(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("rollup failed")

    monkeypatch.setattr("sieve.core.ingest.recompute_test_stats", boom)
    with pytest.raises(RuntimeError, match="rollup failed"):
        ingest(db_session, commit=1, hour=1, tests={"t::a": P})
    db_session.rollback()

    assert db_session.scalar(select(func.count()).select_from(Run)) == 0


# --- API ----------------------------------------------------------------------------------

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GO_SUBTEST = "github.com/acme/shop/cart::TestTotal/with_discount"


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, api_token=TOKEN))
    app.dependency_overrides[get_session] = lambda: db_session
    with TestClient(app) as test_client:
        yield test_client


def history_url(test_id: str) -> str:
    # Keep "/" and ":" literal to exercise the {test_id:path} route; encode everything else.
    return f"/tests/{quote(test_id, safe='/:')}/history"


def test_history_endpoint_with_slashes_in_test_id(client: TestClient, db_session: Session) -> None:
    for n in range(25):
        ingest(db_session, commit=n + 1, hour=n, tests={GO_SUBTEST: F if n == 24 else P})

    response = client.get(history_url(GO_SUBTEST), params={"repo": REPO}, headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["test_id"] == GO_SUBTEST
    assert body["stats"]["runs"] == 25
    assert body["stats"]["failures"] == 1
    assert body["stats"]["broken_on_main_since_sha"] == sha(25)
    assert len(body["results"]) == 20
    assert [r["commit_sha"] for r in body["results"][:2]] == [sha(25), sha(24)]
    assert body["results"][0]["status"] == "failed"


def test_history_results_are_ordered_by_run_time_not_ingest_order(
    client: TestClient, db_session: Session
) -> None:
    ingest(db_session, commit=2, hour=2, tests={"t::a": P})
    ingest(db_session, commit=1, hour=1, tests={"t::a": [F, P]})  # backfilled later

    body = client.get(history_url("t::a"), params={"repo": REPO}, headers=AUTH).json()

    assert [(r["commit_sha"], r["attempt"]) for r in body["results"]] == [
        (sha(2), 1),
        (sha(1), 2),
        (sha(1), 1),
    ]


def test_history_for_test_id_needing_percent_encoding(
    client: TestClient, db_session: Session
) -> None:
    test_id = "tests/test_io.py::test_read[a b?c#d%e]"
    ingest(db_session, commit=1, hour=1, tests={test_id: P})

    response = client.get(history_url(test_id), params={"repo": REPO}, headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["test_id"] == test_id


def test_unknown_test_returns_404(client: TestClient, db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": P})

    response = client.get(history_url("t::nope"), params={"repo": REPO}, headers=AUTH)

    assert response.status_code == 404
    assert "t::nope" in response.json()["detail"]


def test_unknown_repo_returns_404(client: TestClient, db_session: Session) -> None:
    ingest(db_session, commit=1, hour=1, tests={"t::a": P})
    response = client.get(history_url("t::a"), params={"repo": "acme/other"}, headers=AUTH)
    assert response.status_code == 404


def test_history_requires_repo_and_token(client: TestClient) -> None:
    assert client.get(history_url("t::a"), headers=AUTH).status_code == 422
    assert client.get(history_url("t::a"), params={"repo": REPO}).status_code == 401


def test_duplicate_upload_race_does_not_double_count(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    xml = b'<testsuite><testcase classname="t" name="a"/></testsuite>'
    meta = json.dumps(
        {"repo": REPO, "commit_sha": sha(1), "branch": "main", "is_main": True, "ci_run_id": "1"}
    )

    def upload() -> int:
        response = client.post(
            "/runs", files={"files": ("r.xml", xml)}, data={"metadata": meta}, headers=AUTH
        )
        status_code: int = response.status_code
        return status_code

    assert upload() == 201
    # Lose the race: the pre-check misses and the insert hits the unique constraint.
    monkeypatch.setattr("sieve.api.routes.runs.find_existing_run", lambda session, meta: None)
    assert upload() == 200

    assert stats(db_session).runs == 1
