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
* materialises the forecast for the next unplayed tour once the snapshot is
  published, so the freshly imported players arrive with a projection instead of
  an empty «Прогноз» column;
* mirrors the pipeline's progress messages onto the job row as a coarse stage
  (see :mod:`fantasy_analytics.ingestion_progress`) so the admin refresh UI
  (step 17) can show *what* is happening while it polls;
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
from .forecast_service import ensure_next_tour_forecasts
from .ingestion import IngestionOptions, ProgressCallback, run_ingestion
from .ingestion_progress import (
    STAGE_FINISHED,
    ProgressTracker,
    percent_for,
)
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


def _store_progress(
    session_factory: sessionmaker,
    job_id: int,
    *,
    stage: str,
    percent: int,
    message: str | None = None,
) -> None:
    """Write one progress update, tolerating a failure.

    Progress is advisory: it exists so the admin UI can render a stage while the
    import runs. A failed update must never abort the import, which reports its
    own errors through the job status.
    """
    try:
        with session_scope(session_factory) as session:
            repo = IngestionJobRepository(session)
            job = repo.get(job_id)
            if job is not None:
                repo.record_progress(
                    job, stage=stage, percent=percent, message=message
                )
    except Exception:  # noqa: BLE001 - a lost progress note is not an error
        return


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
    forecast_rows: int = 0,
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
        "forecast_rows": forecast_rows,
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
    build_forecasts_fn: Callable[..., int] = ensure_next_tour_forecasts,
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

    # Every progress message is forwarded to the caller and mirrored onto the
    # job row as a coarse, monotonic stage the admin UI can poll.
    tracker = ProgressTracker()

    def report(message: str) -> None:
        on_progress(message)
        stage, percent, note = tracker.observe(message)
        _store_progress(
            session_factory, job_id, stage=stage, percent=percent, message=note
        )

    def enter_stage(stage: str, message: str) -> None:
        on_progress(message)
        resolved, percent = tracker.advance_to(stage)
        tracker.message = message
        _store_progress(
            session_factory,
            job_id,
            stage=resolved,
            percent=percent,
            message=message,
        )

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
            report(f"Job {job_id} started")

            ingestion_report = run_ingestion_fn(
                client, session_factory, options, report
            )
            run_id = ingestion_report["run_id"]
            with session_scope(session_factory) as session:
                repo = IngestionJobRepository(session)
                repo.link_run(repo.get(job_id), run_id)

            enter_stage("quality_gate", f"Running quality checks for run {run_id}")
            quality_report = run_quality_fn(session_factory, run_id=run_id)

            # Only a published snapshot is worth forecasting: a blocked one is
            # never read, and its projections would point at a run nobody sees.
            forecast_rows = 0
            if quality_report.get("is_active"):
                enter_stage(
                    "forecast", f"Forecasting the next tour for run {run_id}"
                )
                forecast_rows = build_forecasts_fn(
                    session_factory, run_id=run_id, on_progress=report
                )

            result = _build_result(
                session_factory,
                ingestion_report=ingestion_report,
                quality_report=quality_report,
                forecast_rows=forecast_rows,
            )
            with session_scope(session_factory) as session:
                repo = IngestionJobRepository(session)
                repo.mark_succeeded(repo.get(job_id), result)
            enter_stage(
                STAGE_FINISHED,
                f"Job {job_id} succeeded "
                f"(snapshot_active={result['snapshot_active']})",
            )
            return "succeeded"
        except Exception as error:  # noqa: BLE001 - record and report cleanly
            message = safe_error_message(error)
            on_progress(f"Job {job_id} failed: {message}")
            # Keep the stage the job died in; only the message changes, so the
            # UI can tell the user *where* the refresh broke.
            _store_progress(
                session_factory,
                job_id,
                stage=tracker.stage,
                percent=percent_for(tracker.stage),
                message=message,
            )
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
