"""GET /tests/{test_id}/history: per-test stats and recent results, for debugging."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from siftwise.core.history import get_recent_results, get_test_stats
from siftwise.core.schemas import TestHistoryResponse, TestResultOut, TestStatsOut
from siftwise.db import get_session

router = APIRouter()

HISTORY_LIMIT = 20


# test_id contains "::" and often "/" (Go subtests, Jest titles, pytest params), so it is a
# path parameter. Clients must still percent-encode characters such as "?", "#" and "%".
@router.get(
    "/tests/{test_id:path}/history",
    responses={404: {"description": "No history for this test in this repo"}},
)
def get_test_history(
    test_id: str,
    repo: Annotated[str, Query(min_length=1, description="Repository name, e.g. acme/shop")],
    session: Annotated[Session, Depends(get_session)],
) -> TestHistoryResponse:
    stats = get_test_stats(session, repo, test_id)
    if stats is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"no history for test {test_id!r} in repo {repo!r}"
        )
    rows = get_recent_results(session, stats.repo_id, test_id, HISTORY_LIMIT)
    return TestHistoryResponse(
        repo=repo,
        test_id=test_id,
        stats=TestStatsOut.model_validate(stats),
        results=[
            TestResultOut(
                run_id=run.id,
                commit_sha=run.commit_sha,
                branch=run.branch,
                is_main=run.is_main,
                ci_run_id=run.ci_run_id,
                run_attempt=run.run_attempt,
                variant=run.variant,
                occurred_at=run.occurred_at,
                status=result.status,
                attempt=result.attempt,
                duration_ms=result.duration_ms,
                file_path=result.file_path,
                message=result.message,
            )
            for result, run in rows
        ],
    )
