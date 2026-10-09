"""POST /repos/{repo}/rollup: recompute a repo's test_stats after a batch ingest."""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from siftwise.api.deps import get_app_settings
from siftwise.config import Settings
from siftwise.core.history import recompute_repo_stats
from siftwise.core.models import Repo
from siftwise.core.schemas import RollupResponse
from siftwise.db import get_session

router = APIRouter()


# Repo names contain "/", hence the path converter.
@router.post(
    "/repos/{repo:path}/rollup",
    responses={404: {"description": "Unknown repo"}},
)
def rollup_repo(
    repo: str,
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> RollupResponse:
    """Recompute ``test_stats`` for every test with results in the stats window.

    Run once after uploading with ``POST /runs?defer_rollup=true``; until then the repo's
    stats (and the selector's broken-on-main signal) are stale.
    """
    repo_id = session.scalars(select(Repo.id).where(Repo.name == repo)).one_or_none()
    if repo_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {repo!r}")
    start = time.perf_counter()
    tests = recompute_repo_stats(session, repo_id, settings.stats_window_days)
    session.commit()
    return RollupResponse(
        repo=repo,
        tests_updated=tests,
        window_days=settings.stats_window_days,
        seconds=round(time.perf_counter() - start, 3),
    )
