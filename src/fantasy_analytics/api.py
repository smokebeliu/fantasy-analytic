"""FastAPI control plane and user REST API (development-plan steps 5 and 9).

The application serves two concerns behind one app:

* **Admin ingestion (steps 5, 17 and 22).**
  ``POST /admin/ingestion/{tournament_slug}/refresh`` enqueues a refresh job for
  one league and returns immediately; the import runs in a separate worker
  process (:mod:`fantasy_analytics.ingestion_worker`). ``GET
  /admin/ingestion/runs/{job_id}`` reports a job's status, and ``GET
  /admin/ingestion/{tournament_slug}/status`` answers "is a refresh running, and
  what does the published snapshot look like" *without* a job id, so the admin
  screen recovers its state after a page reload. ``rpl`` is accepted as an alias
  for the ``russia`` slug the original single-league endpoints used, and
  ``POST /admin/competitions/sync`` refreshes the catalogue of available leagues.
* **User read API (steps 9 and 22).** Read endpoints for competitions, seasons,
  tours, matches and players (with filters, projections and explaining
  components) plus the ``POST /optimizer/squad`` and ``POST
  /optimizer/transfers`` endpoints. ``GET /competitions`` is what a league
  switcher reads: it lists every catalogued league and, for each, the season and
  snapshot the rest of the UI should use. Every
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
    CatalogueSyncResponse,
    CompetitionListResponse,
    CompetitionModel,
    ForecastModel,
    IngestionJobModel,
    IngestionStatusResponse,
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
from .client import ClientConfig, DEFAULT_ENDPOINT, SportsGraphQLClient
from .competitions import (
    CompetitionCatalogueError,
    DEFAULT_TOURNAMENT_SLUG,
    sync_catalogue,
)
from .db import (
    IngestionJobRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from .db.job_repository import ACTIVE_STATUSES
from .db.models import IngestionJob
from .forecast_service import ensure_tour_forecasts
from .ingestion_progress import STAGES
from .optimizer import OptimizerError, build_squad_optimization
from .read_repository import ReadRepository

# The legacy admin paths (``/admin/ingestion/rpl/*``) are aliases for this slug;
# every league is reachable through ``/admin/ingestion/{tournament_slug}/*``.
RPL_TOURNAMENT_SLUG = DEFAULT_TOURNAMENT_SLUG

# Refreshes the stored league catalogue from Sports.ru. Injectable so tests can
# drive the admin endpoint without network access.
SyncCatalogue = Callable[..., dict[str, Any]]

SpawnWorker = Callable[[int], None]

# Materialises the projections of one (run, tour) pair before a player read is
# answered; see :mod:`fantasy_analytics.forecast_service`.
EnsureForecasts = Callable[..., int]

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


def _job_progress(job: IngestionJob) -> dict[str, Any] | None:
    """Expose the job's coarse progress; ``None`` before the worker reports."""
    if job.progress_stage is None:
        return None
    return {
        "stage": job.progress_stage,
        "percent": int(job.progress_percent or 0),
        "message": job.progress_message,
        "updated_at": _iso(job.progress_updated_at),
    }


def _job_summary(job: IngestionJob) -> dict[str, Any]:
    """A polling-friendly job view: everything but the verbose reports.

    A finished job's ``result`` embeds the whole import and quality report, which
    is far too large to re-send on every poll. The status endpoint therefore
    keeps only the headline numbers; the full report stays available on
    ``GET /admin/ingestion/runs/{job_id}``.
    """
    payload = _job_to_dict(job)
    result = payload.pop("result") or {}
    ingestion = result.get("ingestion") or {}
    quality = result.get("quality") or {}
    payload["result"] = (
        {
            "snapshot_active": result.get("snapshot_active"),
            "data_freshness": result.get("data_freshness"),
            "completed_at": result.get("completed_at"),
            "counts": ingestion.get("counts"),
            "quality": {
                "passed": quality.get("passed"),
                "counts": quality.get("counts"),
            },
        }
        if result
        else None
    )
    return payload


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
        "progress": _job_progress(job),
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


def default_sync_catalogue(
    session_factory: sessionmaker, *, endpoint: str = DEFAULT_ENDPOINT
) -> dict[str, Any]:
    """Refresh the league catalogue from Sports.ru in-process.

    This is the one admin write that calls the GraphQL API inline rather than
    through the worker: it is a single request that returns in well under a
    second, so queueing a job for it would only add latency and a poll loop.
    """
    client = SportsGraphQLClient(ClientConfig(endpoint=endpoint))
    return sync_catalogue(client, session_factory)


