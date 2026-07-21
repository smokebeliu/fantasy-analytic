"""SQLAlchemy 2.0 models mirroring the authoritative PostgreSQL schema.

These models are the single source of truth for the database structure. The
initial Alembic migration materializes this metadata, so there is no separate
hand-maintained DDL file to drift from the ORM definitions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base carrying the shared metadata object."""


def _identity_pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(always=True), primary_key=True)


class _PlayerStatMixin:
    """Non-nullable integer statistics reused by two grains."""

    points: Mapped[int] = mapped_column(Integer, nullable=False)
    goals: Mapped[int] = mapped_column(Integer, nullable=False)
    assists: Mapped[int] = mapped_column(Integer, nullable=False)
    saves: Mapped[int] = mapped_column(Integer, nullable=False)
    penalties_missed: Mapped[int] = mapped_column(Integer, nullable=False)
    penalties_post: Mapped[int] = mapped_column(Integer, nullable=False)
    penalties_target: Mapped[int] = mapped_column(Integer, nullable=False)
    penalties_saved: Mapped[int] = mapped_column(Integer, nullable=False)
    field_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    yellow_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    red_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    goals_conceded: Mapped[int] = mapped_column(Integer, nullable=False)
    penalty_goals_conceded: Mapped[int] = mapped_column(Integer, nullable=False)
    penalties_faced: Mapped[int] = mapped_column(Integer, nullable=False)
    penalty_conceded: Mapped[int] = mapped_column(Integer, nullable=False)
    own_goals: Mapped[int] = mapped_column(Integer, nullable=False)
    ball_recoveries: Mapped[int] = mapped_column(Integer, nullable=False)


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ingestion_runs_status_check",
        ),
        Index(
            "ingestion_runs_active_season_idx",
            "season_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[int] = _identity_pk()
    trigger_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'manual'")
    )
    tournament_slug: Mapped[str] = mapped_column(Text, nullable=False)
    requested_season_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    # Season resolved by the quality gate (step 4); a run only points at the
    # season once its snapshot has been evaluated.
    season_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("seasons.id", ondelete="SET NULL")
    )
    # A snapshot is only published (active) after passing the quality gate with
    # no blocking issues. At most one run per season may be active at a time,
    # enforced by the partial unique index above.
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    quality_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    report: Mapped[Any | None] = mapped_column(JSONB)

    raw_responses: Mapped[list[RawApiResponse]] = relationship(
        back_populates="ingestion_run",
        cascade="all, delete-orphan",
    )
    quality_issues: Mapped[list[DataQualityIssue]] = relationship(
        back_populates="ingestion_run",
        cascade="all, delete-orphan",
    )


class IngestionJob(Base):
    """A manual refresh job (development-plan step 5).

    The job is the admin-facing unit of work behind ``POST /admin/ingestion``:
    it wraps a full import plus the quality gate and survives an API restart
    because its state lives in PostgreSQL. A partial unique index guarantees at
    most one *active* (``pending``/``running``) job per tournament, which is how
    "no more than one refresh runs concurrently" is enforced at the data layer;
    a session-level advisory lock in the worker is the second line of defence.
    Once the worker creates the underlying import, ``ingestion_run_id`` links the
    job to its :class:`IngestionRun`.
    """

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ingestion_jobs_status_check",
        ),
        Index(
            "ingestion_jobs_active_tournament_idx",
            "tournament_slug",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[int] = _identity_pk()
    trigger_type: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'manual'")
    )
    tournament_slug: Mapped[str] = mapped_column(Text, nullable=False)
    requested_season_id: Mapped[str | None] = mapped_column(Text)
    requested_season_name: Mapped[str | None] = mapped_column(Text)
    use_current_season: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )
    ingestion_run_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("ingestion_runs.id", ondelete="SET NULL")
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    result: Mapped[Any | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawApiResponse(Base):
    __tablename__ = "raw_api_responses"
    __table_args__ = (
        UniqueConstraint(
            "ingestion_run_id",
            "operation_name",
            "response_hash",
            name="raw_api_responses_run_operation_hash_key",
        ),
    )

    id: Mapped[int] = _identity_pk()
    ingestion_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingestion_runs.id"), nullable=False
    )
    operation_name: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[Any] = mapped_column(JSONB, nullable=False)
    response: Mapped[Any] = mapped_column(JSONB, nullable=False)
    response_hash: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ingestion_run: Mapped[IngestionRun] = relationship(
        back_populates="raw_responses"
    )


class Competition(Base):
    __tablename__ = "competitions"

    id: Mapped[int] = _identity_pk()
    fantasy_tournament_id: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)


