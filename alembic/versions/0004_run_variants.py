"""matrix variants: runs.variant, per-variant unique key, test_stats.broken_on_main_variants

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-09 12:00:00.000000

Downgrading fails if two runs differ only by variant (the old key can't hold them).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("variant", sa.String(length=255), nullable=True))
    op.drop_constraint("uq_runs_repo_id_ci_run_id_run_attempt", "runs", type_="unique")
    # COALESCE so runs without a variant still dedupe with each other.
    op.create_index(
        "uq_runs_repo_ci_run_attempt_variant",
        "runs",
        ["repo_id", "ci_run_id", "run_attempt", sa.text("COALESCE(variant, '')")],
        unique=True,
    )
    op.add_column(
        "test_stats",
        sa.Column("broken_on_main_variants", postgresql.JSONB(), nullable=True),
    )
    # Existing runs have no variant: carry over the current broken-on-main state.
    op.execute(
        """
        UPDATE test_stats
        SET broken_on_main_variants = jsonb_build_array(
            jsonb_build_object('variant', NULL, 'since_sha', broken_on_main_since_sha))
        WHERE broken_on_main_since_sha IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("test_stats", "broken_on_main_variants")
    op.drop_index("uq_runs_repo_ci_run_attempt_variant", table_name="runs")
    op.create_unique_constraint(
        "uq_runs_repo_id_ci_run_id_run_attempt", "runs", ["repo_id", "ci_run_id", "run_attempt"]
    )
    op.drop_column("runs", "variant")
