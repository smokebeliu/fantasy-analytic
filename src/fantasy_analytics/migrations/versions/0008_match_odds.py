"""match betting odds for the next-tour forecast

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-25 12:00:00.000000

Stores the 1x2 bookmaker line for upcoming fixtures so the event forecast can
blend market-implied expected goals into ``expected_points``. The optimizer
never reads this table.

Also caches the Sports.ru football-tag id on ``competitions`` (the calendar
widget is addressed by tag, not by the fantasy tournament slug) and stamps
``seasons.odds_synced_at`` so the admin screen can show when the line was last
refreshed.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "competitions", sa.Column("sports_tag_id", sa.Text(), nullable=True)
    )
    op.add_column(
        "seasons",
        sa.Column("odds_synced_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "match_odds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("season_id", sa.BigInteger(), nullable=False),
        sa.Column("match_id", sa.BigInteger(), nullable=True),
        sa.Column("tour_id", sa.BigInteger(), nullable=True),
        sa.Column("stat_match_id", sa.Text(), nullable=False),
        sa.Column("home_stat_team_id", sa.Text(), nullable=True),
        sa.Column("away_stat_team_id", sa.Text(), nullable=True),
        sa.Column("bookmaker", sa.Text(), nullable=True),
        sa.Column("home_odds", sa.Numeric(8, 4), nullable=False),
        sa.Column("draw_odds", sa.Numeric(8, 4), nullable=False),
        sa.Column("away_odds", sa.Numeric(8, 4), nullable=False),
        sa.Column("implied_home", sa.Numeric(8, 6), nullable=False),
        sa.Column("implied_draw", sa.Numeric(8, 6), nullable=False),
        sa.Column("implied_away", sa.Numeric(8, 6), nullable=False),
        sa.Column("expected_home_goals", sa.Numeric(8, 4), nullable=False),
        sa.Column("expected_away_goals", sa.Numeric(8, 4), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
        sa.UniqueConstraint("stat_match_id", name="match_odds_stat_match_key"),
    )
    op.create_index("match_odds_season_idx", "match_odds", ["season_id"])
    op.create_index("match_odds_match_idx", "match_odds", ["match_id"])


def downgrade() -> None:
    op.drop_index("match_odds_match_idx", table_name="match_odds")
    op.drop_index("match_odds_season_idx", table_name="match_odds")
    op.drop_table("match_odds")
    op.drop_column("seasons", "odds_synced_at")
    op.drop_column("competitions", "sports_tag_id")
