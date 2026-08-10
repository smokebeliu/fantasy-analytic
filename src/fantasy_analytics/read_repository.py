"""Read-only query layer for the user-facing REST API (development-plan step 9).

Every function here reads exclusively from PostgreSQL: the Sports.ru GraphQL API
is *never* touched by a read endpoint. Catalog entities (seasons, tours, matches,
players and clubs) are stored idempotently and are always available, while the
mutable per-player numbers (price, availability, ownership, season score) come
from the single *active* snapshot published by the quality gate (step 4).
Projections and their explaining components are served from the persisted
``player_forecasts`` rows (step 7) for a given tour and model.

Everything is scoped by season, and a season belongs to exactly one competition,
so the same queries serve every imported league (step 22). :meth:`
ReadRepository.list_competitions` is the entry point a league switcher needs: it
joins the stored catalogue of leagues to what has actually been imported.

Every player also carries a ``prior_season`` block: what the same person did in
the previous season, resolved through the cross-season identity
(``players.stat_player_id``) that step 14 already relies on. Early in a new
season the current-season numbers are still nearly empty, so last season's
points, average and rank are what actually tell a manager whether a player is
worth buying.

The repository returns plain, JSON-serialisable ``dict``s so the API layer only
has to validate and shape them with Pydantic; it never commits or mutates data.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, null, select
from sqlalchemy.orm import Session

from .db.models import (
    Club,
    Competition,
    FantasyPlayerSnapshot,
    FantasyTour,
    IngestionRun,
    Match,
    Player,
    PlayerForecast,
    PlayerMatchStats,
    PlayerSeason,
    PlayerSeasonStats,
    Season,
    SeasonClub,
    SeasonRules,
)
from .features import resolve_prior_run

# Roles accepted by the ``role`` player filter.
ROLES = ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")

# Ordering keys the players endpoint understands.
PLAYER_ORDERS = ("projection", "price", "name", "selected_by", "season_score")


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


class ReadRepository:
    """Read-only access to catalog, snapshot and forecast data."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Snapshot resolution.
    # ------------------------------------------------------------------
    def resolve_active_run(self, season_id: int) -> IngestionRun | None:
        """Return the season's published (active) ingestion run, if any."""
        return self._session.execute(
            select(IngestionRun)
            .where(
                IngestionRun.season_id == season_id,
                IngestionRun.is_active.is_(True),
            )
            .order_by(IngestionRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def snapshot_meta(self, run: IngestionRun | None) -> dict[str, Any] | None:
        if run is None:
            return None
        return {
            "run_id": run.id,
            "season_id": run.season_id,
            "data_freshness": _iso(run.finished_at),
            "quality_checked_at": _iso(run.quality_checked_at),
        }

    # ------------------------------------------------------------------
    # Competitions.
    # ------------------------------------------------------------------
    def list_competitions(
        self, *, imported_only: bool = False
    ) -> list[dict[str, Any]]:
        """List the leagues in the catalogue with what has been imported.

        Every league Sports.ru offers appears once the catalogue has been synced
        (:mod:`fantasy_analytics.competitions`); ``seasons`` holds only the
        seasons an import actually produced, so the switcher can show a league as
        available-but-empty and the admin screen can offer it for import.
        ``imported_only`` narrows the result to what the read API can serve,
        which is what a league switcher wants.
        """
        competitions = list(
            self._session.execute(
                select(Competition).order_by(Competition.sort_order, Competition.id)
            ).scalars()
        )
        seasons_by_competition: dict[int, list[dict[str, Any]]] = {}
        for season in self.list_seasons():
            seasons_by_competition.setdefault(season["competition_id"], []).append(
                season
            )

        payload: list[dict[str, Any]] = []
        for competition in competitions:
            seasons = seasons_by_competition.get(competition.id, [])
            if imported_only and not seasons:
                continue
            # Prefer a season the read API can actually serve; a league whose
            # only import was blocked by the quality gate still reports its
            # newest season so the UI can say what it tried to publish.
            published = next(
                (season for season in seasons if season.get("snapshot")), None
            )
            latest = published or (seasons[0] if seasons else None)
            available = list(competition.available_seasons or [])
            payload.append(
                {
                    "competition_id": competition.id,
                    "fantasy_tournament_id": competition.fantasy_tournament_id,
                    "slug": competition.slug,
                    "name": competition.name,
                    "sort_order": competition.sort_order,
                    "catalogue_synced_at": _iso(competition.catalogue_synced_at),
                    "available_seasons": available,
                    "has_active_season": any(
                        bool(season.get("is_active")) for season in available
                    ),
                    "seasons": seasons,
                    "latest_season": latest,
                    "snapshot": (latest or {}).get("snapshot"),
                    "is_imported": bool(seasons),
                }
            )
        return payload

    def get_competition_by_slug(self, slug: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.list_competitions() if item["slug"] == slug), None
        )

    # ------------------------------------------------------------------
    # Seasons.
    # ------------------------------------------------------------------
    def _season_dict(
        self,
        season: Season,
        competition: Competition | None,
        label: str | None = None,
    ) -> dict[str, Any]:
        active_run = self.resolve_active_run(season.id)
        return {
            "season_id": season.id,
            "fantasy_season_id": season.fantasy_season_id,
            "stat_season_id": season.stat_season_id,
            "name": season.name,
            "label": label or season.name,
            "competition_id": season.competition_id,
            "competition_name": competition.name if competition else None,
            "competition_slug": competition.slug if competition else None,
            "is_active": season.is_active,
            "starts_at": _iso(season.starts_at),
            "ends_at": _iso(season.ends_at),
            "snapshot": self.snapshot_meta(active_run),
        }

    def _labels_for(self, seasons: Sequence[Season]) -> dict[int, str]:
        """Label each season uniquely within its competition.

        A stat season name repeats inside a competition whenever a tournament is
        split into phases — the Champions and Europa League publish a league-phase
        and a knockout season per year, both named e.g. ``2025/2026``. Only those
        get the fantasy season id appended, so domestic leagues stay clean.
        """
        counts: dict[tuple[int, str], int] = {}
        for season in seasons:
            key = (season.competition_id, season.name)
            counts[key] = counts.get(key, 0) + 1
        return {
            season.id: (
                f"{season.name} (#{season.fantasy_season_id})"
                if counts[(season.competition_id, season.name)] > 1
                else season.name
            )
            for season in seasons
        }

    def list_seasons(
        self, *, competition_id: int | None = None
    ) -> list[dict[str, Any]]:
        conditions = []
        if competition_id is not None:
            conditions.append(Season.competition_id == competition_id)
        rows = self._session.execute(
            select(Season, Competition)
            .join(Competition, Season.competition_id == Competition.id)
            .where(*conditions)
            .order_by(
                Season.starts_at.is_(None), Season.starts_at.desc(), Season.id.desc()
            )
        ).all()
        # Labels must be unique per competition, so they are derived from every
        # season of the competitions in the result, not from the current page.
        labels = self._labels_for(
            list(
                self._session.execute(
                    select(Season).where(
                        Season.competition_id.in_(
                            {season.competition_id for season, _ in rows} or {-1}
                        )
                    )
                ).scalars()
            )
        )
        return [
            self._season_dict(season, competition, labels.get(season.id))
            for season, competition in rows
        ]

    def get_season(self, season_id: int) -> dict[str, Any] | None:
        row = self._session.execute(
            select(Season, Competition)
            .join(Competition, Season.competition_id == Competition.id)
            .where(Season.id == season_id)
        ).one_or_none()
        if row is None:
            return None
        season, competition = row
        labels = self._labels_for(
            list(
                self._session.execute(
                    select(Season).where(
                        Season.competition_id == season.competition_id
                    )
                ).scalars()
            )
        )
        payload = self._season_dict(season, competition, labels.get(season.id))
        rules = self._session.get(SeasonRules, season_id)
        if rules is not None:
            payload["rules"] = {
                "total_budget": _num(rules.total_budget),
                "total_players": rules.total_players,
                "starting_players": rules.starting_players,
                "full_roster_constraints": rules.full_roster_constraints,
                "starting_roster_constraints": rules.starting_roster_constraints,
            }
        else:
            payload["rules"] = None
        return payload

    # ------------------------------------------------------------------
    # Tours.
    # ------------------------------------------------------------------
    def _tour_dict(self, tour: FantasyTour) -> dict[str, Any]:
        return {
            "tour_id": tour.id,
            "season_id": tour.season_id,
            "fantasy_tour_id": tour.fantasy_tour_id,
            "name": tour.name,
            "status": tour.status,
            "starts_at": _iso(tour.starts_at),
            "finishes_at": _iso(tour.finishes_at),
            "transfers_start_at": _iso(tour.transfers_start_at),
            "transfers_deadline_at": _iso(tour.transfers_deadline_at),
            "total_transfers": tour.total_transfers,
            "max_same_team_players": tour.max_same_team_players,
        }

    def list_tours(
        self,
        *,
        season_id: int | None = None,
        status: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        conditions = []
        if season_id is not None:
            conditions.append(FantasyTour.season_id == season_id)
        if status is not None:
            conditions.append(func.upper(FantasyTour.status) == status.upper())

        total = self._session.execute(
            select(func.count()).select_from(FantasyTour).where(*conditions)
        ).scalar_one()
        tours = self._session.execute(
            select(FantasyTour)
            .where(*conditions)
            .order_by(
                FantasyTour.season_id,
                FantasyTour.starts_at.is_(None),
                FantasyTour.starts_at,
                FantasyTour.id,
            )
            .limit(limit)
            .offset(offset)
        ).scalars()
        return total, [self._tour_dict(tour) for tour in tours]

    def get_tour(self, tour_id: int) -> dict[str, Any] | None:
        tour = self._session.get(FantasyTour, tour_id)
        return self._tour_dict(tour) if tour is not None else None

    # ------------------------------------------------------------------
    # Matches.
    # ------------------------------------------------------------------
    def _match_query(self, conditions):
        home = Club.__table__.alias("home_club")
        away = Club.__table__.alias("away_club")
        return (
            select(
                Match.id,
                Match.season_id,
                Match.tour_id,
                Match.stat_match_id,
                Match.scheduled_at,
                Match.home_club_id,
                Match.away_club_id,
                Match.home_score,
                Match.away_score,
                home.c.canonical_name.label("home_name"),
                away.c.canonical_name.label("away_name"),
                FantasyTour.fantasy_tour_id,
                FantasyTour.name.label("tour_name"),
                FantasyTour.status.label("tour_status"),
            )
            .join(home, Match.home_club_id == home.c.id)
            .join(away, Match.away_club_id == away.c.id)
            .join(FantasyTour, Match.tour_id == FantasyTour.id, isouter=True)
            .where(*conditions)
        )

    @staticmethod
    def _match_dict(row: Any) -> dict[str, Any]:
        return {
            "match_id": row.id,
            "season_id": row.season_id,
            "tour_id": row.tour_id,
            "fantasy_tour_id": row.fantasy_tour_id,
            "tour_name": row.tour_name,
            "tour_status": row.tour_status,
            "stat_match_id": row.stat_match_id,
            "scheduled_at": _iso(row.scheduled_at),
            "home_club_id": row.home_club_id,
            "home_club_name": row.home_name,
            "away_club_id": row.away_club_id,
            "away_club_name": row.away_name,
            "home_score": row.home_score,
            "away_score": row.away_score,
        }

    def list_matches(
        self,
        *,
        season_id: int | None = None,
        tour_id: int | None = None,
        club_id: int | None = None,
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        conditions = []
        if season_id is not None:
            conditions.append(Match.season_id == season_id)
        if tour_id is not None:
            conditions.append(Match.tour_id == tour_id)
        if club_id is not None:
            conditions.append(
                (Match.home_club_id == club_id) | (Match.away_club_id == club_id)
            )

        total = self._session.execute(
            select(func.count()).select_from(Match).where(*conditions)
        ).scalar_one()
        rows = self._session.execute(
            self._match_query(conditions)
            .order_by(Match.scheduled_at, Match.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return total, [self._match_dict(row) for row in rows]

    def get_match(self, match_id: int) -> dict[str, Any] | None:
        row = self._session.execute(
            self._match_query([Match.id == match_id])
        ).one_or_none()
        return self._match_dict(row) if row is not None else None

    # ------------------------------------------------------------------
    # Prior-season stats.
    # ------------------------------------------------------------------
    def _prior_season_context(
        self, season_id: int
    ) -> tuple[Season, IngestionRun] | None:
        """Resolve the previous season and the snapshot its numbers come from.

        Shares the feature builder's definition of a prior season — another
        season of the same competition with a published snapshot — but insists it
        actually started earlier, since a block labelled "last season" must not
        show a later one. Returns ``None`` when nothing precedes this season,
        which simply means no player has a prior-season block.
        """
        season = self._session.get(Season, season_id)
        if season is None:
            return None
        prior_run = resolve_prior_run(self._session, season, require_earlier=True)
        if prior_run is None or prior_run.season_id is None:
            return None
        prior_season = self._session.get(Season, prior_run.season_id)
        if prior_season is None:
            return None
        return prior_season, prior_run

    def prior_season_stats(
        self, season_id: int, player_season_ids: Sequence[int]
    ) -> dict[int, dict[str, Any]]:
        """Map current ``player_season_id`` to that player's previous season.

        One batched query per grain rather than a lookup per player, so listing
        200 players costs three extra statements regardless of page size. The
        link between the two seasons is ``player_seasons.player_id``, the same
        cross-season identity the forecast uses.
        """
        ids = list(dict.fromkeys(player_season_ids))
        if not ids:
            return {}
        context = self._prior_season_context(season_id)
        if context is None:
            return {}
        prior_season, prior_run = context

        current = PlayerSeason.__table__.alias("current_season")
        prior = PlayerSeason.__table__.alias("prior_season")
        snapshot = FantasyPlayerSnapshot.__table__.alias("prior_snapshot")
        totals = PlayerSeasonStats.__table__.alias("prior_totals")
        club = SeasonClub.__table__.alias("prior_club")

        rows = self._session.execute(
            select(
                current.c.id.label("player_season_id"),
                prior.c.id.label("prior_player_season_id"),
                prior.c.role.label("prior_role"),
                club.c.display_name.label("club_name"),
                snapshot.c.season_score,
                snapshot.c.average_score,
                snapshot.c.rank,
                snapshot.c.price,
                totals.c.points.label("stat_points"),
                totals.c.goals,
                totals.c.assists,
                totals.c.saves,
                totals.c.ball_recoveries,
                totals.c.yellow_cards,
                totals.c.red_cards,
                totals.c.goals_conceded,
                totals.c.field_minutes,
            )
            .select_from(current)
            .join(prior, prior.c.player_id == current.c.player_id)
            .join(club, prior.c.current_season_club_id == club.c.id, isouter=True)
            .join(
                snapshot,
                (snapshot.c.player_season_id == prior.c.id)
                & (snapshot.c.ingestion_run_id == prior_run.id),
                isouter=True,
            )
            .join(
                totals,
                (totals.c.player_season_id == prior.c.id)
                & (totals.c.ingestion_run_id == prior_run.id),
                isouter=True,
            )
            .where(
                current.c.id.in_(ids),
                prior.c.season_id == prior_season.id,
            )
        ).all()
        if not rows:
            return {}

        appearances = self._prior_appearances(
            [row.prior_player_season_id for row in rows], prior_run.id
        )
        return {
            row.player_season_id: {
                "season_id": prior_season.id,
                "season_name": prior_season.name,
                "player_season_id": row.prior_player_season_id,
                "role": row.prior_role,
                "club_name": row.club_name,
                "points": row.season_score,
                "average_points": _num(row.average_score),
                "rank": row.rank,
                "price": _num(row.price),
                "matches": appearances.get(row.prior_player_season_id, 0),
                "minutes": row.field_minutes,
                "goals": row.goals,
                "assists": row.assists,
                "saves": row.saves,
                "ball_recoveries": row.ball_recoveries,
                "yellow_cards": row.yellow_cards,
                "red_cards": row.red_cards,
                "goals_conceded": row.goals_conceded,
            }
            for row in rows
        }

    def _prior_appearances(
        self, prior_player_season_ids: Sequence[int], prior_run_id: int
    ) -> dict[int, int]:
        """Count matches the player actually appeared in last season.

        ``player_season_stats`` aggregates minutes but not the number of games,
        so appearances are counted from the per-match grain; a substitute who
        never came on has a row with zero minutes and is not counted.
        """
        ids = [pid for pid in prior_player_season_ids if pid is not None]
        if not ids:
            return {}
        rows = self._session.execute(
            select(
                PlayerMatchStats.player_season_id,
                func.count().label("matches"),
            )
            .where(
                PlayerMatchStats.player_season_id.in_(ids),
                PlayerMatchStats.ingestion_run_id == prior_run_id,
                PlayerMatchStats.field_minutes > 0,
            )
            .group_by(PlayerMatchStats.player_season_id)
        ).all()
        return {row.player_season_id: row.matches for row in rows}

    # ------------------------------------------------------------------
    # Players.
    # ------------------------------------------------------------------
    def list_players(
        self,
        *,
        season_id: int,
        run_id: int | None,
        tour_id: int | None = None,
        model: str | None = None,
        role: str | None = None,
        club_id: int | None = None,
        status: str | None = None,
        min_price: float | None = None,
        max_price: float | None = None,
        order: str = "name",
        limit: int,
        offset: int,
    ) -> tuple[int, list[dict[str, Any]]]:
        """List players from the active snapshot with optional projections.

        ``run_id`` is the active snapshot run (``None`` when the season has no
        published snapshot, in which case snapshot columns come back null). When
        ``run_id``, ``tour_id`` and ``model`` are all given, the persisted
        forecast for that tour and model is joined in as ``projection``.
        """
        snapshot = FantasyPlayerSnapshot.__table__.alias("snapshot")
        forecast = PlayerForecast.__table__.alias("forecast")
        join_snapshot = run_id is not None
        join_forecast = run_id is not None and tour_id is not None and model is not None

        snapshot_cols = (
            "price",
            "availability_status",
            "status_description",
            "selected_by",
            "form",
            "season_score",
            "average_score",
            "last_tour_score",
            "rank",
        )
        forecast_cols = (
            "expected_points",
            "uncertainty",
            "p_appearance",
            "expected_minutes",
            "components",
            "model_name",
            "model_version",
            "feature_version",
            "scoring_version",
            "stat_source",
            "has_history",
            "match_id",
        )

        columns: list[Any] = [
            PlayerSeason.id.label("player_season_id"),
            PlayerSeason.fantasy_player_id,
            PlayerSeason.role,
            Player.canonical_name.label("player_name"),
            SeasonClub.club_id,
            SeasonClub.display_name.label("club_name"),
        ]
        if join_snapshot:
            columns += [snapshot.c[name] for name in snapshot_cols]
        else:
            columns += [null().label(name) for name in snapshot_cols]
        if join_forecast:
            columns += [forecast.c[name] for name in forecast_cols]
        else:
            columns += [null().label(name) for name in forecast_cols]

        base = (
            select(*columns)
            .select_from(PlayerSeason)
            .join(Player, PlayerSeason.player_id == Player.id)
            .join(
                SeasonClub,
                PlayerSeason.current_season_club_id == SeasonClub.id,
                isouter=True,
            )
        )
        if join_snapshot:
            base = base.join(
                snapshot,
                (snapshot.c.player_season_id == PlayerSeason.id)
                & (snapshot.c.ingestion_run_id == run_id),
                isouter=True,
            )
        if join_forecast:
            base = base.join(
                forecast,
                (forecast.c.player_season_id == PlayerSeason.id)
                & (forecast.c.ingestion_run_id == run_id)
                & (forecast.c.tour_id == tour_id)
                & (forecast.c.model_name == model),
                isouter=True,
            )

        conditions = [PlayerSeason.season_id == season_id]
        if role is not None:
            conditions.append(PlayerSeason.role == role.upper())
        if club_id is not None:
            conditions.append(SeasonClub.club_id == club_id)
        if join_snapshot and status is not None:
            conditions.append(
                func.upper(snapshot.c.availability_status) == status.upper()
            )
        if join_snapshot and min_price is not None:
            conditions.append(snapshot.c.price >= min_price)
        if join_snapshot and max_price is not None:
            conditions.append(snapshot.c.price <= max_price)
        base = base.where(*conditions)

        total = self._session.execute(
            select(func.count()).select_from(base.subquery())
        ).scalar_one()

        base = base.order_by(
            *self._player_order(order, snapshot, forecast, join_snapshot, join_forecast)
        )
        rows = self._session.execute(base.limit(limit).offset(offset)).all()
        prior = self.prior_season_stats(
            season_id, [row.player_season_id for row in rows]
        )
        return total, [
            self._player_row_dict(row, prior.get(row.player_season_id))
            for row in rows
        ]

    @staticmethod
    def _player_order(order, snapshot, forecast, join_snapshot, join_forecast):
        pid = PlayerSeason.id
        if order == "projection" and join_forecast:
            return (forecast.c.expected_points.desc().nullslast(), pid)
        if order == "price" and join_snapshot:
            return (snapshot.c.price.desc().nullslast(), pid)
        if order == "selected_by" and join_snapshot:
            return (snapshot.c.selected_by.desc().nullslast(), pid)
        if order == "season_score" and join_snapshot:
            return (snapshot.c.season_score.desc().nullslast(), pid)
        return (Player.canonical_name, pid)

    @staticmethod
    def _projection_from_row(row: Any) -> dict[str, Any] | None:
        if row.model_name is None:
            return None
        return {
            "model_name": row.model_name,
            "model_version": row.model_version,
            "feature_version": row.feature_version,
            "scoring_version": row.scoring_version,
            "stat_source": row.stat_source,
            "has_history": row.has_history,
            "match_id": row.match_id,
            "expected_points": _num(row.expected_points),
            "uncertainty": _num(row.uncertainty),
            "p_appearance": _num(row.p_appearance),
            "expected_minutes": _num(row.expected_minutes),
            "components": row.components,
        }

    def _player_row_dict(
        self, row: Any, prior_season: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return {
            "player_season_id": row.player_season_id,
            "fantasy_player_id": row.fantasy_player_id,
            "player_name": row.player_name,
            "role": row.role,
            "club_id": row.club_id,
            "club_name": row.club_name,
            "price": _num(row.price),
            "availability_status": row.availability_status,
            "status_description": row.status_description,
            "selected_by": _num(row.selected_by),
            "form": row.form,
            "season_score": row.season_score,
            "average_score": _num(row.average_score),
            "last_tour_score": row.last_tour_score,
            "rank": row.rank,
            "projection": self._projection_from_row(row),
            "prior_season": prior_season,
        }

    def get_player(
        self,
        *,
        player_season_id: int,
        run_id: int | None,
        tour_id: int | None = None,
        model: str | None = None,
        history_limit: int = 50,
    ) -> dict[str, Any] | None:
        """Return one player's card: identity, snapshot, projection and history."""
        row = self._session.execute(
            select(
                PlayerSeason.id.label("player_season_id"),
                PlayerSeason.fantasy_player_id,
                PlayerSeason.role,
                PlayerSeason.season_id,
                Player.canonical_name.label("player_name"),
                SeasonClub.club_id,
                SeasonClub.display_name.label("club_name"),
            )
            .select_from(PlayerSeason)
            .join(Player, PlayerSeason.player_id == Player.id)
            .join(
                SeasonClub,
                PlayerSeason.current_season_club_id == SeasonClub.id,
                isouter=True,
            )
            .where(PlayerSeason.id == player_season_id)
        ).one_or_none()
        if row is None:
            return None

        payload: dict[str, Any] = {
            "player_season_id": row.player_season_id,
            "fantasy_player_id": row.fantasy_player_id,
            "player_name": row.player_name,
            "role": row.role,
            "season_id": row.season_id,
            "club_id": row.club_id,
            "club_name": row.club_name,
        }
        payload.update(self._player_snapshot(player_season_id, run_id))
        payload["projection"] = self._player_projection(
            player_season_id, run_id, tour_id, model
        )
        payload["prior_season"] = self.prior_season_stats(
            row.season_id, [player_season_id]
        ).get(player_season_id)
        payload["history"] = self._player_history(
            player_season_id, run_id, history_limit
        )
        return payload

    def _player_snapshot(
        self, player_season_id: int, run_id: int | None
    ) -> dict[str, Any]:
        if run_id is None:
            return {
                "price": None,
                "availability_status": None,
                "status_description": None,
                "selected_by": None,
                "form": None,
                "season_score": None,
                "average_score": None,
                "last_tour_score": None,
                "rank": None,
            }
        snap = self._session.execute(
            select(FantasyPlayerSnapshot).where(
                FantasyPlayerSnapshot.player_season_id == player_season_id,
                FantasyPlayerSnapshot.ingestion_run_id == run_id,
            )
        ).scalar_one_or_none()
        if snap is None:
            return {
                "price": None,
                "availability_status": None,
                "status_description": None,
                "selected_by": None,
                "form": None,
                "season_score": None,
                "average_score": None,
                "last_tour_score": None,
                "rank": None,
            }
        return {
            "price": _num(snap.price),
            "availability_status": snap.availability_status,
            "status_description": snap.status_description,
            "selected_by": _num(snap.selected_by),
            "form": snap.form,
            "season_score": snap.season_score,
            "average_score": _num(snap.average_score),
            "last_tour_score": snap.last_tour_score,
            "rank": snap.rank,
        }

    def _player_projection(
        self,
        player_season_id: int,
        run_id: int | None,
        tour_id: int | None,
        model: str | None,
    ) -> dict[str, Any] | None:
        if run_id is None or tour_id is None or model is None:
            return None
        row = self._session.execute(
            select(PlayerForecast).where(
                PlayerForecast.player_season_id == player_season_id,
                PlayerForecast.ingestion_run_id == run_id,
                PlayerForecast.tour_id == tour_id,
                PlayerForecast.model_name == model,
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "model_name": row.model_name,
            "model_version": row.model_version,
            "feature_version": row.feature_version,
            "scoring_version": row.scoring_version,
            "stat_source": row.stat_source,
            "has_history": row.has_history,
            "match_id": row.match_id,
            "expected_points": _num(row.expected_points),
            "uncertainty": _num(row.uncertainty),
            "p_appearance": _num(row.p_appearance),
            "expected_minutes": _num(row.expected_minutes),
            "components": row.components,
        }

    def _player_history(
        self, player_season_id: int, run_id: int | None, history_limit: int
    ) -> list[dict[str, Any]]:
        if run_id is None:
            return []
        rows = self._session.execute(
            select(
                PlayerMatchStats.match_id,
                Match.scheduled_at,
                PlayerMatchStats.tour_id,
                PlayerMatchStats.points,
                PlayerMatchStats.goals,
                PlayerMatchStats.assists,
                PlayerMatchStats.saves,
                PlayerMatchStats.ball_recoveries,
                PlayerMatchStats.yellow_cards,
                PlayerMatchStats.red_cards,
                PlayerMatchStats.goals_conceded,
                PlayerMatchStats.field_minutes,
            )
            .join(Match, PlayerMatchStats.match_id == Match.id)
            .where(
                PlayerMatchStats.player_season_id == player_season_id,
                PlayerMatchStats.ingestion_run_id == run_id,
            )
            .order_by(Match.scheduled_at.desc(), PlayerMatchStats.match_id.desc())
            .limit(history_limit)
        ).all()
        return [
            {
                "match_id": r.match_id,
                "tour_id": r.tour_id,
                "scheduled_at": _iso(r.scheduled_at),
                "minutes": r.field_minutes,
                "points": r.points,
                "goals": r.goals,
                "assists": r.assists,
                "saves": r.saves,
                "ball_recoveries": r.ball_recoveries,
                "yellow_cards": r.yellow_cards,
                "red_cards": r.red_cards,
                "goals_conceded": r.goals_conceded,
            }
            for r in rows
        ]


__all__ = ["ReadRepository", "ROLES", "PLAYER_ORDERS"]
