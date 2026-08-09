"""player forecast cross-season provenance

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-09 16:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0005'
down_revision: Union[str, None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'player_forecasts',
        sa.Column('stat_source', sa.Text(), nullable=True),
    )
    op.add_column(
        'player_forecasts',
        sa.Column('has_history', sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('player_forecasts', 'has_history')
    op.drop_column('player_forecasts', 'stat_source')