def create_app(
    *,
    session_factory: sessionmaker | None = None,
    spawn_worker: SpawnWorker | None = None,
    ensure_forecasts: EnsureForecasts = ensure_tour_forecasts,
    sync_competitions: SyncCatalogue | None = None,
    database_url: str | None = None,
    endpoint: str = DEFAULT_ENDPOINT,
) -> FastAPI:
    """Build the combined admin + user API.

    ``session_factory``, ``spawn_worker``, ``ensure_forecasts`` and
    ``sync_competitions`` are injectable so tests can use a transactional
    session, a synchronous/fake worker, a no-op forecast materialiser and a
    canned catalogue instead of a subprocess, a real solver run and the network.
    """
    if session_factory is None:
        engine = create_db_engine(database_url)
        session_factory = create_session_factory(engine)
    if spawn_worker is None:
        def spawn_worker(job_id: int) -> None:
            default_spawn_worker(
                job_id, database_url=database_url, endpoint=endpoint
            )
    if sync_competitions is None:
        def sync_competitions(factory: sessionmaker) -> dict[str, Any]:
            return default_sync_catalogue(factory, endpoint=endpoint)

    app = FastAPI(
        title="Fantasy Analytics API",
        version="0.3.0",
        description=(
            "Read API and squad optimizer for the Sports.ru fantasy pipeline, "
            "plus the manual ingestion control plane. Every league in the "
            "catalogue is served by the same endpoints; read endpoints never "
            "call Sports.ru."
        ),
    )
    app.state.session_factory = session_factory
    app.state.spawn_worker = spawn_worker
    app.state.ensure_forecasts = ensure_forecasts
    app.state.sync_competitions = sync_competitions

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
    # Competitions (leagues).
    # ------------------------------------------------------------------
    @app.get(
        "/competitions", response_model=CompetitionListResponse, tags=["catalog"]
    )
    def list_competitions(
        imported_only: bool = Query(
            default=False,
            description="Only leagues that already have an imported season",
        ),
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        """List the leagues the pipeline knows about.

        This is what a league switcher reads: with ``imported_only=true`` it
        returns exactly the leagues the read API can serve, each with the season
        and snapshot the rest of the UI should use.
        """
        with session_scope(app.state.session_factory) as session:
            competitions = ReadRepository(session).list_competitions(
                imported_only=imported_only
            )
        total = len(competitions)
        page = competitions[offset : offset + limit]
        return {
            "items": page,
            "pagination": _page_meta(
                limit=limit, offset=offset, total=total, count=len(page)
            ),
        }

    @app.get(
        "/competitions/{slug}", response_model=CompetitionModel, tags=["catalog"]
    )
    def get_competition(slug: str) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            competition = ReadRepository(session).get_competition_by_slug(slug)
        if competition is None:
            raise api_error(404, f"Competition {slug!r} not found")
        return competition

    # ------------------------------------------------------------------
    # Seasons.
    # ------------------------------------------------------------------
    @app.get("/seasons", response_model=SeasonListResponse, tags=["catalog"])
    def list_seasons(
        competition_id: int | None = Query(
            default=None, description="Restrict to one league"
        ),
        limit: int = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            seasons = ReadRepository(session).list_seasons(
                competition_id=competition_id
            )
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

        # A snapshot published before this tour was forecast would answer with
        # an empty «Прогноз» column; materialise it once, then read normally.
        if tour_id is not None:
            app.state.ensure_forecasts(
                app.state.session_factory, run_id=run_id, tour_id=tour_id
            )

        with session_scope(app.state.session_factory) as session:
            repo = ReadRepository(session)
            active_run = repo.resolve_active_run(season_id)
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

        if tour_id is not None:
            app.state.ensure_forecasts(
                app.state.session_factory, run_id=run_id, tour_id=tour_id
            )

        with session_scope(app.state.session_factory) as session:
            repo = ReadRepository(session)
            active_run = repo.resolve_active_run(probe["season_id"])
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
        locked: list[str] | None = None,
        locked_starters: list[str] | None = None,
        formation: str | None = None,
        fixture_conflict_weight: float | None = None,
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
                locked=locked,
                locked_starters=locked_starters,
                formation=formation,
                fixture_conflict_weight=fixture_conflict_weight,
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
            locked=body.locked,
            locked_starters=body.locked_starters,
            formation=body.formation,
            fixture_conflict_weight=body.fixture_conflict_weight,
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
            locked=request.locked,
            locked_starters=request.locked_starters,
            formation=request.formation,
            fixture_conflict_weight=request.fixture_conflict_weight,
        )

    # ------------------------------------------------------------------
    # Admin ingestion (step 5), scoped to one league (step 22).
    # ------------------------------------------------------------------
    def _resolve_tournament_slug(slug: str) -> str:
        """Map a path slug to a tournament slug, keeping the legacy RPL alias.

        The original endpoints were ``/admin/ingestion/rpl/*`` while the
        Sports.ru slug is ``russia``; ``rpl`` therefore stays accepted so older
        clients and bookmarks keep working.
        """
        if slug == "rpl":
            return RPL_TOURNAMENT_SLUG
        with session_scope(app.state.session_factory) as session:
            known = ReadRepository(session).list_competitions()
        if not known:
            # Nothing catalogued yet: allow any slug so the very first import
            # (which also seeds the catalogue) is not blocked by a chicken-and-egg.
            return slug
        if any(item["slug"] == slug for item in known):
            return slug
        raise api_error(
            404,
            f"Unknown tournament slug {slug!r}",
            details={"known_slugs": [item["slug"] for item in known]},
        )

    def _enqueue_refresh(
        tournament_slug: str, body: RefreshRequest
    ) -> JSONResponse:
        with session_scope(app.state.session_factory) as session:
            repo = IngestionJobRepository(session)
            job, created = repo.enqueue(
                tournament_slug=tournament_slug,
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

    def _ingestion_status(tournament_slug: str) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            jobs = IngestionJobRepository(session)
            active = jobs.active_job(tournament_slug)
            latest = jobs.latest_job(tournament_slug)
            successful = jobs.latest_successful_job(tournament_slug)
            repo = ReadRepository(session)
            competition = repo.get_competition_by_slug(tournament_slug)
            payload = {
                "tournament_slug": tournament_slug,
                "competition": competition,
                "is_refreshing": active is not None
                and active.status in ACTIVE_STATUSES,
                "active_job": _job_summary(active) if active else None,
                "latest_job": _job_summary(latest) if latest else None,
                "latest_successful_job": (
                    _job_summary(successful) if successful else None
                ),
                "stages": [
                    {"stage": stage, "percent": percent} for stage, percent in STAGES
                ],
            }

            # Show the season the read API serves *for this league*: the most
            # recent one with a published snapshot, falling back to the most
            # recent import. Before the catalogue is synced the competition row
            # may not exist yet, in which case there is nothing imported either.
            seasons = (
                repo.list_seasons(competition_id=competition["competition_id"])
                if competition is not None
                else []
            )
            season = next(
                (item for item in seasons if item.get("snapshot")),
                seasons[0] if seasons else None,
            )
            payload["season"] = season
            payload["snapshot"] = season.get("snapshot") if season else None

            target_tour = None
            if season is not None:
                _, tours = repo.list_tours(
                    season_id=season["season_id"], limit=MAX_PAGE_LIMIT, offset=0
                )
                target_tour = next(
                    (
                        tour
                        for tour in tours
                        if (tour.get("status") or "").upper() != "FINISHED"
                    ),
                    tours[-1] if tours else None,
                )
            payload["target_tour"] = target_tour
        return payload

    @app.post(
        "/admin/competitions/sync",
        response_model=CatalogueSyncResponse,
        tags=["admin"],
    )
    def sync_competition_catalogue() -> dict[str, Any]:
        """Refresh the catalogue of leagues and their seasons from Sports.ru.

        The only admin call that reaches the GraphQL API inline: it is a single
        request, and without it a freshly created database has no league list for
        the switcher or the import screen to offer.
        """
        try:
            return app.state.sync_competitions(app.state.session_factory)
        except CompetitionCatalogueError as error:
            raise api_error(
                502, str(error), type_="upstream_error"
            ) from error

    @app.post(
        "/admin/ingestion/{tournament_slug}/refresh",
        status_code=202,
        tags=["admin"],
    )
    def refresh_competition(
        tournament_slug: str, request: RefreshRequest | None = None
    ) -> JSONResponse:
        """Queue an import of one league's season.

        The job is keyed by ``tournament_slug``, and the ``ingestion_jobs``
        partial unique index only forbids two concurrent refreshes *of the same
        league* — so different leagues can be imported in parallel.
        """
        return _enqueue_refresh(
            _resolve_tournament_slug(tournament_slug), request or RefreshRequest()
        )

    @app.get(
        "/admin/ingestion/runs/{job_id}",
        response_model=IngestionJobModel,
        tags=["admin"],
    )
    def get_run(job_id: int) -> dict[str, Any]:
        with session_scope(app.state.session_factory) as session:
            job = IngestionJobRepository(session).get(job_id)
            if job is None:
                raise api_error(404, f"Ingestion job {job_id} not found")
            return _job_to_dict(job)

    @app.get(
        "/admin/ingestion/{tournament_slug}/status",
        response_model=IngestionStatusResponse,
        tags=["admin"],
    )
    def ingestion_status(tournament_slug: str) -> dict[str, Any]:
        """Report one league's refresh state plus the snapshot and tour to show.

        The admin screen calls this on load (and while polling), so it never has
        to remember a job id: an in-flight refresh is discovered from the
        database, which is also what makes a second browser tab consistent.
        """
        return _ingestion_status(_resolve_tournament_slug(tournament_slug))

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
