"""append-only history of the 1x2 line

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-04 12:00:00.000000

``match_odds`` keeps one (the latest) line per fixture, which is what the
next-tour forecast wants but leaves nothing for a backtest to check the odds
weight against: by the time a season is over, every line has been overwritten
by the last refresh. ``match_odds_history`` records every capture, so a tour
replayed at its own cutoff can use the line that was known *then* (step 23).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "match_odds_history",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("season_id", sa.BigInteger(), nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=True),
        sa.Column("tour_id", sa.BigInteger(), nullable=True),
        sa.Column("stat_match_id", sa.Text(), nullable=False),
        sa.Column("bookmaker", sa.Text(), nullable=True),
        sa.Column("home_odds", sa.Numeric(8, 4), nullable=False),
        sa.Column("draw_odds", sa.Numeric(8, 4), nullable=False),
        sa.Column("away_odds", sa.Numeric(8, 4), nullable=False),
        sa.Column("implied_home", sa.Numeric(8, 6), nullable=False),
        sa.Column("implied_draw", sa.Numeric(8, 6), nullable=False),
        sa.Column("implied_away", sa.Numeric(8, 6), nullable=False),
        sa.Column("expected_home_goals", sa.Numeric(8, 4), nullable=False),
        sa.Column("expected_away_goals", sa.Numeric(8, 4), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "raw",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["season_id"], ["seasons.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["tour_id"], ["fantasy_tours.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stat_match_id", "captured_at", name="match_odds_history_capture_key"
        ),
    )
    op.create_index(
        "match_odds_history_season_captured_idx",
        "match_odds_history",
        ["season_id", "captured_at"],
    )
    op.create_index(
        "match_odds_history_match_idx", "match_odds_history", ["match_id"]
    )


def downgrade() -> None:
    op.drop_index("match_odds_history_match_idx", table_name="match_odds_history")
    op.drop_index(
        "match_odds_history_season_captured_idx", table_name="match_odds_history"
    )
    op.drop_table("match_odds_history")
