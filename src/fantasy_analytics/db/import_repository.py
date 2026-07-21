"""Idempotent upsert layer for the full historical import (development-plan step 2).

The methods here translate normalized Sports.ru payloads into the domain tables
defined in :mod:`fantasy_analytics.db.models`. Catalog entities (competitions,
seasons, clubs, tours, matches, players, per-match stats) are upserted by their
external identifiers so a repeated import never changes the count of logical
rows. Run-scoped snapshots (fantasy prices, season aggregates, club season
aggregates) are inserted fresh for every ingestion run because they are a
time-series keyed by ``ingestion_run_id``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import models

_CHUNK_SIZE = 500


def _chunks(rows: Sequence[dict[str, Any]], size: int = _CHUNK_SIZE):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _dedupe(rows: Iterable[dict[str, Any]], key_columns: Sequence[str]):
    """Keep the last row per conflict key so a single INSERT never hits the
    same conflict target twice (PostgreSQL forbids that in ON CONFLICT)."""
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        unique[tuple(row[column] for column in key_columns)] = row
    return list(unique.values())


class DomainImportRepository:
    """Transactional idempotent writes for the normalized domain model.

    The repository never commits; the caller owns the transaction boundary
    (typically :func:`fantasy_analytics.db.session_scope`), which guarantees a
    failed import leaves no partially published snapshot.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def _bulk_upsert(
        self,
        model: type[models.Base],
        rows: Sequence[dict[str, Any]],
        *,
        conflict: Sequence[str],
        update: Sequence[str],
        returning: Sequence[str],
    ) -> dict[tuple[Any, ...], int]:
        """Upsert ``rows`` and map each conflict/returning key tuple to its id.

        ``update`` must be non-empty so ``RETURNING`` also yields the primary
        key of rows that already existed (a pure ``DO NOTHING`` would skip them).
        """
        result: dict[tuple[Any, ...], int] = {}
        if not rows:
            return result
        deduped = _dedupe(rows, conflict)
        returning_columns = [model.id, *(getattr(model, name) for name in returning)]
        for chunk in _chunks(deduped):
            statement = pg_insert(model).values(chunk)
            statement = statement.on_conflict_do_update(
                index_elements=list(conflict),
                set_={name: getattr(statement.excluded, name) for name in update},
            ).returning(*returning_columns)
            for record in self._session.execute(statement):
                key = tuple(getattr(record, name) for name in returning)
                result[key] = record.id
        return result

    def _bulk_insert(
        self, model: type[models.Base], rows: Sequence[dict[str, Any]]
    ) -> None:
        for chunk in _chunks(rows):
            self._session.execute(pg_insert(model), list(chunk))

    def upsert_competition(
        self, *, fantasy_tournament_id: str, slug: str, name: str
    ) -> int:
        mapping = self._bulk_upsert(
            models.Competition,
            [
                {
                    "fantasy_tournament_id": fantasy_tournament_id,
                    "slug": slug,
                    "name": name,
                }
            ],
            conflict=["fantasy_tournament_id"],
            update=["slug", "name"],
            returning=["fantasy_tournament_id"],
        )
        return mapping[(fantasy_tournament_id,)]

    def upsert_season(
        self,
        *,
        competition_id: int,
        fantasy_season_id: str,
        stat_season_id: str,
        name: str,
        is_active: bool,
        starts_at: Any = None,
        ends_at: Any = None,
    ) -> int:
        mapping = self._bulk_upsert(
            models.Season,
            [
                {
                    "competition_id": competition_id,
                    "fantasy_season_id": fantasy_season_id,
                    "stat_season_id": stat_season_id,
                    "name": name,
                    "is_active": is_active,
                    "starts_at": starts_at,
                    "ends_at": ends_at,
                }
            ],
            conflict=["fantasy_season_id"],
            update=[
                "competition_id",
                "stat_season_id",
                "name",
                "is_active",
                "starts_at",
                "ends_at",
            ],
            returning=["fantasy_season_id"],
        )
        return mapping[(fantasy_season_id,)]

    def upsert_season_rules(self, *, season_id: int, **columns: Any) -> None:
        statement = pg_insert(models.SeasonRules).values(season_id=season_id, **columns)
        statement = statement.on_conflict_do_update(
            index_elements=["season_id"],
            set_={name: getattr(statement.excluded, name) for name in columns},
        )
        self._session.execute(statement)

    def upsert_clubs(self, clubs: Sequence[dict[str, Any]]) -> dict[str, int]:
        """Upsert clubs keyed by ``stat_team_id``; returns stat_team_id -> id."""
        mapping = self._bulk_upsert(
            models.Club,
            clubs,
            conflict=["stat_team_id"],
            update=["canonical_name"],
            returning=["stat_team_id"],
        )
        return {key[0]: value for key, value in mapping.items()}

    def upsert_season_clubs(
        self, season_clubs: Sequence[dict[str, Any]]
    ) -> dict[str, int]:
        """Upsert season clubs; returns fantasy_team_id -> season_club id."""
        mapping = self._bulk_upsert(
            models.SeasonClub,
            season_clubs,
            conflict=["season_id", "club_id"],
            update=["fantasy_team_id", "display_name"],
            returning=["fantasy_team_id"],
        )
        return {key[0]: value for key, value in mapping.items()}

    def upsert_tours(self, tours: Sequence[dict[str, Any]]) -> dict[str, int]:
        """Upsert tours; returns fantasy_tour_id -> id."""
        mapping = self._bulk_upsert(
            models.FantasyTour,
            tours,
            conflict=["season_id", "fantasy_tour_id"],
            update=[
                "name",
                "status",
                "starts_at",
                "finishes_at",
                "transfers_start_at",
                "transfers_deadline_at",
                "total_transfers",
                "max_same_team_players",
            ],
            returning=["fantasy_tour_id"],
        )
        return {key[0]: value for key, value in mapping.items()}

    def upsert_matches(self, matches: Sequence[dict[str, Any]]) -> dict[str, int]:
        """Upsert matches keyed by ``stat_match_id``; returns stat_match_id -> id."""
        mapping = self._bulk_upsert(
            models.Match,
            matches,
            conflict=["stat_match_id"],
            update=[
                "season_id",
                "tour_id",
                "scheduled_at",
                "home_club_id",
                "away_club_id",
                "home_score",
                "away_score",
            ],
            returning=["stat_match_id"],
        )
        return {key[0]: value for key, value in mapping.items()}

    def upsert_club_match_stats(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        deduped = _dedupe(rows, ["match_id", "club_id"])
        for chunk in _chunks(deduped):
            statement = pg_insert(models.ClubMatchStats).values(chunk)
            statement = statement.on_conflict_do_update(
                index_elements=["match_id", "club_id"],
                set_={
                    name: getattr(statement.excluded, name)
                    for name in (
                        "opponent_club_id",
                        "is_home",
                        "goals_scored",
                        "goals_conceded",
                        "provider_metrics",
                        "ingestion_run_id",
                    )
                },
            )
            self._session.execute(statement)

    def existing_player_ids(self, season_id: int) -> dict[str, int]:
        """Return fantasy_player_id -> players.id already linked to the season."""
        rows = self._session.execute(
            select(
                models.PlayerSeason.fantasy_player_id, models.PlayerSeason.player_id
            ).where(models.PlayerSeason.season_id == season_id)
        )
        return {fantasy_id: player_id for fantasy_id, player_id in rows}

    def upsert_players_by_stat(
        self, players: Sequence[dict[str, Any]]
    ) -> dict[str, int]:
        """Upsert players keyed by ``stat_player_id``; returns stat_id -> id."""
        mapping = self._bulk_upsert(
            models.Player,
            players,
            conflict=["stat_player_id"],
            update=["canonical_name"],
            returning=["stat_player_id"],
        )
        return {key[0]: value for key, value in mapping.items()}

    def create_player(self, *, canonical_name: str) -> int:
        """Insert a player that has no cross-season stat identity."""
        player = models.Player(canonical_name=canonical_name, stat_player_id=None)
        self._session.add(player)
        self._session.flush()
        return player.id

    def upsert_player_seasons(
        self, player_seasons: Sequence[dict[str, Any]]
    ) -> dict[str, int]:
        """Upsert player seasons; returns fantasy_player_id -> player_season id."""
        mapping = self._bulk_upsert(
            models.PlayerSeason,
            player_seasons,
            conflict=["season_id", "fantasy_player_id"],
            update=["player_id", "role", "current_season_club_id"],
            returning=["fantasy_player_id"],
        )
        return {key[0]: value for key, value in mapping.items()}

    def insert_player_snapshots(self, rows: Sequence[dict[str, Any]]) -> None:
        self._bulk_insert(models.FantasyPlayerSnapshot, rows)

    def insert_player_season_stats(self, rows: Sequence[dict[str, Any]]) -> None:
        self._bulk_insert(models.PlayerSeasonStats, rows)

    def insert_club_season_stats(self, rows: Sequence[dict[str, Any]]) -> None:
        self._bulk_insert(models.ClubSeasonStats, rows)

    def upsert_player_match_stats(
        self, rows: Sequence[dict[str, Any]]
    ) -> dict[tuple[int, int], int]:
        """Upsert per-match player stats; returns (player_season_id, match_id) -> id."""
        return self._bulk_upsert(
            models.PlayerMatchStats,
            rows,
            conflict=["player_season_id", "match_id"],
            update=[
                "tour_id",
                "season_club_id",
                "ingestion_run_id",
                "points",
                "goals",
                "assists",
                "saves",
                "penalties_missed",
                "penalties_post",
                "penalties_target",
                "penalties_saved",
                "field_minutes",
                "yellow_cards",
                "red_cards",
                "goals_conceded",
                "penalty_goals_conceded",
                "penalties_faced",
                "penalty_conceded",
                "own_goals",
                "ball_recoveries",
            ],
            returning=["player_season_id", "match_id"],
        )

    def upsert_point_details(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        deduped = _dedupe(rows, ["player_match_stat_id", "ordinal"])
        for chunk in _chunks(deduped):
            statement = pg_insert(models.FantasyPointDetail).values(chunk)
            statement = statement.on_conflict_do_update(
                index_elements=["player_match_stat_id", "ordinal"],
                set_={
                    name: getattr(statement.excluded, name)
                    for name in ("reason", "score")
                },
            )
            self._session.execute(statement)


__all__ = ["DomainImportRepository"]
