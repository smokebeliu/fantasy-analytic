"""appearances in matches the fantasy does not score

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-15 12:00:00.000000

Sports.ru dropped the UCL 2026/27 second round from the fantasy calendar
after its matches were played, so no fantasy history covers them — yet a red
card shown there still bans the player from the next match. The import reads
such matches from the stat API into ``unscored_appearances`` (minutes and
cards only), and the suspension check is the one place that uses them.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "unscored_appearances",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("player_season_id", sa.BigInteger(), nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=False),
        sa.Column("ingestion_run_id", sa.BigInteger(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("yellow_cards", sa.Integer(), nullable=False),
        sa.Column("red_cards", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["player_season_id"], ["player_seasons.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "player_season_id", "match_id", name="unscored_appearances_player_match_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("unscored_appearances")
