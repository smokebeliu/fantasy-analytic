"""FastAPI admin API for manual ingestion (development-plan step 5).

The API is intentionally thin: it enqueues a refresh job and reports job status.
The actual import runs in a separate worker process
(:mod:`fantasy_analytics.ingestion_worker`) so the request returns immediately.

Endpoints
---------
* ``POST /admin/ingestion/rpl/refresh`` — enqueue an RPL refresh. Returns ``202``
  with the new job id, or ``409`` when a refresh for the tournament is already
  active (at most one concurrent refresh per tournament).
* ``GET /admin/ingestion/runs/{job_id}`` — read a refresh job's status. ``{job_id}``
  is the id returned by the refresh endpoint; the resource is called "runs"
  because a job *is* an admin-level refresh run. Statuses are read from
  PostgreSQL, so they survive an API restart.
* ``GET /health`` — liveness probe.

The job table plus a worker-held advisory lock replace Redis for both
persistence and mutual exclusion.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import sessionmaker

from .client import DEFAULT_ENDPOINT
from .db import (
    IngestionJobRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from .db.models import IngestionJob

# The refresh endpoint is tournament-scoped by path; RPL maps to this slug.
RPL_TOURNAMENT_SLUG = "russia"

SpawnWorker = Callable[[int], None]


class RefreshRequest(BaseModel):
    """Optional season selector for a refresh; defaults to latest completed."""

    season_id: str | None = Field(default=None, description="Exact fantasy season id")
    season_name: str | None = Field(
        default=None, description="Exact stat season name, e.g. 2025/2026"
    )
    current: bool = Field(
        default=False, description="Refresh the active season instead of the latest"
    )

    @model_validator(mode="after")
    def _only_one_selector(self) -> "RefreshRequest":
        selectors = [
            bool(self.season_id),
            bool(self.season_name),
            bool(self.current),
        ]
        if sum(selectors) > 1:
            raise ValueError(
                "Provide at most one of season_id, season_name or current"
            )
        return self


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _job_to_dict(job: IngestionJob) -> dict[str, Any]:
    result = job.result or None
    return {
        "id": job.id,
        "status": job.status,
        "tournament_slug": job.tournament_slug,
        "trigger_type": job.trigger_type,
        "requested_season_id": job.requested_season_id,
        "requested_season_name": job.requested_season_name,
        "use_current_season": job.use_current_season,
        "ingestion_run_id": job.ingestion_run_id,
        "error_message": job.error_message,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "data_freshness": (result or {}).get("data_freshness"),
        "result": result,
    }


def default_spawn_worker(
    job_id: int,
    *,
    database_url: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
) -> None:
    """Launch the ingestion worker as a detached background process.

    ``start_new_session=True`` detaches the child from the API process group so
    a queued import keeps running even if the API is restarted or stopped.
    """
    args = [sys.executable, "-m", "fantasy_analytics.ingestion_worker", str(job_id)]
    if database_url:
        args += ["--database-url", database_url]
    if endpoint:
        args += ["--endpoint", endpoint]
    subprocess.Popen(args, start_new_session=True)


def create_app(
    *,
    session_factory: sessionmaker | None = None,
    spawn_worker: SpawnWorker | None = None,
    database_url: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
) -> FastAPI:
    """Build the admin API.

    ``session_factory`` and ``spawn_worker`` are injectable so tests can use a
    transactional session and a synchronous/fake worker instead of a subprocess.
    """
    if session_factory is None:
        engine = create_db_engine(database_url)
        session_factory = create_session_factory(engine)
    if spawn_worker is None:
        def spawn_worker(job_id: int) -> None:
            default_spawn_worker(
                job_id, database_url=database_url, endpoint=endpoint
            )

    app = FastAPI(
        title="Fantasy Analytics Admin API",
        version="0.1.0",
        description="Manual ingestion control plane for the Fantasy RPL pipeline.",
    )
    app.state.session_factory = session_factory
    app.state.spawn_worker = spawn_worker

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/admin/ingestion/rpl/refresh", status_code=202)
    def refresh_rpl(request: RefreshRequest | None = None) -> JSONResponse:
        body = request or RefreshRequest()
        with session_scope(app.state.session_factory) as session:
            repo = IngestionJobRepository(session)
            job, created = repo.enqueue(
                tournament_slug=RPL_TOURNAMENT_SLUG,
                requested_season_id=body.season_id,
                requested_season_name=body.season_name,
                use_current_season=body.current,
            )
            payload = _job_to_dict(job)

        if not created:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "A refresh for this tournament is already in progress",
                    "job": payload,
                },
            )

        app.state.spawn_worker(payload["id"])
        return JSONResponse(status_code=202, content=payload)

    @app.get("/admin/ingestion/runs/{job_id}")
    def get_run(job_id: int) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            job = IngestionJobRepository(session).get(job_id)
            if job is None:
                raise HTTPException(
                    status_code=404, detail=f"Ingestion job {job_id} not found"
                )
            return _job_to_dict(job)

    return app


def main(argv: list[str] | None = None) -> int:
    """Run the admin API with uvicorn (``fantasy-api`` console script)."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="fantasy-api",
        description="Serve the Fantasy Analytics admin API.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8000, help="Bind port")
    parser.add_argument("--database-url", help="Override DATABASE_URL")
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"GraphQL endpoint passed to workers (default: {DEFAULT_ENDPOINT})",
    )
    args = parser.parse_args(argv)

    app = create_app(database_url=args.database_url, endpoint=args.endpoint)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
