"""Repository layer for manual ingestion jobs (development-plan step 5).

An ingestion job is the admin-facing unit of work behind the refresh endpoint.
The repository owns three concerns, all inside the caller's transaction:

* enqueuing a job while guaranteeing at most one *active* job per tournament
  (the ``ingestion_jobs_active_tournament_idx`` partial unique index);
* transitioning its lifecycle (``pending`` → ``running`` → ``succeeded``/
  ``failed``), recording its coarse progress and linking it to the
  :class:`IngestionRun` it produced;
* reading a job back for the status endpoint (by id, or the latest / latest
  successful one for a tournament, which is what the admin UI needs after a page
  reload).

The repository never commits on its own; the caller owns the transaction
boundary (typically :func:`fantasy_analytics.db.session_scope`).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import IngestionJob

JOB_STATUSES = ("pending", "running", "succeeded", "failed")
ACTIVE_STATUSES = ("pending", "running")
_TERMINAL_STATUSES = ("succeeded", "failed")


def advisory_lock_key(tournament_slug: str) -> int:
    """Return a stable signed 64-bit PostgreSQL advisory-lock key for a slug.

    ``pg_advisory_lock`` takes a ``bigint``; hashing the tournament slug into a
    signed 64-bit integer keeps the key deterministic across processes so two
    workers refreshing the same tournament always contend for the same lock.
    """
    digest = hashlib.sha256(f"fantasy-ingestion:{tournament_slug}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class IngestionJobRepository:
    """Transactional access to manual ingestion jobs."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def active_job(self, tournament_slug: str) -> IngestionJob | None:
        """Return the pending/running job for a tournament, if any."""
        return self._session.execute(
            select(IngestionJob)
            .where(
                IngestionJob.tournament_slug == tournament_slug,
                IngestionJob.status.in_(ACTIVE_STATUSES),
            )
            .order_by(IngestionJob.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def enqueue(
        self,
        *,
        tournament_slug: str,
        requested_season_id: str | None = None,
        requested_season_name: str | None = None,
        use_current_season: bool = False,
        trigger_type: str = "manual",
    ) -> tuple[IngestionJob, bool]:
        """Insert a pending job unless one is already active.

        Returns ``(job, created)``. When an active job already exists for the
        tournament it is returned with ``created=False`` and no row is inserted,
        so the caller can answer with a conflict. The partial unique index makes
        this safe under a race: a concurrent insert raises ``IntegrityError`` and
        we fall back to returning the winner.
        """
        existing = self.active_job(tournament_slug)
        if existing is not None:
            return existing, False

        job = IngestionJob(
            tournament_slug=tournament_slug,
            requested_season_id=requested_season_id,
            requested_season_name=requested_season_name,
            use_current_season=use_current_season,
            trigger_type=trigger_type,
            status="pending",
        )
        self._session.add(job)
        try:
            self._session.flush()
        except IntegrityError:
            self._session.rollback()
            winner = self.active_job(tournament_slug)
            if winner is not None:
                return winner, False
            raise
        return job, True

    def get(self, job_id: int) -> IngestionJob | None:
        return self._session.get(IngestionJob, job_id)

    def latest_job(self, tournament_slug: str) -> IngestionJob | None:
        """Return the most recently created job for a tournament, if any."""
        return self._session.execute(
            select(IngestionJob)
            .where(IngestionJob.tournament_slug == tournament_slug)
            .order_by(IngestionJob.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def latest_successful_job(self, tournament_slug: str) -> IngestionJob | None:
        """Return the most recent job that finished successfully, if any."""
        return self._session.execute(
            select(IngestionJob)
            .where(
                IngestionJob.tournament_slug == tournament_slug,
                IngestionJob.status == "succeeded",
            )
            .order_by(IngestionJob.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def record_progress(
        self,
        job: IngestionJob,
        *,
        stage: str,
        percent: int,
        message: str | None = None,
    ) -> IngestionJob:
        """Store the job's coarse progress for the admin UI to poll.

        The stage vocabulary and the monotonicity of the sequence are owned by
        :mod:`fantasy_analytics.ingestion_progress`; the repository only writes
        what it is given.
        """
        job.progress_stage = stage
        job.progress_percent = percent
        if message is not None:
            job.progress_message = message
        job.progress_updated_at = datetime.now(UTC)
        self._session.flush()
        return job

    def _set_status(
        self,
        job: IngestionJob,
        status: str,
        *,
        error_message: str | None = None,
        result: Any | None = None,
    ) -> IngestionJob:
        if status not in JOB_STATUSES:
            raise ValueError(f"Unknown ingestion job status: {status!r}")
        job.status = status
        if error_message is not None:
            job.error_message = error_message
        if result is not None:
            job.result = result
        if status == "running" and job.started_at is None:
            job.started_at = datetime.now(UTC)
        if status in _TERMINAL_STATUSES and job.finished_at is None:
            job.finished_at = datetime.now(UTC)
        self._session.flush()
        return job

    def mark_running(self, job: IngestionJob) -> IngestionJob:
        return self._set_status(job, "running")

    def mark_succeeded(self, job: IngestionJob, result: Any | None = None) -> IngestionJob:
        return self._set_status(job, "succeeded", result=result)

    def mark_failed(self, job: IngestionJob, error_message: str) -> IngestionJob:
        return self._set_status(job, "failed", error_message=error_message)

    def link_run(self, job: IngestionJob, run_id: int) -> IngestionJob:
        """Attach the ingestion run the worker created for this job."""
        job.ingestion_run_id = run_id
        self._session.flush()
        return job


__all__ = [
    "ACTIVE_STATUSES",
    "IngestionJobRepository",
    "JOB_STATUSES",
    "advisory_lock_key",
]