class Season(Base):
    __tablename__ = "seasons"

    id: Mapped[int] = _identity_pk()
    competition_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("competitions.id"), nullable=False
    )
    fantasy_season_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    stat_season_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SeasonRules(Base):
    __tablename__ = "season_rules"

    season_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("seasons.id", ondelete="CASCADE"),
        primary_key=True,
    )
    rules_html: Mapped[str] = mapped_column(Text, nullable=False)
    total_budget: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    total_players: Mapped[int] = mapped_column(Integer, nullable=False)
    starting_players: Mapped[int] = mapped_column(Integer, nullable=False)
    full_roster_constraints: Mapped[Any] = mapped_column(JSONB, nullable=False)
    starting_roster_constraints: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Club(Base):
    __tablename__ = "clubs"

    id: Mapped[int] = _identity_pk()
    stat_team_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)


class SeasonClub(Base):
    __tablename__ = "season_clubs"
    __table_args__ = (
        UniqueConstraint("season_id", "club_id", name="season_clubs_season_club_key"),
        UniqueConstraint(
            "season_id",
            "fantasy_team_id",
            name="season_clubs_season_fantasy_team_key",
        ),
    )

    id: Mapped[int] = _identity_pk()
    season_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    club_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clubs.id"), nullable=False
    )
    fantasy_team_id: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)


class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = _identity_pk()
    stat_player_id: Mapped[str | None] = mapped_column(Text, unique=True)
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)


class PlayerSeason(Base):
    __tablename__ = "player_seasons"
    __table_args__ = (
        UniqueConstraint(
            "season_id",
            "fantasy_player_id",
            name="player_seasons_season_fantasy_player_key",
        ),
        UniqueConstraint(
            "season_id", "player_id", name="player_seasons_season_player_key"
        ),
        CheckConstraint(
            "role IN ('GOALKEEPER', 'DEFENDER', 'MIDFIELDER', 'FORWARD')",
            name="player_seasons_role_check",
        ),
    )

    id: Mapped[int] = _identity_pk()
    season_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    player_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("players.id"), nullable=False
    )
    fantasy_player_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    current_season_club_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("season_clubs.id")
    )


class FantasyTour(Base):
    __tablename__ = "fantasy_tours"
    __table_args__ = (
        UniqueConstraint(
            "season_id", "fantasy_tour_id", name="fantasy_tours_season_tour_key"
        ),
    )

    id: Mapped[int] = _identity_pk()
    season_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    fantasy_tour_id: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finishes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    transfers_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    transfers_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    total_transfers: Mapped[int | None] = mapped_column(Integer)
    max_same_team_players: Mapped[int | None] = mapped_column(Integer)


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        Index("matches_season_scheduled_idx", "season_id", "scheduled_at"),
    )

    id: Mapped[int] = _identity_pk()
    season_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    tour_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("fantasy_tours.id")
    )
    stat_match_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    home_club_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clubs.id"), nullable=False
    )
    away_club_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clubs.id"), nullable=False
    )
    home_score: Mapped[int | None] = mapped_column(Integer)
    away_score: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FantasyPlayerSnapshot(Base):
    __tablename__ = "fantasy_player_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "player_season_id",
            "ingestion_run_id",
            name="fantasy_player_snapshots_player_run_key",
        ),
        Index(
            "fantasy_player_snapshots_latest_idx",
            "player_season_id",
            text("captured_at DESC"),
        ),
    )

    id: Mapped[int] = _identity_pk()
    player_season_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("player_seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    ingestion_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingestion_runs.id"), nullable=False
    )
    season_club_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("season_clubs.id")
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    price: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    availability_status: Mapped[str] = mapped_column(Text, nullable=False)
    status_description: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    selected_by: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    form: Mapped[int | None] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(Integer)
    season_score: Mapped[int | None] = mapped_column(Integer)
    average_score: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
    last_tour_score: Mapped[int | None] = mapped_column(Integer)
    top_percent: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))


