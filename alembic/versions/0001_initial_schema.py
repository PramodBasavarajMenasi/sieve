"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-08 00:10:50.099279

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repos")),
        sa.UniqueConstraint("name", name=op.f("uq_repos_name")),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("commit_sha", sa.String(length=64), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=False),
        sa.Column("is_main", sa.Boolean(), nullable=False),
        sa.Column("ci_run_id", sa.String(length=255), nullable=True),
        sa.Column("run_attempt", sa.Integer(), server_default="1", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_runs_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint(
            "repo_id",
            "ci_run_id",
            "run_attempt",
            name=op.f("uq_runs_repo_id_ci_run_id_run_attempt"),
        ),
    )
    op.create_index(op.f("ix_runs_repo_id_commit_sha"), "runs", ["repo_id", "commit_sha"])
    op.create_index(
        op.f("ix_runs_repo_id_is_main_created_at"), "runs", ["repo_id", "is_main", "created_at"]
    )
    op.create_table(
        "changed_files",
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_changed_files_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id", "path", name=op.f("pk_changed_files")),
    )
    op.create_table(
        "test_results",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("test_id", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=True),
        # Plain VARCHAR + named CHECK (not a native PG enum) so statuses can change cheaply.
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default="1", nullable=False),
        # Truncated to 4 KB by the ORM column type (sieve.core.models.TruncatedText).
        sa.Column("message", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'skipped', 'error')",
            name=op.f("ck_test_results_test_status"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_test_results_run_id_runs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_results")),
    )
    op.create_index(op.f("ix_test_results_run_id"), "test_results", ["run_id"])
    op.create_index(op.f("ix_test_results_test_id"), "test_results", ["test_id"])
    op.create_table(
        "test_stats",
        sa.Column("repo_id", sa.Integer(), nullable=False),
        sa.Column("test_id", sa.Text(), nullable=False),
        sa.Column("runs", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flaky_score", sa.Float(), server_default="0", nullable=False),
        sa.Column("broken_on_main_since_sha", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["repo_id"], ["repos.id"], name=op.f("fk_test_stats_repo_id_repos"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("repo_id", "test_id", name=op.f("pk_test_stats")),
    )


def downgrade() -> None:
    op.drop_table("test_stats")
    op.drop_index(op.f("ix_test_results_test_id"), table_name="test_results")
    op.drop_index(op.f("ix_test_results_run_id"), table_name="test_results")
    op.drop_table("test_results")
    op.drop_table("changed_files")
    op.drop_index(op.f("ix_runs_repo_id_is_main_created_at"), table_name="runs")
    op.drop_index(op.f("ix_runs_repo_id_commit_sha"), table_name="runs")
    op.drop_table("runs")
    op.drop_table("repos")
