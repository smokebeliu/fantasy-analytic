"""Database configuration, engine and session helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DEFAULT_DATABASE_URL = "postgresql+psycopg://fantasy:fantasy@localhost:5432/fantasy"


def get_database_url(explicit: str | None = None) -> str:
    """Resolve the database URL from an explicit value or ``DATABASE_URL``."""
    if explicit:
        return explicit
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def create_db_engine(database_url: str | None = None, **engine_kwargs: Any) -> Engine:
    """Create a SQLAlchemy engine for the resolved database URL."""
    return create_engine(get_database_url(database_url), future=True, **engine_kwargs)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Build a session factory bound to ``engine``."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """Provide a transactional scope: commit on success, rollback on error."""
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
