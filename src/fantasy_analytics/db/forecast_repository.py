"""Repository layer for player forecasts (development-plan step 7).

It persists the forecast rows produced by
:func:`fantasy_analytics.forecast.build_forecast_dataset` into the
``player_forecasts`` table. Writes are idempotent per
``(ingestion_run_id, tour_id, model_name, model_version)``: the previous set for
that combination is deleted and replaced, so recomputing a model on the same
snapshot never accumulates duplicates.

The repository never commits on its own; the caller owns the transaction
boundary (typically :func:`fantasy_analytics.db.session_scope`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from .models import PlayerForecast


class ForecastRepository:
    """Transactional access to persisted player forecasts."""

    def __init__(self, session) -> None:
        self._session = session

    def replace_forecasts(
        self,
        *,
        run_id: int,
        season_id: int | None,
        tour_id: int,
        cutoff: datetime,
        rows: Sequence[dict[str, Any]],
    ) -> int:
        """Replace all forecasts for a run/tour with the supplied rows.

        Every ``(model_name, model_version)`` present in ``rows`` is cleared for
        the run and tour before the new rows are inserted, so a partial re-run of
        one model does not disturb another model's stored forecasts.
        """
        model_keys = {(row["model_name"], row["model_version"]) for row in rows}
        for model_name, model_version in model_keys:
            self._session.execute(
                delete(PlayerForecast).where(
                    PlayerForecast.ingestion_run_id == run_id,
                    PlayerForecast.tour_id == tour_id,
                    PlayerForecast.model_name == model_name,
                    PlayerForecast.model_version == model_version,
                )
            )
        self._session.flush()

        if not rows:
            return 0

        payload = [
            {
                "ingestion_run_id": run_id,
                "season_id": season_id,
                "tour_id": tour_id,
                "match_id": row["match_id"],
                "player_season_id": row["player_season_id"],
                "model_name": row["model_name"],
                "model_version": row["model_version"],
                "feature_version": row["feature_version"],
                "scoring_version": row.get("scoring_version"),
                "stat_source": row.get("stat_source"),
                "has_history": row.get("has_history"),
                "cutoff": cutoff,
                "expected_points": row["expected_points"],
                "uncertainty": row.get("uncertainty"),
                "p_appearance": row.get("p_appearance"),
                "expected_minutes": row.get("expected_minutes"),
                "components": row.get("components"),
                "params": row.get("params"),
            }
            for row in rows
        ]
        self._session.execute(PlayerForecast.__table__.insert(), payload)
        self._session.flush()
        return len(payload)

    def list_forecasts(
        self,
        *,
        run_id: int,
        tour_id: int,
        model_name: str | None = None,
    ) -> list[PlayerForecast]:
        query = select(PlayerForecast).where(
            PlayerForecast.ingestion_run_id == run_id,
            PlayerForecast.tour_id == tour_id,
        )
        if model_name is not None:
            query = query.where(PlayerForecast.model_name == model_name)
        query = query.order_by(PlayerForecast.expected_points.desc())
        return list(self._session.execute(query).scalars())

    def has_forecasts(self, *, run_id: int, tour_id: int) -> bool:
        """Whether the run/tour pair has any stored forecast at all.

        Cheaper than :meth:`count_forecasts` because it stops at the first row;
        callers that only need to know "was this tour ever forecast" use it on
        the read path.
        """
        return (
            self._session.execute(
                select(PlayerForecast.id)
                .where(
                    PlayerForecast.ingestion_run_id == run_id,
                    PlayerForecast.tour_id == tour_id,
                )
                .limit(1)
            ).first()
            is not None
        )

    def count_forecasts(self, *, run_id: int, tour_id: int) -> int:
        return len(
            self._session.execute(
                select(PlayerForecast.id).where(
                    PlayerForecast.ingestion_run_id == run_id,
                    PlayerForecast.tour_id == tour_id,
                )
            )
            .scalars()
            .all()
        )


__all__ = ["ForecastRepository"]
