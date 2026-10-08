"""POST /select: which tests to run for a change."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from sieve.core.schemas import SelectedTestOut, SelectRequest, SelectResponse
from sieve.core.selector import SelectorConfig, load_history, select_tests
from sieve.db import get_session

router = APIRouter()


@router.post("/select", responses={404: {"description": "Unknown repo"}})
def post_select(
    body: SelectRequest, session: Annotated[Session, Depends(get_session)]
) -> SelectResponse:
    """Select tests for a change. Falls back to the full suite whenever unsure."""
    config = SelectorConfig()
    history = load_history(session, body.repo, config)
    if history is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown repo {body.repo!r}")
    selection = select_tests(history, body.changed_files, body.changed_files_known, config)
    return SelectResponse(
        repo=body.repo,
        mode=selection.mode.value,
        reason=selection.reason,
        selected_count=selection.selected_count,
        total_known=selection.total_known,
        command=selection.command,
        commands=list(selection.commands),
        tests=[
            SelectedTestOut(
                test_id=t.test_id,
                reasons=list(t.reasons),
                reason=t.reason,
                runner=t.runner.value if t.runner else None,
                file_path=t.file_path,
            )
            for t in selection.tests
        ],
    )
