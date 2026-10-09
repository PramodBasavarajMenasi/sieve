"""fast /select history: test_stats last seen + file path, partial index on failed results

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-09 18:00:00.000000

``load_history`` now reads known tests from ``test_stats`` instead of scanning raw results, so
existing stats need the new columns filled: run ``POST /repos/{repo}/rollup`` for each repo
after upgrading. Until then the repo has no known tests and /select returns the full suite.

Building the partial index reads all of ``test_results`` once (minutes on tens of millions
of rows) and blocks writes to it meanwhile.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("test_stats", sa.Column("last_seen_at", sa.DateTime(timezone=True)))
    op.add_column("test_stats", sa.Column("last_seen_run_id", sa.BigInteger()))
    op.add_column("test_stats", sa.Column("file_path", sa.Text()))
    op.create_index(
        "ix_test_stats_repo_id_last_seen_run_id", "test_stats", ["repo_id", "last_seen_run_id"]
    )
    op.create_index(
        "ix_test_results_failed",
        "test_results",
        ["run_id", "test_id"],
        postgresql_where=sa.text("status IN ('failed', 'error')"),
    )


def downgrade() -> None:
    op.drop_index("ix_test_results_failed", table_name="test_results")
    op.drop_index("ix_test_stats_repo_id_last_seen_run_id", table_name="test_stats")
    op.drop_column("test_stats", "file_path")
    op.drop_column("test_stats", "last_seen_run_id")
    op.drop_column("test_stats", "last_seen_at")
