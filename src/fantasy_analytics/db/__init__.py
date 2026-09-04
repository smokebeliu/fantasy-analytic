"""Persistence layer: SQLAlchemy models, configuration and repositories."""

from __future__ import annotations

from .config import (
    DEFAULT_DATABASE_URL,
    create_db_engine,
    create_session_factory,
    get_database_url,
    session_scope,
)
from .forecast_repository import ForecastRepository
from .import_repository import DomainImportRepository
from .job_repository import IngestionJobRepository, advisory_lock_key
from .models import (
    Base,
    DataQualityIssue,
    IngestionJob,
    IngestionRun,
    RawApiResponse,
)
from .odds_repository import OddsRepository
from .quality_repository import QualityRepository
from .repository import IngestionRepository

__all__ = [
    "Base",
    "DEFAULT_DATABASE_URL",
    "DataQualityIssue",
    "DomainImportRepository",
    "ForecastRepository",
    "IngestionJob",
    "IngestionJobRepository",
    "IngestionRepository",
    "IngestionRun",
    "OddsRepository",
    "QualityRepository",
    "RawApiResponse",
    "advisory_lock_key",
    "create_db_engine",
    "create_session_factory",
    "get_database_url",
    "session_scope",
]
