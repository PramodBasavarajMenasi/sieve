"""SQLAlchemy ORM models.

``runs``, ``changed_files`` and ``test_results`` are append-only raw ingest data.
``test_stats`` is a rollup recomputed from them and may be rebuilt at any time.
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    TypeDecorator,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, column_property, mapped_column

from sieve.core.junit import Status

# Deterministic constraint names so Alembic autogenerate and downgrades are stable.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


RUN_UNIQUE_INDEX = "uq_runs_repo_ci_run_attempt_variant"

MESSAGE_MAX_BYTES = 4096
TRUNCATION_MARKER = "…[truncated]"


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Fit ``value`` into ``max_bytes`` of UTF-8, ending with ``TRUNCATION_MARKER`` if cut.

    Never splits a multi-byte character.
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    marker = TRUNCATION_MARKER.encode("utf-8")
    head = encoded[: max(max_bytes - len(marker), 0)].decode("utf-8", errors="ignore")
    return head + TRUNCATION_MARKER


class TruncatedText(TypeDecorator[str]):
    """TEXT that truncates on write. Applies to ORM and Core (incl. bulk) inserts."""

    impl = Text
    cache_ok = True

    def __init__(self, max_bytes: int) -> None:
        super().__init__()
        self.max_bytes = max_bytes

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        return None if value is None else truncate_utf8(value, self.max_bytes)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index(None, "repo_id", "commit_sha"),
        Index(None, "repo_id", "is_main", "created_at"),
        # Idempotent ingest key, one row per matrix variant. NULL variants dedupe with each
        # other via COALESCE; a NULL ci_run_id never conflicts (Postgres NULLs are distinct).
        Index(
            RUN_UNIQUE_INDEX,
            "repo_id",
            "ci_run_id",
            "run_attempt",
            text("COALESCE(variant, '')"),
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    commit_sha: Mapped[str] = mapped_column(String(64))
    branch: Mapped[str] = mapped_column(String(255))
    is_main: Mapped[bool] = mapped_column(Boolean)
    ci_run_id: Mapped[str | None] = mapped_column(String(255))
    run_attempt: Mapped[int] = mapped_column(Integer, server_default="1")
    # Matrix leg of the CI run (e.g. "ubuntu-py3.12"); one row per variant. NULL = no matrix.
    variant: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # False when the uploader could not determine the diff (so changed_files is incomplete or
    # empty). Selection must then fall back to the full suite.
    changed_files_known: Mapped[bool] = mapped_column(Boolean, server_default=true())

    # When the run happened. Use this (never created_at alone) wherever "latest" matters:
    # backfill ingests old runs late, so created_at is ingest time, not run time.
    occurred_at: Mapped[datetime] = column_property(func.coalesce(started_at, created_at))


class ChangedFile(Base):
    __tablename__ = "changed_files"

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    path: Mapped[str] = mapped_column(Text, primary_key=True)


class TestResult(Base):
    __tablename__ = "test_results"
    __test__ = False  # not a pytest test class
    __table_args__ = (
        # Failures only (a tiny fraction of rows): the selector's co-change and recently-failed
        # queries read failures through this, never the passing results around them.
        Index(
            "ix_test_results_failed",
            "run_id",
            "test_id",
            postgresql_where=text("status IN ('failed', 'error')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    test_id: Mapped[str] = mapped_column(Text, index=True)
    file_path: Mapped[str | None] = mapped_column(Text)
    status: Mapped[Status] = mapped_column(
        Enum(
            Status,
            name="test_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda enum: [member.value for member in enum],
        )
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    attempt: Mapped[int] = mapped_column(Integer, server_default="1")
    message: Mapped[str | None] = mapped_column(TruncatedText(MESSAGE_MAX_BYTES))


class TestStats(Base):
    __tablename__ = "test_stats"
    __test__ = False
    __table_args__ = (Index(None, "repo_id", "last_seen_run_id"),)

    repo_id: Mapped[int] = mapped_column(
        ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    test_id: Mapped[str] = mapped_column(Text, primary_key=True)
    runs: Mapped[int] = mapped_column(Integer, server_default="0")
    failures: Mapped[int] = mapped_column(Integer, server_default="0")
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flaky_score: Mapped[float] = mapped_column(Float, server_default="0")
    # Earliest start of a current failing streak on main, across variants.
    broken_on_main_since_sha: Mapped[str | None] = mapped_column(String(64))
    avg_duration_ms: Mapped[float | None] = mapped_column(Float)
    # Which variants are broken on main: [{"variant": str | None, "since_sha": str}], oldest
    # streak first. NULL when not broken on any variant.
    broken_on_main_variants: Mapped[list[dict[str, str | None]] | None] = mapped_column(JSONB)
    # The test's most recent appearance (any status) in the window: the run's time, and one
    # run (variant) of the latest CI run it appeared in, by the selector's CI-run order. The
    # selector's "known tests" are those whose last-seen run is in the repo's latest CI runs.
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_run_id: Mapped[int | None] = mapped_column(BigInteger)
    # Test file as reported in the window (latest non-null), for the selector's commands.
    file_path: Mapped[str | None] = mapped_column(Text)
