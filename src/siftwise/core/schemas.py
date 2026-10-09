"""Pydantic request/response schemas."""

from datetime import datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

from siftwise.core.junit import Status, normalize_path

NonEmpty255 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


def _blank_to_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


# Matrix variant of a CI run, e.g. "ubuntu-py3.12". Blank means "no variant" (None).
Variant = Annotated[NonEmpty255 | None, BeforeValidator(_blank_to_none)]


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
    variant: Variant = Field(
        default=None,
        description="Matrix leg (OS, Python version...). Each variant of a CI run attempt is "
        "its own run; results are only compared within the same variant.",
    )
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
    stats_deferred: bool = Field(
        default=False,
        description="True if uploaded with defer_rollup: test_stats are stale until "
        "POST /repos/{repo}/rollup",
    )


class RollupResponse(BaseModel):
    repo: str
    tests_updated: int
    window_days: int = Field(description="Stats window (0 = all history)")
    seconds: float


class RunLookupResponse(BaseModel):
    run_id: int
    repo: str
    ci_run_id: str
    run_attempt: int
    variant: str | None
    commit_sha: str


Glob = Annotated[str, StringConstraints(min_length=1, max_length=1024)]


class DependsRuleIn(BaseModel):
    """A ``[[depends]]`` rule from the repo's .siftwise.toml."""

    model_config = ConfigDict(extra="forbid")

    tests: Glob
    on: list[Glob] = Field(min_length=1, max_length=100)


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
    affected_packages: dict[str, list[str]] = Field(
        default_factory=dict,
        max_length=20_000,
        description="Go package -> changed packages it imports, from `go list -deps -test`",
    )
    affected_files: dict[str, list[str]] = Field(
        default_factory=dict,
        max_length=50_000,
        description="Python test file -> changed modules it imports, from a static import graph",
    )
    deleted_files: list[Annotated[str, StringConstraints(max_length=4096)]] = Field(
        default_factory=list,
        max_length=50_000,
        description="Changed files that no longer exist at head; never named in a command",
    )
    depends: list[DependsRuleIn] = Field(default_factory=list, max_length=100)
    always_run: list[Glob] = Field(default_factory=list, max_length=1000)
    go_module: NonEmpty255 | None = None


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
    go_packages_run_whole: list[str] = Field(
        description="Go packages run without -run (changed, dependent or declared)"
    )
    python_files_run_whole: list[str] = Field(
        default_factory=list,
        description="Python files passed to pytest whole (changed or importing changed code, "
        "collection errors, doctest modules)",
    )
    tests: list[SelectedTestOut] = Field(description="Selected tests; empty in full mode")


class BrokenVariant(BaseModel):
    variant: str | None
    since_sha: str


class TestStatsOut(BaseModel):
    __test__ = False  # not a pytest test class
    model_config = ConfigDict(from_attributes=True)

    runs: int = Field(description="Runs (one per variant) where the test's outcome wasn't skipped")
    failures: int
    last_failed_at: datetime | None
    flaky_score: float = Field(
        description="Fraction of (commit, variant) pairs with both a failure and a pass"
    )
    avg_duration_ms: float | None
    broken_on_main_since_sha: str | None = Field(
        description="Earliest start of a failing streak on main, across variants"
    )
    broken_on_main_variants: list[BrokenVariant] = Field(
        default_factory=list, description="Each variant currently failing on main"
    )

    @field_validator("broken_on_main_variants", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return value or []


class TestResultOut(BaseModel):
    __test__ = False

    run_id: int
    commit_sha: str
    branch: str
    is_main: bool
    ci_run_id: str | None
    run_attempt: int
    variant: str | None
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
