"""FastAPI control plane and user REST API (development-plan steps 5 and 9).

The application serves two concerns behind one app:

* **Admin ingestion (step 5).** ``POST /admin/ingestion/rpl/refresh`` enqueues a
  refresh job and returns immediately; the import runs in a separate worker
  process (:mod:`fantasy_analytics.ingestion_worker`). ``GET
  /admin/ingestion/runs/{job_id}`` reports a job's status.
* **User read API (step 9).** Read endpoints for seasons, tours, matches and
  players (with filters, projections and explaining components) plus the
  ``POST /optimizer/squad`` and ``POST /optimizer/transfers`` endpoints. Every
  read endpoint is served exclusively from PostgreSQL — the Sports.ru GraphQL
  API is never called from a read path. Numbers that vary per snapshot come from
  the single *active* snapshot published by the quality gate; projections come
  from the persisted ``player_forecasts`` rows.

All errors share one envelope: ``{"error": {"type", "message", "details"}}``.
List endpoints are paginated with a bounded ``limit`` and expose the snapshot
time and (for projections) the model version.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api_schemas import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    ForecastModel,
    MatchListResponse,
    MatchModel,
    OptimizerResponse,
    PlayerDetailModel,
    PlayerListResponse,
    PlayerOrder,
    Role,
    SeasonDetailModel,
    SeasonListResponse,
    SquadRequest,
    TourListResponse,
    TourModel,
    TransfersRequest,
)
from .client import DEFAULT_ENDPOINT
from .db import (
    IngestionJobRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from .db.models import IngestionJob
from .optimizer import OptimizerError, build_squad_optimization
from .read_repository import ReadRepository

# The refresh endpoint is tournament-scoped by path; RPL maps to this slug.
RPL_TOURNAMENT_SLUG = "russia"

SpawnWorker = Callable[[int], None]

_STATUS_ERROR_TYPES = {
    400: "bad_request",
    404: "not_found",
    409: "conflict",
    422: "unprocessable_entity",
}


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


def _error_detail(
    type_: str, message: str, details: Any | None = None
) -> dict[str, Any]:
    return {"type": type_, "message": message, "details": details}


def api_error(
    status_code: int, message: str, *, type_: str | None = None, details: Any = None
) -> HTTPException:
    """Build an :class:`HTTPException` carrying the unified error envelope."""
    resolved = type_ or _STATUS_ERROR_TYPES.get(status_code, "error")
    return HTTPException(
        status_code=status_code,
        detail=_error_detail(resolved, message, details),
    )


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


def _page_meta(*, limit: int, offset: int, total: int, count: int) -> dict[str, int]:
    return {"limit": limit, "offset": offset, "total": total, "count": count}


def create_app(
    *,
    session_factory: sessionmaker | None = None,
    spawn_worker: SpawnWorker | None = None,
    database_url: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
) -> FastAPI:
    """Build the combined admin + user API.

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
        title="Fantasy Analytics API",
        version="0.2.0",
        description=(
            "Read API and squad optimizer for the Fantasy RPL pipeline, plus the "
            "manual ingestion control plane. Read endpoints never call Sports.ru."
        ),
    )
    app.state.session_factory = session_factory
    app.state.spawn_worker = spawn_worker

    # ------------------------------------------------------------------
    # Unified error envelope.
    # ------------------------------------------------------------------
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "type" in detail and "message" in detail:
            payload = detail
        else:
            payload = _error_detail(
                _STATUS_ERROR_TYPES.get(exc.status_code, "error"), str(detail)
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": jsonable_encoder(payload)},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": _error_detail(
                    "validation_error",
                    "Request validation failed",
                    jsonable_encoder(exc.errors()),
                )
            },
        )

    # ------------------------------------------------------------------
    # Health.
    # ------------------------------------------------------------------
    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # Read helpers.
    # ------------------------------------------------------------------
    def _require_season(repo: ReadRepository, season_id: int) -> dict[str, Any]:
        season = repo.get_season(season_id)
        if season is None:
            raise api_error(404, f"Season {season_id} not found")
        return season

    def _resolve_run_id(repo: ReadRepository, season_id: int) -> int | None:
        run = repo.resolve_active_run(season_id)
        return run.id if run is not None else None

    # ------------------------------------------------------------------
    # Seasons.
    # ------------------------------------------------------------------
    @app.get("/seasons", response_model=SeasonListResponse, tags=["catalog"])
    def list_seasons(
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            seasons = ReadRepository(session).list_seasons()
        total = len(seasons)
        page = seasons[offset : offset + limit]
        return {
            "items": page,
            "pagination": _page_meta(
                limit=limit, offset=offset, total=total, count=len(page)
            ),
        }

    @app.get(
        "/seasons/{season_id}", response_model=SeasonDetailModel, tags=["catalog"]
    )
    def get_season(season_id: int) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            season = _require_season(ReadRepository(session), season_id)
        return season

    # ------------------------------------------------------------------
    # Tours.
    # ------------------------------------------------------------------
    @app.get("/tours", response_model=TourListResponse, tags=["catalog"])
    def list_tours(
        season_id: int | None = Query(default=None),
        status: str | None = Query(default=None),
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            total, items = ReadRepository(session).list_tours(
                season_id=season_id, status=status, limit=limit, offset=offset
            )
        return {
            "items": items,
            "pagination": _page_meta(
                limit=limit, offset=offset, total=total, count=len(items)
            ),
        }

    @app.get("/tours/{tour_id}", response_model=TourModel, tags=["catalog"])
    def get_tour(tour_id: int) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            tour = ReadRepository(session).get_tour(tour_id)
        if tour is None:
            raise api_error(404, f"Tour {tour_id} not found")
        return tour

    # ------------------------------------------------------------------
    # Matches.
    # ------------------------------------------------------------------
    @app.get("/matches", response_model=MatchListResponse, tags=["catalog"])
    def list_matches(
        season_id: int | None = Query(default=None),
        tour_id: int | None = Query(default=None),
        club_id: int | None = Query(default=None),
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            total, items = ReadRepository(session).list_matches(
                season_id=season_id,
                tour_id=tour_id,
                club_id=club_id,
                limit=limit,
                offset=offset,
            )
        return {
            "items": items,
            "pagination": _page_meta(
                limit=limit, offset=offset, total=total, count=len(items)
            ),
        }

    @app.get("/matches/{match_id}", response_model=MatchModel, tags=["catalog"])
    def get_match(match_id: int) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            match = ReadRepository(session).get_match(match_id)
        if match is None:
            raise api_error(404, f"Match {match_id} not found")
        return match

    # ------------------------------------------------------------------
    # Players and projections.
    # ------------------------------------------------------------------
    @app.get("/players", response_model=PlayerListResponse, tags=["players"])
    def list_players(
        season_id: int = Query(..., description="Internal season id"),
        tour_id: int | None = Query(
            default=None, description="Join projections for this tour"
        ),
        model: ForecastModel = Query(default="poisson_events"),
        role: Role | None = Query(default=None, description="Position filter"),
        club_id: int | None = Query(default=None),
        status: str | None = Query(
            default=None, description="Availability status filter"
        ),
        min_price: float | None = Query(default=None, ge=0),
        max_price: float | None = Query(default=None, ge=0),
        order: PlayerOrder = Query(default="name"),
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            repo = ReadRepository(session)
            _require_season(repo, season_id)
            active_run = repo.resolve_active_run(season_id)
            run_id = active_run.id if active_run is not None else None
            total, items = repo.list_players(
                season_id=season_id,
                run_id=run_id,
                tour_id=tour_id,
                model=model,
                role=role,
                club_id=club_id,
                status=status,
                min_price=min_price,
                max_price=max_price,
                order=order,
                limit=limit,
                offset=offset,
            )
            snapshot = repo.snapshot_meta(active_run)
        return {
            "items": items,
            "pagination": _page_meta(
                limit=limit, offset=offset, total=total, count=len(items)
            ),
            "snapshot": snapshot,
        }

    @app.get(
        "/players/{player_season_id}",
        response_model=PlayerDetailModel,
        tags=["players"],
    )
    def get_player(
        player_season_id: int,
        tour_id: int | None = Query(default=None),
        model: ForecastModel = Query(default="poisson_events"),
    ) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            repo = ReadRepository(session)
            # Peek at the player to learn its season before resolving the run.
            probe = repo.get_player(
                player_season_id=player_season_id, run_id=None
            )
            if probe is None:
                raise api_error(404, f"Player {player_season_id} not found")
            active_run = repo.resolve_active_run(probe["season_id"])
            run_id = active_run.id if active_run is not None else None
            player = repo.get_player(
                player_season_id=player_season_id,
                run_id=run_id,
                tour_id=tour_id,
                model=model,
            )
            player["snapshot"] = repo.snapshot_meta(active_run)
        return player

    # ------------------------------------------------------------------
    # Optimizer.
    # ------------------------------------------------------------------
    def _run_optimizer(
        *,
        run_id: int | None,
        season: str | None,
        tour: str | None,
        model: str,
        current_squad: list[str] | None,
        max_transfers: int | None,
    ) -> dict[str, Any]:
        try:
            return build_squad_optimization(
                app.state.session_factory,
                run_id=run_id,
                season_ref=season,
                tour_ref=tour,
                model=model,
                current_squad=current_squad,
                max_transfers=max_transfers,
            )
        except OptimizerError as error:
            raise api_error(422, str(error), type_="optimizer_error") from error

    @app.post(
        "/optimizer/squad", response_model=OptimizerResponse, tags=["optimizer"]
    )
    def optimize_squad(request: SquadRequest | None = None) -> dict[str, Any]:
        body = request or SquadRequest()
        return _run_optimizer(
            run_id=body.run_id,
            season=body.season,
            tour=body.tour,
            model=body.model,
            current_squad=None,
            max_transfers=None,
        )

    @app.post(
        "/optimizer/transfers",
        response_model=OptimizerResponse,
        tags=["optimizer"],
    )
    def optimize_transfers(request: TransfersRequest) -> dict[str, Any]:
        return _run_optimizer(
            run_id=request.run_id,
            season=request.season,
            tour=request.tour,
            model=request.model,
            current_squad=request.current_squad,
            max_transfers=request.max_transfers,
        )

    # ------------------------------------------------------------------
    # Admin ingestion (step 5).
    # ------------------------------------------------------------------
    @app.post("/admin/ingestion/rpl/refresh", status_code=202, tags=["admin"])
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
                    "error": _error_detail(
                        "conflict",
                        "A refresh for this tournament is already in progress",
                        {"job": jsonable_encoder(payload)},
                    )
                },
            )

        app.state.spawn_worker(payload["id"])
        return JSONResponse(status_code=202, content=jsonable_encoder(payload))

    @app.get("/admin/ingestion/runs/{job_id}", tags=["admin"])
    def get_run(job_id: int) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            job = IngestionJobRepository(session).get(job_id)
            if job is None:
                raise api_error(404, f"Ingestion job {job_id} not found")
            return _job_to_dict(job)

    return app


def main(argv: list[str] | None = None) -> int:
    """Run the API with uvicorn (``fantasy-api`` console script)."""
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="fantasy-api",
        description="Serve the Fantasy Analytics API.",
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
