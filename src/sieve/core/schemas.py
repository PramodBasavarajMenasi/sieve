"""Pydantic request/response schemas."""

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from sieve.core.junit import Status, normalize_path

NonEmpty255 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class RunMetadata(BaseModel):
    """JSON metadata sent alongside the JUnit files in ``POST /runs``."""

    model_config = ConfigDict(extra="forbid")

    repo: NonEmpty255
    commit_sha: Annotated[
        str,
        # Full 40-char SHA-1 only: abbreviated SHAs are ambiguous as run identity.
        StringConstraints(strip_whitespace=True, to_lower=True, pattern=r"^[0-9a-fA-F]{40}$"),
    ]
    branch: NonEmpty255
    is_main: bool
    ci_run_id: NonEmpty255 | None = None
    run_attempt: int = Field(default=1, ge=1)
    started_at: datetime | None = None
    changed_files: list[str] = Field(default_factory=list)
    changed_files_known: bool = Field(
        default=True,
        description="False if the uploader could not determine the diff; forces full-suite "
        "selection for this run.",
    )

    @field_validator("changed_files")
    @classmethod
    def _normalize_changed_files(cls, paths: list[str]) -> list[str]:
        # Normalized and de-duplicated (order kept): (run_id, path) is the table's primary key.
        normalized = (normalize_path(path) for path in paths)
        return list(dict.fromkeys(path for path in normalized if path))


class StatusCounts(BaseModel):
    """Result rows per status. Each retry attempt counts separately."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    error: int = 0
    total: int = 0


class RunResponse(BaseModel):
    run_id: int
    repo: str
    created: bool
    counts: StatusCounts


class RunLookupResponse(BaseModel):
    run_id: int
    repo: str
    ci_run_id: str
    run_attempt: int
    commit_sha: str


class SelectRequest(BaseModel):
    """Body of ``POST /select``."""

    model_config = ConfigDict(extra="forbid")

    repo: NonEmpty255
    changed_files: list[Annotated[str, StringConstraints(max_length=4096)]] = Field(
        default_factory=list, max_length=50_000
    )
    changed_files_known: bool = Field(
        default=True,
        description="False if the caller could not determine the diff; forces the full suite.",
    )


class SelectedTestOut(BaseModel):
    test_id: str
    reasons: list[str]
    reason: str = Field(description="All reasons, joined with '; '")
    runner: str | None
    file_path: str | None


class SelectResponse(BaseModel):
    repo: str
    mode: str = Field(description="'full' or 'selective'")
    reason: str
    selected_count: int
    total_known: int
    command: str = Field(description="Shell line to run; empty when no tests are affected")
    commands: list[str]
    tests: list[SelectedTestOut] = Field(description="Selected tests; empty in full mode")


class TestStatsOut(BaseModel):
    __test__ = False  # not a pytest test class
    model_config = ConfigDict(from_attributes=True)

    runs: int
    failures: int
    last_failed_at: datetime | None
    flaky_score: float
    avg_duration_ms: float | None
    broken_on_main_since_sha: str | None


class TestResultOut(BaseModel):
    __test__ = False

    run_id: int
    commit_sha: str
    branch: str
    is_main: bool
    ci_run_id: str | None
    run_attempt: int
    occurred_at: datetime
    status: Status
    attempt: int
    duration_ms: int | None
    file_path: str | None
    message: str | None


class TestHistoryResponse(BaseModel):
    __test__ = False

    repo: str
    test_id: str
    stats: TestStatsOut
    results: list[TestResultOut] = Field(description="Latest result rows, newest run first")
