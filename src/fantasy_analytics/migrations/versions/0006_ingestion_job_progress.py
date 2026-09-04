"""ingestion job progress columns

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-09 18:05:00.000000

Adds the coarse progress fields the admin refresh UI (development-plan step 17)
polls while a manual ingestion job runs. They are nullable because a job that
was queued before this revision (or one that never reported progress) simply has
no stage yet.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0006'
down_revision: Union[str, None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'ingestion_jobs', sa.Column('progress_stage', sa.Text(), nullable=True)
    )
    op.add_column(
        'ingestion_jobs', sa.Column('progress_percent', sa.Integer(), nullable=True)
    )
    op.add_column(
        'ingestion_jobs', sa.Column('progress_message', sa.Text(), nullable=True)
    )
    op.add_column(
        'ingestion_jobs',
        sa.Column('progress_updated_at', sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('ingestion_jobs', 'progress_updated_at')
    op.drop_column('ingestion_jobs', 'progress_message')
    op.drop_column('ingestion_jobs', 'progress_percent')
    op.drop_column('ingestion_jobs', 'progress_stage')