class PlayerSeasonStats(_PlayerStatMixin, Base):
    __tablename__ = "player_season_stats"
    __table_args__ = (
        UniqueConstraint(
            "player_season_id",
            "ingestion_run_id",
            name="player_season_stats_player_run_key",
        ),
    )

    id: Mapped[int] = _identity_pk()
    player_season_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("player_seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    ingestion_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingestion_runs.id"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class PlayerMatchStats(_PlayerStatMixin, Base):
    __tablename__ = "player_match_stats"
    __table_args__ = (
        UniqueConstraint(
            "player_season_id", "match_id", name="player_match_stats_player_match_key"
        ),
        Index("player_match_stats_player_idx", "player_season_id", "match_id"),
    )

    id: Mapped[int] = _identity_pk()
    player_season_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("player_seasons.id", ondelete="CASCADE"),
        nullable=False,
    )
    match_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    tour_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("fantasy_tours.id"), nullable=False
    )
    season_club_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("season_clubs.id")
    )
    ingestion_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingestion_runs.id"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FantasyPointDetail(Base):
    __tablename__ = "fantasy_point_details"
    __table_args__ = (
        UniqueConstraint(
            "player_match_stat_id",
            "ordinal",
            name="fantasy_point_details_stat_ordinal_key",
        ),
    )

    id: Mapped[int] = _identity_pk()
    player_match_stat_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("player_match_stats.id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)


class ClubSeasonStats(Base):
    __tablename__ = "club_season_stats"
    __table_args__ = (
        UniqueConstraint(
            "season_club_id",
            "ingestion_run_id",
            name="club_season_stats_club_run_key",
        ),
    )

    id: Mapped[int] = _identity_pk()
    season_club_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("season_clubs.id", ondelete="CASCADE"),
        nullable=False,
    )
    ingestion_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingestion_runs.id"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    matches_played: Mapped[int] = mapped_column(Integer, nullable=False)
    matches_won: Mapped[int] = mapped_column(Integer, nullable=False)
    matches_drawn: Mapped[int] = mapped_column(Integer, nullable=False)
    matches_lost: Mapped[int] = mapped_column(Integer, nullable=False)
    goals_scored: Mapped[int] = mapped_column(Integer, nullable=False)
    goals_conceded: Mapped[int] = mapped_column(Integer, nullable=False)
    yellow_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    red_cards: Mapped[int] = mapped_column(Integer, nullable=False)
    clean_sheets: Mapped[int | None] = mapped_column(Integer)
    home_matches: Mapped[int | None] = mapped_column(Integer)
    home_goals_scored: Mapped[int | None] = mapped_column(Integer)
    home_goals_conceded: Mapped[int | None] = mapped_column(Integer)
    away_matches: Mapped[int | None] = mapped_column(Integer)
    away_goals_scored: Mapped[int | None] = mapped_column(Integer)
    away_goals_conceded: Mapped[int | None] = mapped_column(Integer)


class ClubMatchStats(Base):
    __tablename__ = "club_match_stats"
    __table_args__ = (
        UniqueConstraint(
            "match_id", "club_id", name="club_match_stats_match_club_key"
        ),
    )

    id: Mapped[int] = _identity_pk()
    match_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    club_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clubs.id"), nullable=False
    )
    opponent_club_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("clubs.id"), nullable=False
    )
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False)
    goals_scored: Mapped[int | None] = mapped_column(Integer)
    goals_conceded: Mapped[int | None] = mapped_column(Integer)
    provider_metrics: Mapped[Any] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    ingestion_run_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("ingestion_runs.id"), nullable=False
    )


class DataQualityIssue(Base):
    """One violation recorded by the quality gate (development-plan step 4).

    Every issue is scoped to the ingestion run whose snapshot was evaluated.
    ``severity`` is either ``blocking`` (prevents the snapshot from becoming
    active) or ``warning`` (recorded but non-fatal, e.g. discrepancies that the
    72-hour Sports.ru adjustment window can still explain). ``expected`` and
    ``actual`` hold the human-readable values behind each check so the report
    can show both sides of every comparison.
    """

    __tablename__ = "data_quality_issues"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('blocking', 'warning')",
            name="data_quality_issues_severity_check",
        ),
        Index("data_quality_issues_run_idx", "ingestion_run_id"),
        Index("data_quality_issues_run_severity_idx", "ingestion_run_id", "severity"),
    )

    id: Mapped[int] = _identity_pk()
    ingestion_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ingestion_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    season_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("seasons.id", ondelete="CASCADE")
    )
    check_name: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(Text)
    entity_ref: Mapped[str | None] = mapped_column(Text)
    expected: Mapped[str | None] = mapped_column(Text)
    actual: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[Any | None] = mapped_column(JSONB)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    ingestion_run: Mapped[IngestionRun] = relationship(
        back_populates="quality_issues"
    )


__all__ = [
    "Base",
    "Club",
    "ClubMatchStats",
    "ClubSeasonStats",
    "Competition",
    "DataQualityIssue",
    "FantasyPlayerSnapshot",
    "FantasyPointDetail",
    "FantasyTour",
    "IngestionJob",
    "IngestionRun",
    "Match",
    "Player",
    "PlayerMatchStats",
    "PlayerSeason",
    "PlayerSeasonStats",
    "RawApiResponse",
    "Season",
    "SeasonClub",
    "SeasonRules",
]
