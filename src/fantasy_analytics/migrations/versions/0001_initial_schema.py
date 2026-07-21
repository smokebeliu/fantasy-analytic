"""Initial schema materialized from the SQLAlchemy models.

This baseline revision creates every table defined by the ORM metadata, which
is the single authoritative description of the schema. Subsequent revisions
should use explicit ``op`` operations produced by ``alembic revision
--autogenerate`` against these models.

Revision ID: 0001
Revises:
Create Date: 2026-07-20

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from fantasy_analytics.db.models import Base

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
