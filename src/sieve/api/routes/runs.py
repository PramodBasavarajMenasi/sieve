"""POST /runs: ingest a CI run's JUnit XML. GET /runs/lookup: check whether one is stored."""

from collections import Counter
from dataclasses import replace
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import ValidationError
from sqlalchemy.orm import Session

from sieve.core.ingest import count_results, create_run, find_existing_run, find_run
from sieve.core.junit import JUnitParseError, ParsedTestResult, parse_junit
from sieve.core.schemas import RunLookupResponse, RunMetadata, RunResponse
from sieve.db import get_session

# Auth is enforced by BearerAuthMiddleware, before the request body is read.
router = APIRouter()


@router.get(
    "/runs/lookup",
    responses={404: {"description": "No run recorded for this CI run attempt"}},
)
def lookup_run(
    session: Annotated[Session, Depends(get_session)],
    repo: Annotated[str, Query(min_length=1)],
    ci_run_id: Annotated[str, Query(min_length=1)],
    run_attempt: Annotated[int, Query(ge=1)] = 1,
) -> RunLookupResponse:
    """Whether a CI run attempt is already ingested, so uploaders can skip the work."""
    run = find_run(session, repo, ci_run_id, run_attempt)
    if run is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"no run {ci_run_id} attempt {run_attempt} recorded for {repo!r}",
        )
    return RunLookupResponse(
        run_id=run.id,
        repo=repo,
        ci_run_id=ci_run_id,
        run_attempt=run_attempt,
        commit_sha=run.commit_sha,
    )


@router.post(
    "/runs",
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"model": RunResponse, "description": "Run was already uploaded"},
        400: {"description": "Invalid JUnit XML"},
        401: {"description": "Missing or invalid bearer token"},
        409: {"description": "Run already recorded for a different commit"},
        413: {"description": "Upload too large"},
    },
)
def post_run(
    response: Response,
    session: Annotated[Session, Depends(get_session)],
    files: Annotated[list[UploadFile], File(description="One or more JUnit XML files")],
    metadata: Annotated[str, Form(description="RunMetadata as a JSON string")],
) -> RunResponse:
    """Record a CI run. Idempotent per ``(repo, ci_run_id, run_attempt)``."""
    try:
        meta = RunMetadata.model_validate_json(metadata)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            [
                {"loc": ["metadata", *e["loc"]], "msg": e["msg"], "type": e["type"]}
                for e in exc.errors()
            ],
        ) from exc

    # Re-uploads (e.g. re-running backfill) skip parsing entirely.
    existing = find_existing_run(session, meta)
    if existing is None:
        results = _parse_files(files)
        run, created = create_run(session, meta, results)
    else:
        run, created = existing, False

    if not created:
        if run.commit_sha != meta.commit_sha:
            # Same CI run attempt claiming a different commit is a client bug, not a re-upload.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"run {meta.ci_run_id} attempt {meta.run_attempt} of {meta.repo} is already "
                f"recorded for commit {run.commit_sha}, not {meta.commit_sha}",
            )
        response.status_code = status.HTTP_200_OK
    return RunResponse(
        run_id=run.id,
        repo=meta.repo,
        created=created,
        counts=count_results(session, run.id),
    )


def _parse_files(files: list[UploadFile]) -> list[ParsedTestResult]:
    results: list[ParsedTestResult] = []
    # The parser numbers attempts per file; continue the numbering across files so a test
    # that appears in several files gets attempts 1, 2, ... within the run.
    seen: Counter[str] = Counter()
    for upload in files:
        try:
            parsed = parse_junit(upload.file.read())
        except JUnitParseError as exc:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, f"{upload.filename or 'upload'}: {exc}"
            ) from exc
        offset = dict(seen)
        for result in parsed:
            if result.test_id in offset:
                result = replace(result, attempt=result.attempt + offset[result.test_id])
            seen[result.test_id] = max(seen[result.test_id], result.attempt)
            results.append(result)
    return results
