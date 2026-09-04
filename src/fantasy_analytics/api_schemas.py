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
ForecastModel = Literal["poisson_events", "season_mean", "recent_form", "ridge_stack"]
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
    label: str = Field(
        description=(
            "Display name, unique within the competition: the season name, with "
            "the fantasy id appended when a tournament splits one season into "
            "phases (Champions League league phase vs knockout stage)"
        )
    )
    competition_id: int | None = None
    competition_name: str | None = None
    competition_slug: str | None = None
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
# Competitions (step 22).
# ---------------------------------------------------------------------------
class CatalogueSeasonModel(BaseModel):
    """A season Sports.ru exposes for a league, imported or not."""

    fantasy_season_id: str
    stat_season_id: str | None = None
    name: str
    label: str
    is_active: bool


class CompetitionModel(BaseModel):
    """One fantasy league with its catalogue and what has been imported."""

    competition_id: int
    fantasy_tournament_id: str
    slug: str = Field(description="Tournament slug, e.g. russia, spain, italy")
    name: str
    sort_order: int
    catalogue_synced_at: str | None = None
    available_seasons: list[CatalogueSeasonModel] = Field(
        default_factory=list,
        description="Seasons Sports.ru offers for this league, oldest first",
    )
    has_active_season: bool = Field(
        description="Whether a season is currently in progress (affects 'refresh active season')"
    )
    seasons: list[SeasonModel] = Field(
        default_factory=list, description="Imported seasons, newest first"
    )
    latest_season: SeasonModel | None = Field(
        default=None,
        description="Season the read API serves: the newest published one",
    )
    snapshot: SnapshotMeta | None = None
    is_imported: bool


class CompetitionListResponse(BaseModel):
    items: list[CompetitionModel]
    pagination: PageMeta


class CatalogueSyncResponse(BaseModel):
    """Result of refreshing the league catalogue from Sports.ru."""

    synced_at: str
    competitions: int
    seasons: int
    slugs: list[str]


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


class PriorSeasonModel(BaseModel):
    """What the same player did in the previous season.

    Early in a new season the current-season columns are nearly empty, so these
    numbers are what a manager actually judges a player by. They come from the
    previous season's published snapshot, matched through the cross-season
    player identity, and are absent when only one season has been imported.
    """

    season_id: int
    season_name: str | None = None
    player_season_id: int
    role: str | None = None
    club_name: str | None = Field(
        default=None, description="Club the player belonged to last season"
    )
    points: int | None = Field(
        default=None, description="Fantasy points scored over the whole season"
    )
    average_points: float | None = None
    rank: int | None = Field(
        default=None, description="Fantasy rank inside the season (1 is best)"
    )
    price: float | None = Field(
        default=None, description="Price at the end of the previous season"
    )
    matches: int | None = Field(
        default=None, description="Matches with at least one minute played"
    )
    minutes: int | None = None
    goals: int | None = None
    assists: int | None = None
    saves: int | None = None
    ball_recoveries: int | None = None
    yellow_cards: int | None = None
    red_cards: int | None = None
    goals_conceded: int | None = None


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
    prior_season: PriorSeasonModel | None = None


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
    prior_season: PriorSeasonModel | None = None
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
    competition: str | None = Field(
        default=None,
        description=(
            "League to optimize: tournament slug or fantasy tournament id. With "
            "several leagues imported there is no single active snapshot, so name "
            "this (or season/run_id) to avoid getting whichever league was "
            "published most recently"
        ),
    )
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
    captain_risk_weight: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Standard deviations of a player's forecast added to his captain "
            "score, so the armband goes to the upper tail rather than the mean "
            "(default 0.5)"
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
    budget: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Money the plan may spend on the whole roster (team value plus "
            "bank, as reported by the squad import); defaults to the season's "
            "opening budget"
        ),
    )
    min_transfer_gain: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Least expected-points gain a single transfer must bring to be "
            "proposed (default 0.5; 0 proposes any gain)"
        ),
    )
    transfer_gain_sigma: float | None = Field(
        default=None,
        ge=0,
        description=(
            "Extra margin a transfer must clear, in standard deviations of "
            "both players' forecasts (default 0)"
        ),
    )
    horizon_tours: int | None = Field(
        default=None,
        ge=1,
        le=6,
        description=(
            "How many tours the roster is judged on: the target tour plus the "
            "following ones, forecast from the target tour's cutoff and "
            "discounted by horizon_decay per tour (default 2)"
        ),
    )
    horizon_decay: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Discount per tour ahead for horizon_tours (default 0.7)",
    )


class ImportSquadRequest(BaseModel):
    """Load a manager's Sports.ru team from its public URL into the builder."""

    url: str = Field(
        min_length=1,
        description=(
            "Public team page, e.g. https://www.sports.ru/fantasy/football/"
            "portugal/588960/, or a bare squad id"
        ),
    )
    season_id: int = Field(description="Internal season the players are resolved in")
    tour_id: int | None = Field(
        default=None,
        description="Tour whose prices and projections are attached to the players",
    )
    competition: str | None = Field(
        default=None,
        description=(
            "Expected league slug. The import is rejected when the URL or the "
            "remote team belongs to a different league"
        ),
    )
    model: ForecastModel = Field(
        default="poisson_events",
        description="Forecast model attached to the resolved players",
    )


class MissingImportedPlayer(BaseModel):
    fantasy_player_id: str
    player_name: str | None = None
    role: str | None = None


