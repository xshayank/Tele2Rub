"""add queue-related columns to jobs

Adds format_hint and collection_name to the jobs table so that queued jobs
can be fully reconstructed and dispatched when a concurrency slot opens.

Revision ID: 0003_job_queue
Revises: 0002_user_job_limit
Create Date: 2026-05-05 00:00:00.000000
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003_job_queue"
down_revision: Union[str, None] = "0002_user_job_limit"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("format_hint", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("collection_name", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "collection_name")
    op.drop_column("jobs", "format_hint")
