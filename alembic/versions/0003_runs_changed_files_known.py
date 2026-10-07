"""add runs.changed_files_known

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing runs were uploaded without the flag; treat their changed_files as known.
    op.add_column(
        "runs",
        sa.Column("changed_files_known", sa.Boolean(), server_default=sa.true(), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("runs", "changed_files_known")
