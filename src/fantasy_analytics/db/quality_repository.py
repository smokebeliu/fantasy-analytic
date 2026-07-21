"""Repository layer for the data-quality gate (development-plan step 4).

It owns three concerns, all inside the caller's transaction:

* resolving which ingestion run to evaluate and the season its snapshot
  describes;
* replacing the recorded ``data_quality_issues`` for a run idempotently;
* publishing a snapshot by toggling ``ingestion_runs.is_active`` so that an
  invalid snapshot never becomes the active one and at most one run per season
  is active at a time.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from .models import (
    ClubSeasonStats,
    DataQualityIssue,
    IngestionRun,
    PlayerSeason,
    PlayerSeasonStats,
    Season,
    SeasonClub,
)


class QualityRepository:
    """Transactional access for quality issues and snapshot activation.

    The repository never commits on its own; the caller owns the transaction
    boundary (typically :func:`fantasy_analytics.db.session_scope`).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest_succeeded_run(self) -> IngestionRun | None:
        """Return the most recent run that finished successfully."""
        return self._session.execute(
            select(IngestionRun)
            .where(IngestionRun.status == "succeeded")
            .order_by(IngestionRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def get_run(self, run_id: int) -> IngestionRun | None:
        return self._session.get(IngestionRun, run_id)

    def resolve_season_id(self, run: IngestionRun) -> int | None:
        """Resolve the season a run's snapshot belongs to.

        Snapshots are keyed by ``ingestion_run_id``; the club and player season
        aggregates carry a season reference. Fall back to the season recorded in
        the run report when a run produced no aggregate rows.
        """
        season_id = self._session.execute(
            select(SeasonClub.season_id)
            .join(ClubSeasonStats, ClubSeasonStats.season_club_id == SeasonClub.id)
            .where(ClubSeasonStats.ingestion_run_id == run.id)
            .limit(1)
        ).scalar_one_or_none()
        if season_id is not None:
            return int(season_id)

        season_id = self._session.execute(
            select(PlayerSeason.season_id)
            .join(
                PlayerSeasonStats,
                PlayerSeasonStats.player_season_id == PlayerSeason.id,
            )
            .where(PlayerSeasonStats.ingestion_run_id == run.id)
            .limit(1)
        ).scalar_one_or_none()
        if season_id is not None:
            return int(season_id)

        report = run.report or {}
        fantasy_id = ((report.get("season") or {}).get("fantasy_id")) if report else None
        if fantasy_id:
            return self._session.execute(
                select(Season.id).where(
                    Season.fantasy_season_id == str(fantasy_id)
                )
            ).scalar_one_or_none()
        return None

    def replace_issues(
        self,
        run: IngestionRun,
        season_id: int | None,
        issues: Sequence[dict[str, Any]],
    ) -> None:
        """Delete previous issues for a run and insert the fresh set."""
        self._session.execute(
            delete(DataQualityIssue).where(
                DataQualityIssue.ingestion_run_id == run.id
            )
        )
        self._session.flush()
        if not issues:
            return
        rows = [
            {
                "ingestion_run_id": run.id,
                "season_id": season_id,
                **issue,
            }
            for issue in issues
        ]
        self._session.execute(DataQualityIssue.__table__.insert(), rows)
        self._session.flush()

    def publish(
        self,
        run: IngestionRun,
        season_id: int | None,
        *,
        active: bool,
        checked_at: datetime | None = None,
    ) -> IngestionRun:
        """Record the quality outcome and toggle the active snapshot.

        When ``active`` is true the run becomes the season's single active
        snapshot and any previously active run for that season is deactivated.
        When false the run is left inactive, so a snapshot with blocking issues
        never supersedes the last valid one.
        """
        run.season_id = season_id
        run.quality_checked_at = checked_at or datetime.now(UTC)
        if active:
            if season_id is not None:
                self._session.execute(
                    update(IngestionRun)
                    .where(
                        IngestionRun.season_id == season_id,
                        IngestionRun.id != run.id,
                        IngestionRun.is_active.is_(True),
                    )
                    .values(is_active=False)
                )
                self._session.flush()
            run.is_active = True
        else:
            run.is_active = False
        self._session.flush()
        return run

    def count_issues(self, run: IngestionRun, severity: str | None = None) -> int:
        query = select(DataQualityIssue).where(
            DataQualityIssue.ingestion_run_id == run.id
        )
        if severity is not None:
            query = query.where(DataQualityIssue.severity == severity)
        return len(self._session.execute(query).scalars().all())


__all__ = ["QualityRepository"]
