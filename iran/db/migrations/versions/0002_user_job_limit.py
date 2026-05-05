"""add job limit columns to users

Revision ID: 0002_user_job_limit
Revises: 0001_initial_schema
Create Date: 2026-05-05 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_user_job_limit"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("job_limit", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("job_limit_start_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("job_limit_expires_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "job_limit_expires_at")
    op.drop_column("users", "job_limit_start_at")
    op.drop_column("users", "job_limit")
