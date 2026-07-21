"""Repository layer for ingestion runs and captured raw API responses."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from .models import IngestionRun, RawApiResponse

RUN_STATUSES = ("pending", "running", "succeeded", "failed")
_TERMINAL_STATUSES = ("succeeded", "failed")


def compute_response_hash(payload: Any) -> str:
    """Return a stable SHA-256 hash for a GraphQL response payload."""
    serialized = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class IngestionRepository:
    """Transactional access to ingestion runs and their raw responses.

    The repository never commits on its own; the caller owns the transaction
    boundary (for example through :func:`fantasy_analytics.db.session_scope`).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def create_run(
        self,
        *,
        tournament_slug: str,
        requested_season_id: str | None = None,
        trigger_type: str = "manual",
        status: str = "pending",
    ) -> IngestionRun:
        """Insert a new ingestion run and return it with its generated id."""
        if status not in RUN_STATUSES:
            raise ValueError(f"Unknown ingestion status: {status!r}")
        run = IngestionRun(
            tournament_slug=tournament_slug,
            requested_season_id=requested_season_id,
            trigger_type=trigger_type,
            status=status,
        )
        self._session.add(run)
        self._session.flush()
        return run

    def get_run(self, run_id: int) -> IngestionRun | None:
        return self._session.get(IngestionRun, run_id)

    def update_status(
        self,
        run: IngestionRun,
        status: str,
        *,
        error_message: str | None = None,
        report: Any | None = None,
    ) -> IngestionRun:
        """Update a run status; terminal statuses set ``finished_at``."""
        if status not in RUN_STATUSES:
            raise ValueError(f"Unknown ingestion status: {status!r}")
        run.status = status
        if error_message is not None:
            run.error_message = error_message
        if report is not None:
            run.report = report
        if status in _TERMINAL_STATUSES and run.finished_at is None:
            run.finished_at = datetime.now(UTC)
        self._session.flush()
        return run

    def mark_running(self, run: IngestionRun) -> IngestionRun:
        return self.update_status(run, "running")

    def mark_succeeded(
        self, run: IngestionRun, report: Any | None = None
    ) -> IngestionRun:
        return self.update_status(run, "succeeded", report=report)

    def mark_failed(self, run: IngestionRun, error_message: str) -> IngestionRun:
        return self.update_status(run, "failed", error_message=error_message)

    def save_raw_response(
        self,
        run: IngestionRun,
        *,
        operation_name: str,
        variables: Any,
        response: Any,
    ) -> RawApiResponse:
        """Persist a raw response idempotently.

        Re-saving an identical payload for the same run and operation returns
        the existing row instead of creating a duplicate, relying on the unique
        constraint over ``(ingestion_run_id, operation_name, response_hash)``.
        """
        response_hash = compute_response_hash(response)
        statement = (
            pg_insert(RawApiResponse)
            .values(
                ingestion_run_id=run.id,
                operation_name=operation_name,
                variables=variables,
                response=response,
                response_hash=response_hash,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "ingestion_run_id",
                    "operation_name",
                    "response_hash",
                ]
            )
            .returning(RawApiResponse.id)
        )
        inserted_id = self._session.execute(statement).scalar_one_or_none()
        self._session.flush()
        if inserted_id is not None:
            stored = self._session.get(RawApiResponse, inserted_id)
            assert stored is not None
            return stored
        return self._session.execute(
            select(RawApiResponse).where(
                RawApiResponse.ingestion_run_id == run.id,
                RawApiResponse.operation_name == operation_name,
                RawApiResponse.response_hash == response_hash,
            )
        ).scalar_one()

    def count_raw_responses(self, run: IngestionRun) -> int:
        return int(
            self._session.execute(
                select(func.count())
                .select_from(RawApiResponse)
                .where(RawApiResponse.ingestion_run_id == run.id)
            ).scalar_one()
        )
