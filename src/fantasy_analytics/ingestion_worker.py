"""Worker process for manual ingestion jobs (development-plan step 5).

The refresh endpoint only enqueues a row in ``ingestion_jobs`` and returns
immediately; the heavy work happens here, in a separate process launched by the
API (``python -m fantasy_analytics.ingestion_worker <job_id>``). Running the
import out of process keeps the request thread free, lets the job outlive an API
restart and isolates a crash in the collector from the web server.

The worker:

* takes a **session-level advisory lock** keyed by the tournament so two workers
  never import the same tournament at once (belt-and-braces on top of the
  ``ingestion_jobs`` partial unique index);
* marks the job ``running``, executes the full import and then the quality gate,
  which publishes the snapshot only when no blocking issue is found;
* records a combined result (import counts, quality verdict and the resulting
  data-freshness timestamp) or a **safe** error message, and always releases the
  lock.

:func:`execute_job` is the importable core so tests can drive it with a fake
GraphQL client instead of spawning a subprocess.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from typing import Any, Callable, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from .client import ClientConfig, DEFAULT_ENDPOINT, SportsGraphQLClient
from .db import (
    IngestionJobRepository,
    advisory_lock_key,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from .db.models import IngestionRun
from .ingestion import IngestionOptions, ProgressCallback, run_ingestion
from .quality import run_quality_checks

# Redact any embedded connection credentials before persisting an error so the
# diagnostic message stored on the job never leaks secrets.
_CREDENTIALS = re.compile(r"(postgresql(?:\+\w+)?://)[^@\s/]*@", re.IGNORECASE)
_MAX_ERROR_LENGTH = 500


def _noop(_message: str) -> None:
    return None


def safe_error_message(error: BaseException) -> str:
    """Return a bounded, credential-free description of an exception."""
    message = f"{type(error).__name__}: {error}".strip()
    message = _CREDENTIALS.sub(r"\1***@", message)
    if len(message) > _MAX_ERROR_LENGTH:
        message = message[: _MAX_ERROR_LENGTH - 1] + "…"
    return message


def _build_options(job: Any) -> IngestionOptions:
    return IngestionOptions(
        tournament_slug=job.tournament_slug,
        season_id=job.requested_season_id,
        season_name=job.requested_season_name,
        use_current_season=bool(job.use_current_season),
    )


def _build_result(
    session_factory: sessionmaker,
    *,
    ingestion_report: dict[str, Any],
    quality_report: dict[str, Any],
) -> dict[str, Any]:
    """Combine the import and quality reports and derive data freshness.

    Data freshness is the ingestion run's ``finished_at`` when the snapshot was
    published (passed the quality gate); a blocked snapshot leaves it ``None``
    so consumers keep trusting the previous active snapshot.
    """
    run_id = ingestion_report.get("run_id")
    published = bool(quality_report.get("is_active"))
    data_freshness: str | None = None
    if published and run_id is not None:
        with session_scope(session_factory) as session:
            run = session.get(IngestionRun, run_id)
            if run is not None and run.finished_at is not None:
                data_freshness = run.finished_at.isoformat()
    return {
        "ingestion": ingestion_report,
        "quality": quality_report,
        "snapshot_active": published,
        "data_freshness": data_freshness,
        "completed_at": datetime.now(UTC).isoformat(),
    }


def execute_job(
    engine: Engine,
    session_factory: sessionmaker,
    job_id: int,
    *,
    client: Any,
    run_ingestion_fn: Callable[..., dict[str, Any]] = run_ingestion,
    run_quality_fn: Callable[..., dict[str, Any]] = run_quality_checks,
    on_progress: ProgressCallback = _noop,
) -> str:
    """Run one ingestion job end to end and return its terminal status.

    Returns ``"succeeded"``, ``"failed"`` or ``"skipped"`` (when the job is not
    ``pending``). The function never raises for an import failure: the job is
    marked ``failed`` with a safe message and the status is returned so the CLI
    can set an exit code.
    """
    with session_scope(session_factory) as session:
        job = IngestionJobRepository(session).get(job_id)
        if job is None:
            raise ValueError(f"Ingestion job {job_id} does not exist")
        if job.status != "pending":
            on_progress(
                f"Job {job_id} is {job.status!r}, not pending; skipping"
            )
            return "skipped"
        tournament_slug = job.tournament_slug
        options = _build_options(job)

    lock_key = advisory_lock_key(tournament_slug)
    lock_connection = engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    )
    try:
        acquired = lock_connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}
        ).scalar_one()
        if not acquired:
            with session_scope(session_factory) as session:
                repo = IngestionJobRepository(session)
                repo.mark_failed(
                    repo.get(job_id),
                    "Another refresh for this tournament is already running",
                )
            on_progress(f"Job {job_id} could not acquire the tournament lock")
            return "failed"

        try:
            with session_scope(session_factory) as session:
                repo = IngestionJobRepository(session)
                repo.mark_running(repo.get(job_id))
            on_progress(f"Job {job_id} started")

            ingestion_report = run_ingestion_fn(
                client, session_factory, options, on_progress
            )
            run_id = ingestion_report["run_id"]
            with session_scope(session_factory) as session:
                repo = IngestionJobRepository(session)
                repo.link_run(repo.get(job_id), run_id)

            quality_report = run_quality_fn(session_factory, run_id=run_id)
            result = _build_result(
                session_factory,
                ingestion_report=ingestion_report,
                quality_report=quality_report,
            )
            with session_scope(session_factory) as session:
                repo = IngestionJobRepository(session)
                repo.mark_succeeded(repo.get(job_id), result)
            on_progress(
                f"Job {job_id} succeeded "
                f"(snapshot_active={result['snapshot_active']})"
            )
            return "succeeded"
        except Exception as error:  # noqa: BLE001 - record and report cleanly
            message = safe_error_message(error)
            on_progress(f"Job {job_id} failed: {message}")
            with session_scope(session_factory) as session:
                repo = IngestionJobRepository(session)
                repo.mark_failed(repo.get(job_id), message)
            return "failed"
    finally:
        try:
            lock_connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
            )
        finally:
            lock_connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-ingestion-worker",
        description="Execute a queued manual ingestion job by id.",
    )
    parser.add_argument("job_id", type=int, help="ingestion_jobs.id to execute")
    parser.add_argument(
        "--database-url", help="Override DATABASE_URL for this worker"
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"GraphQL endpoint (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress output on stderr",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)
    client = SportsGraphQLClient(
        ClientConfig(endpoint=args.endpoint, timeout_seconds=args.timeout)
    )

    def on_progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    try:
        status = execute_job(
            engine,
            session_factory,
            args.job_id,
            client=client,
            on_progress=on_progress,
        )
    finally:
        engine.dispose()

    return 0 if status in ("succeeded", "skipped") else 1


if __name__ == "__main__":
    raise SystemExit(main())
