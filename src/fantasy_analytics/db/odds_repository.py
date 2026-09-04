"""Persist 1x2 match odds for the next-tour forecast."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from . import models


class OddsRepository:
    """Transactional upserts for ``match_odds``.

    Never commits; the caller owns the transaction boundary.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, rows: Sequence[dict[str, Any]]) -> int:
        """Insert or replace odds keyed by ``stat_match_id``.

        A later refresh of the same fixture overwrites the line, implied
        probabilities and expected goals; identity columns (season, match,
        tour) are updated so a match that was unmatched on the first fetch
        can be linked once the season import lands.
        """
        if not rows:
            return 0
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            unique[str(row["stat_match_id"])] = row
        payload = list(unique.values())
        statement = pg_insert(models.MatchOdds).values(payload)
        update_cols = [
            "season_id",
            "match_id",
            "tour_id",
            "home_stat_team_id",
            "away_stat_team_id",
            "bookmaker",
            "home_odds",
            "draw_odds",
            "away_odds",
            "implied_home",
            "implied_draw",
            "implied_away",
            "expected_home_goals",
            "expected_away_goals",
            "captured_at",
            "raw",
        ]
        statement = statement.on_conflict_do_update(
            index_elements=["stat_match_id"],
            set_={name: getattr(statement.excluded, name) for name in update_cols},
        )
        self._session.execute(statement)
        self._session.flush()
        self._append_history(payload)
        return len(payload)

    def _append_history(self, rows: Sequence[dict[str, Any]]) -> None:
        """Record every capture in ``match_odds_history`` (step 23).

        The history is what lets a backtest replay a tour with the line that
        was known at its cutoff. A capture that repeats an earlier one to the
        second is ignored, so re-running a refresh does not duplicate rows.
        """
        history_cols = {
            "season_id",
            "match_id",
            "tour_id",
            "stat_match_id",
            "bookmaker",
            "home_odds",
            "draw_odds",
            "away_odds",
            "implied_home",
            "implied_draw",
            "implied_away",
            "expected_home_goals",
            "expected_away_goals",
            "captured_at",
            "raw",
        }
        payload = [
            {key: value for key, value in row.items() if key in history_cols}
            for row in rows
        ]
        statement = pg_insert(models.MatchOddsHistory).values(payload)
        statement = statement.on_conflict_do_nothing(
            index_elements=["stat_match_id", "captured_at"]
        )
        self._session.execute(statement)
        self._session.flush()

    def history_before(
        self, season_id: int, cutoff: datetime
    ) -> dict[int, models.MatchOddsHistory]:
        """The latest capture per match that was known before ``cutoff``."""
        rows = self._session.execute(
            select(models.MatchOddsHistory)
            .where(
                models.MatchOddsHistory.season_id == season_id,
                models.MatchOddsHistory.match_id.is_not(None),
                models.MatchOddsHistory.captured_at < cutoff,
            )
            .order_by(models.MatchOddsHistory.captured_at)
        ).scalars()
        latest: dict[int, models.MatchOddsHistory] = {}
        for row in rows:
            latest[int(row.match_id)] = row
        return latest

    def list_for_season(self, season_id: int) -> list[models.MatchOdds]:
        return list(
            self._session.execute(
                select(models.MatchOdds)
                .where(models.MatchOdds.season_id == season_id)
                .order_by(models.MatchOdds.stat_match_id)
            ).scalars()
        )

    def count_for_season(self, season_id: int) -> int:
        from sqlalchemy import func

        return int(
            self._session.execute(
                select(func.count())
                .select_from(models.MatchOdds)
                .where(models.MatchOdds.season_id == season_id)
            ).scalar_one()
        )

    def latest_captured_at(self, season_id: int) -> datetime | None:
        from sqlalchemy import func

        return self._session.execute(
            select(func.max(models.MatchOdds.captured_at)).where(
                models.MatchOdds.season_id == season_id
            )
        ).scalar_one_or_none()
