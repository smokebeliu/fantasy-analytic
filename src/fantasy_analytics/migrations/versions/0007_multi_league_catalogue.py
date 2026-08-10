"""multi-league competition catalogue

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-10 02:10:00.000000

Makes the schema able to hold every fantasy league Sports.ru offers, not just the
RPL (development-plan step 22).

Two changes:

* ``competitions`` gains the catalogue Sports.ru publishes for a league — the
  display order the site itself uses and every season it exposes, imported or
  not. The admin UI reads it to offer a league/season picker without any read
  path calling the GraphQL API.
* ``seasons.stat_season_id`` loses its UNIQUE constraint. It was only ever true
  for domestic leagues: the Champions League and Europa League each publish
  *two* fantasy seasons per year (the league phase and the knockout stage) that
  share a single stat season id, so importing them would violate it. The fantasy
  season id stays globally unique and is what identifies a season anyway; the
  stat id keeps a plain index for lookups.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0007'
down_revision: Union[str, None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _unique_constraint_on(table: str, column: str) -> str | None:
    """Name the single-column UNIQUE constraint on ``table.column``, if any.

    The constraint was created unnamed in revision 0001, so its name comes from
    PostgreSQL's default rules. Looking it up keeps the migration working on a
    database whose name differs.
    """
    return op.get_bind().execute(
        sa.text(
            """
            SELECT con.conname
            FROM pg_constraint con
            JOIN pg_class rel ON rel.oid = con.conrelid
            JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
            WHERE con.contype = 'u'
              AND rel.relname = :table
              AND nsp.nspname = current_schema()
              AND con.conkey = ARRAY[(
                  SELECT att.attnum
                  FROM pg_attribute att
                  WHERE att.attrelid = rel.oid AND att.attname = :column
              )]::smallint[]
            """
        ),
        {"table": table, "column": column},
    ).scalar()


def upgrade() -> None:
    op.add_column(
        'competitions',
        sa.Column(
            'sort_order', sa.Integer(), nullable=False, server_default=sa.text('0')
        ),
    )
    op.add_column(
        'competitions',
        sa.Column(
            'available_seasons',
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        'competitions',
        sa.Column(
            'catalogue_synced_at', sa.DateTime(timezone=True), nullable=True
        ),
    )

    constraint = _unique_constraint_on('seasons', 'stat_season_id')
    if constraint is not None:
        op.drop_constraint(constraint, 'seasons', type_='unique')
    op.create_index('seasons_stat_season_idx', 'seasons', ['stat_season_id'])
    op.create_index('seasons_competition_idx', 'seasons', ['competition_id'])


def downgrade() -> None:
    op.drop_index('seasons_competition_idx', table_name='seasons')
    op.drop_index('seasons_stat_season_idx', table_name='seasons')
    # Only possible when no competition with per-phase seasons was imported.
    op.create_unique_constraint(
        'seasons_stat_season_id_key', 'seasons', ['stat_season_id']
    )

    op.drop_column('competitions', 'catalogue_synced_at')
    op.drop_column('competitions', 'available_seasons')
    op.drop_column('competitions', 'sort_order')