class RemoteTourRef(BaseModel):
    fantasy_tour_id: str
    name: str
    status: str


class ImportSquadResponse(BaseModel):
    squad_id: str
    squad_name: str
    competition_slug: str
    competition_name: str | None = None
    remote_season_id: str
    remote_tour: RemoteTourRef | None = None
    players: list[PlayerModel]
    missing: list[MissingImportedPlayer] = Field(default_factory=list)
    total_price: float | None = Field(
        default=None, description="What the roster is worth on Sports.ru"
    )
    current_balance: float | None = Field(
        default=None, description="Money left in the bank on Sports.ru"
    )
    budget: float | None = Field(
        default=None,
        description="total_price + current_balance: the money a transfer plan may spend",
    )
    transfers_left: int | None = Field(
        default=None, description="Free transfers still available this tour"
    )
    transfers_done: int | None = Field(
        default=None, description="Transfers already made this tour"
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


class NightlyEligibleLeague(BaseModel):
    """A league whose current season is already imported and will be refreshed."""

    tournament_slug: str
    season_id: int
    fantasy_season_id: str
    season_name: str


class NightlySkippedLeague(BaseModel):
    """A league the sweep considered but did not enqueue."""

    tournament_slug: str
    reason: str


class NightlyRefreshReport(BaseModel):
    """Result of one nightly sweep across every eligible league."""

    ran_at: str
    force: bool
    eligible: list[NightlyEligibleLeague]
    enqueued: list[IngestionJobModel]
    skipped: list[NightlySkippedLeague]


class NightlyRefreshStatus(BaseModel):
    """Scheduler configuration, who is eligible, and today's scheduled jobs."""

    enabled: bool
    timezone: str
    hour: int
    minute: int
    catchup_hours: int
    now: str
    next_run_at: str
    eligible: list[NightlyEligibleLeague]
    scheduled_today: list[IngestionJobModel]


class OddsStatusModel(BaseModel):
    """Compact odds block on the admin refresh status."""

    season_id: int
    synced_at: str | None = None
    matches: int = 0
    tour_id: int | None = None
    tour_name: str | None = None
    sports_tag_id: str | None = None


class IngestionStatusResponse(BaseModel):
    """Everything the admin refresh screen needs in a single request.

    It is deliberately answerable without knowing a job id, so a page reload
    (or a second browser tab) recovers the state of an in-flight refresh.
    """

    tournament_slug: str
    competition: CompetitionModel | None = Field(
        default=None,
        description="The league this status describes, once its catalogue is known",
    )
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
    odds: OddsStatusModel | None = Field(
        default=None,
        description="Last 1x2 refresh for this league's imported season",
    )


class FullRefreshStepModel(BaseModel):
    """One step of the one-button refresh: a season import or a league's odds."""

    tournament_slug: str
    competition_name: str | None = None
    kind: Literal["latest_completed", "current_season", "odds"]
    status: Literal["pending", "running", "succeeded", "failed", "skipped"]
    job_id: int | None = Field(
        default=None, description="The ingestion job an import step created or waited for"
    )
    detail: str | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class FullRefreshRunModel(BaseModel):
    """A full refresh: every imported league, both seasons, then odds, in order."""

    id: int
    status: Literal["running", "succeeded", "failed"]
    started_at: str
    finished_at: str | None = None
    total_steps: int
    completed_steps: int
    failed_steps: int
    current_step: FullRefreshStepModel | None = None
    steps: list[FullRefreshStepModel]


class FullRefreshStatus(BaseModel):
    """The run in flight, or the last one this process ran (none after a restart)."""

    is_running: bool
    run: FullRefreshRunModel | None = None


class OddsRefreshResponse(BaseModel):
    """Result of fetching the 1x2 line for one league."""

    tournament_slug: str
    competition_name: str | None = None
    season_id: int
    season_name: str
    sports_tag_id: str | None = None
    synced_at: str
    calendar_matches: int = 0
    fetched: int = 0
    stored: int = 0
    linked: int = 0
    unmatched: int = 0
    tour_id: int | None = None
    tour_name: str | None = None
    run_id: int | None = None
    forecast_rows: int = 0


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "FORMATION_PATTERN",
    "MAX_PAGE_LIMIT",
    "ErrorDetail",
    "ErrorResponse",
    "SnapshotMeta",
    "PageMeta",
    "CatalogueSeasonModel",
    "CatalogueSyncResponse",
    "CompetitionListResponse",
    "CompetitionModel",
    "SeasonModel",
    "SeasonDetailModel",
    "SeasonListResponse",
    "TourModel",
    "TourListResponse",
    "MatchModel",
    "MatchListResponse",
    "ProjectionModel",
    "PriorSeasonModel",
    "PlayerModel",
    "PlayerDetailModel",
    "PlayerListResponse",
    "SquadRequest",
    "TransfersRequest",
    "ImportSquadRequest",
    "ImportSquadResponse",
    "MissingImportedPlayer",
    "RemoteTourRef",
    "SquadPlayerModel",
    "OptimizerResponse",
    "IngestionJobModel",
    "IngestionProgressModel",
    "IngestionStatusResponse",
    "OddsRefreshResponse",
    "OddsStatusModel",
    "NightlyEligibleLeague",
    "NightlyRefreshReport",
    "NightlyRefreshStatus",
    "NightlySkippedLeague",
]
