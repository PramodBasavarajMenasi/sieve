"""add test_stats.avg_duration_ms

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("test_stats", sa.Column("avg_duration_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("test_stats", "avg_duration_ms")
