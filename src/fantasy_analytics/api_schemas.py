"""Pydantic request/response contracts for the user REST API (step 9).

These models give the endpoints typed, validated request bodies/queries and make
the generated OpenAPI schema reflect the real payloads. Response models keep the
top-level shape strict while allowing the dynamic, model-specific parts (forecast
component breakdowns, optimizer explanations) to stay flexible.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# Pagination bounds shared by every list endpoint.
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200

Role = Literal["GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD"]
ForecastModel = Literal["poisson_events", "season_mean", "recent_form"]
PlayerOrder = Literal["projection", "price", "name", "selected_by", "season_score"]

# Formations are given as defenders-midfielders-forwards ("4-4-2"); the number of
# goalkeepers follows from the starting-eleven size, so it is not part of the
# string. Semantic checks against the season rules happen in the optimizer.
FORMATION_PATTERN = r"^\d{1,2}-\d{1,2}-\d{1,2}$"


# ---------------------------------------------------------------------------
# Envelopes: unified error format, pagination and snapshot metadata.
# ---------------------------------------------------------------------------
class ErrorDetail(BaseModel):
    type: str = Field(description="Machine-readable error category")
    message: str = Field(description="Human-readable error message")
    details: Any | None = Field(
        default=None, description="Optional structured error context"
    )


class ErrorResponse(BaseModel):
    error: ErrorDetail


class SnapshotMeta(BaseModel):
    run_id: int
    season_id: int | None = None
    data_freshness: str | None = Field(
        default=None, description="ISO time the active snapshot finished importing"
    )
    quality_checked_at: str | None = None


class PageMeta(BaseModel):
    limit: int
    offset: int
    total: int
    count: int


# ---------------------------------------------------------------------------
# Seasons.
# ---------------------------------------------------------------------------
class SeasonRulesModel(BaseModel):
    total_budget: float | None = None
    total_players: int | None = None
    starting_players: int | None = None
    full_roster_constraints: Any | None = None
    starting_roster_constraints: Any | None = None


class SeasonModel(BaseModel):
    season_id: int
    fantasy_season_id: str
    stat_season_id: str
    name: str
    competition_name: str | None = None
    is_active: bool
    starts_at: str | None = None
    ends_at: str | None = None
    snapshot: SnapshotMeta | None = None


class SeasonDetailModel(SeasonModel):
    rules: SeasonRulesModel | None = None


class SeasonListResponse(BaseModel):
    items: list[SeasonModel]
    pagination: PageMeta


# ---------------------------------------------------------------------------
# Tours.
# ---------------------------------------------------------------------------
class TourModel(BaseModel):
    tour_id: int
    season_id: int
    fantasy_tour_id: str
    name: str
    status: str
    starts_at: str | None = None
    finishes_at: str | None = None
    transfers_start_at: str | None = None
    transfers_deadline_at: str | None = None
    total_transfers: int | None = None
    max_same_team_players: int | None = None


class TourListResponse(BaseModel):
    items: list[TourModel]
    pagination: PageMeta


# ---------------------------------------------------------------------------
# Matches.
# ---------------------------------------------------------------------------
class MatchModel(BaseModel):
    match_id: int
    season_id: int
    tour_id: int | None = None
    fantasy_tour_id: str | None = None
    tour_name: str | None = None
    tour_status: str | None = None
    stat_match_id: str
    scheduled_at: str | None = None
    home_club_id: int
    home_club_name: str | None = None
    away_club_id: int
    away_club_name: str | None = None
    home_score: int | None = None
    away_score: int | None = None


class MatchListResponse(BaseModel):
    items: list[MatchModel]
    pagination: PageMeta


# ---------------------------------------------------------------------------
# Players and projections.
# ---------------------------------------------------------------------------
class ProjectionModel(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_name: str
    model_version: str
    feature_version: str | None = None
    scoring_version: str | None = None
    stat_source: str | None = Field(
        default=None,
        description="History source: 'current_season' or 'prior_season' (cross-season)",
    )
    has_history: bool | None = Field(
        default=None, description="Whether the projection is backed by real history"
    )
    match_id: int | None = None
    expected_points: float | None = None
    uncertainty: float | None = None
    p_appearance: float | None = None
    expected_minutes: float | None = None
    components: dict[str, float] | None = None


class PlayerModel(BaseModel):
    player_season_id: int
    fantasy_player_id: str | None = None
    player_name: str | None = None
    role: str
    club_id: int | None = None
    club_name: str | None = None
    price: float | None = None
    availability_status: str | None = None
    status_description: str | None = None
    selected_by: float | None = None
    form: int | None = None
    season_score: int | None = None
    average_score: float | None = None
    last_tour_score: int | None = None
    rank: int | None = None
    projection: ProjectionModel | None = None


class PlayerHistoryEntry(BaseModel):
    match_id: int
    tour_id: int | None = None
    scheduled_at: str | None = None
    minutes: int
    points: int
    goals: int
    assists: int
    saves: int
    ball_recoveries: int
    yellow_cards: int
    red_cards: int
    goals_conceded: int


class PlayerDetailModel(BaseModel):
    model_config = {"protected_namespaces": ()}

    player_season_id: int
    fantasy_player_id: str | None = None
    player_name: str | None = None
    role: str
    season_id: int
    club_id: int | None = None
    club_name: str | None = None
    price: float | None = None
    availability_status: str | None = None
    status_description: str | None = None
    selected_by: float | None = None
    form: int | None = None
    season_score: int | None = None
    average_score: float | None = None
    last_tour_score: int | None = None
    rank: int | None = None
    projection: ProjectionModel | None = None
    history: list[PlayerHistoryEntry] = Field(default_factory=list)
    snapshot: SnapshotMeta | None = None


class PlayerListResponse(BaseModel):
    items: list[PlayerModel]
    pagination: PageMeta
    snapshot: SnapshotMeta | None = None


# ---------------------------------------------------------------------------
# Optimizer requests and responses.
# ---------------------------------------------------------------------------
class SquadRequest(BaseModel):
    model_config = {"protected_namespaces": ()}

    run_id: int | None = Field(default=None, description="Ingestion run to build from")
    season: str | None = Field(
        default=None, description="Season fantasy id, stat id or name"
    )
    tour: str | None = Field(
        default=None, description="Target tour fantasy id or name"
    )
    model: ForecastModel = Field(
        default="poisson_events", description="Forecast model to optimize on"
    )
    locked: list[str] = Field(
        default_factory=list,
        description=(
            "Fantasy (or internal) player ids forced into the squad; the "
            "remaining slots are filled optimally"
        ),
    )
    locked_starters: list[str] = Field(
        default_factory=list,
        description="Player ids forced into the starting eleven (implies locked)",
    )
    formation: str | None = Field(
        default=None,
        pattern=FORMATION_PATTERN,
        description=(
            "Starting formation as defenders-midfielders-forwards, e.g. '4-4-2'; "
            "goalkeepers fill the remaining starting slots"
        ),
    )
    fixture_conflict_weight: float | None = Field(
        default=None,
        ge=0,
        description=(
            "How hard starters that meet each other in the tour are penalised "
            "(default 0.25); 0 ignores the schedule but still reports clashes"
        ),
    )


class TransfersRequest(SquadRequest):
    current_squad: list[str] = Field(
        description="Fantasy player ids of the current squad (limited-transfers mode)",
        min_length=1,
    )
    max_transfers: int | None = Field(
        default=None,
        ge=0,
        description="Override the tour's transfer limit",
    )


class SquadPlayerModel(BaseModel):
    player_season_id: int
    fantasy_player_id: str | None = None
    player_name: str | None = None
    role: str
    club_id: int
    club_name: str | None = None
    price: float
    expected_points: float
    opponent_name: str | None = None
    is_home: bool | None = None
    match_id: int | None = None
    opponent_club_id: int | None = None
    goal_upside: float | None = None
    shutout_stake: float | None = None
    p_appearance: float | None = None
    expected_minutes: float | None = None
    stat_source: str | None = None
    is_newcomer: bool | None = None
    is_starter: bool | None = None
    is_captain: bool | None = None
    is_vice_captain: bool | None = None
    is_locked: bool | None = None
    bench_order: int | None = None


class OptimizerResponse(BaseModel):
    """The optimizer report; nested explanation kept permissive on purpose."""

    model_config = {"protected_namespaces": (), "extra": "allow"}

    optimizer_version: str
    model: str
    mode: str
    generated_at: str
    run_id: int
    season_id: int
    season: dict[str, Any]
    tour: dict[str, Any]
    cutoff: str | None = None
    rules: dict[str, Any]
    counts: dict[str, Any]
    solution: dict[str, Any]
    valid: bool


# ---------------------------------------------------------------------------
# Admin ingestion (steps 5 and 17).
# ---------------------------------------------------------------------------
class IngestionProgressModel(BaseModel):
    """Coarse progress of a running refresh (see ingestion_progress.STAGES)."""

    stage: str = Field(description="Machine-readable stage key")
    percent: int = Field(ge=0, le=100, description="Rough completion percentage")
    message: str | None = Field(
        default=None, description="Latest progress note from the pipeline"
    )
    updated_at: str | None = None


class IngestionJobModel(BaseModel):
    """A manual refresh job as the admin UI sees it."""

    id: int
    status: Literal["pending", "running", "succeeded", "failed"]
    tournament_slug: str
    trigger_type: str | None = None
    requested_season_id: str | None = None
    requested_season_name: str | None = None
    use_current_season: bool | None = None
    ingestion_run_id: int | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    data_freshness: str | None = Field(
        default=None, description="Snapshot time published by a successful job"
    )
    progress: IngestionProgressModel | None = None
    result: dict[str, Any] | None = None


class IngestionStatusResponse(BaseModel):
    """Everything the admin refresh screen needs in a single request.

    It is deliberately answerable without knowing a job id, so a page reload
    (or a second browser tab) recovers the state of an in-flight refresh.
    """

    tournament_slug: str
    is_refreshing: bool = Field(
        description="True while a pending/running job exists for the tournament"
    )
    active_job: IngestionJobModel | None = None
    latest_job: IngestionJobModel | None = None
    latest_successful_job: IngestionJobModel | None = None
    snapshot: SnapshotMeta | None = Field(
        default=None, description="Active snapshot of the season shown in the UI"
    )
    season: SeasonModel | None = None
    target_tour: TourModel | None = Field(
        default=None, description="Next non-finished tour, else the last one"
    )
    stages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Ordered stage vocabulary with completion percentages",
    )


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "FORMATION_PATTERN",
    "MAX_PAGE_LIMIT",
    "ErrorDetail",
    "ErrorResponse",
    "SnapshotMeta",
    "PageMeta",
    "SeasonModel",
    "SeasonDetailModel",
    "SeasonListResponse",
    "TourModel",
    "TourListResponse",
    "MatchModel",
    "MatchListResponse",
    "ProjectionModel",
    "PlayerModel",
    "PlayerDetailModel",
    "PlayerListResponse",
    "SquadRequest",
    "TransfersRequest",
    "SquadPlayerModel",
    "OptimizerResponse",
    "IngestionJobModel",
    "IngestionProgressModel",
    "IngestionStatusResponse",
]
