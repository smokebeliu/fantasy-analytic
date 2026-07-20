"""Programmatic Alembic helpers used by the CLI and integration tests."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from .config import get_database_url

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def build_alembic_config(database_url: str | None = None) -> Config:
    """Build an Alembic ``Config`` pointing at the packaged migrations."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", get_database_url(database_url))
    return config


def upgrade(database_url: str | None = None, revision: str = "head") -> None:
    command.upgrade(build_alembic_config(database_url), revision)


def downgrade(database_url: str | None = None, revision: str = "base") -> None:
    command.downgrade(build_alembic_config(database_url), revision)


def current(database_url: str | None = None) -> None:
    command.current(build_alembic_config(database_url), verbose=True)


def stamp(database_url: str | None = None, revision: str = "head") -> None:
    command.stamp(build_alembic_config(database_url), revision)
