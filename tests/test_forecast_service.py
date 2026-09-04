"""Tests for automatic forecast materialisation.

Before this module existed, the forecast step (step 7) was only ever run by hand:
a published snapshot carried prices and season scores but no projections, so the
player table's «Прогноз» column was empty until somebody remembered to run
``fantasy-forecast``. These tests pin the two places that now close the gap — the
ingestion worker after the quality gate, and the player read endpoints on first
access — plus the guarantees the materialiser makes: it is idempotent, it refuses
tours that do not belong to the run, and it never turns a failed build into a
failed request.

The integration tests import a small synthetic season into a real PostgreSQL
database and are skipped automatically when no database is reachable.
"""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from fantasy_analytics.api import create_app
from fantasy_analytics.db import (
    ForecastRepository,
    create_db_engine,
    create_session_factory,
    migration,
    session_scope,
)
from fantasy_analytics.db.models import FantasyTour, Season
from fantasy_analytics.forecast_service import (
    ensure_next_tour_forecasts,
    ensure_tour_forecasts,
    forecast_lock_key,
    has_tour_forecasts,
)
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.quality import run_quality_checks

from test_ingestion import FakeClient
from test_quality import _consistent_fixture

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)


def _database_available() -> bool:
    if not TEST_DATABASE_URL:
        return False
    try:
        engine = create_db_engine(TEST_DATABASE_URL)
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


DATABASE_AVAILABLE = _database_available()
requires_database = unittest.skipUnless(
    DATABASE_AVAILABLE,
    "PostgreSQL is not reachable via TEST_DATABASE_URL/DATABASE_URL",
)


class ForecastLockKeyTest(unittest.TestCase):
    def test_key_is_stable_for_the_same_pair(self) -> None:
        self.assertEqual(forecast_lock_key(7, 42), forecast_lock_key(7, 42))

    def test_different_pairs_get_different_keys(self) -> None:
        self.assertNotEqual(forecast_lock_key(7, 42), forecast_lock_key(7, 43))
        self.assertNotEqual(forecast_lock_key(7, 42), forecast_lock_key(8, 42))

    def test_key_fits_a_postgres_bigint(self) -> None:
        key = forecast_lock_key(123456, 654321)
        self.assertGreaterEqual(key, -(2**63))
        self.assertLess(key, 2**63)


@requires_database
class EnsureForecastsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)

        report = run_ingestion(
            FakeClient(_consistent_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        self.run_id = report["run_id"]
        run_quality_checks(self.session_factory, run_id=self.run_id)
        with session_scope(self.session_factory) as session:
            self.season_id = session.execute(select(Season.id)).scalar_one()
            self.tour_id = session.execute(select(FantasyTour.id)).scalars().first()

        # The import leaves nothing to project from: that is the bug being fixed.
        self.assertFalse(
            has_tour_forecasts(
                self.session_factory, run_id=self.run_id, tour_id=self.tour_id
            )
        )

    def _stored(self) -> int:
        with session_scope(self.session_factory) as session:
            return ForecastRepository(session).count_forecasts(
                run_id=self.run_id, tour_id=self.tour_id
            )

    def test_missing_forecasts_are_materialised(self) -> None:
        written = ensure_tour_forecasts(
            self.session_factory, run_id=self.run_id, tour_id=self.tour_id
        )
        self.assertGreater(written, 0)
        self.assertEqual(written, self._stored())

    def test_second_call_is_a_no_op(self) -> None:
        ensure_tour_forecasts(
            self.session_factory, run_id=self.run_id, tour_id=self.tour_id
        )
        before = self._stored()
        self.assertEqual(
            0,
            ensure_tour_forecasts(
                self.session_factory, run_id=self.run_id, tour_id=self.tour_id
            ),
        )
        self.assertEqual(before, self._stored())

    def test_missing_arguments_do_nothing(self) -> None:
        self.assertEqual(
            0, ensure_tour_forecasts(self.session_factory, run_id=None, tour_id=None)
        )
        self.assertEqual(
            0,
            ensure_tour_forecasts(
                self.session_factory, run_id=self.run_id, tour_id=None
            ),
        )
        self.assertEqual(0, self._stored())

    def test_a_tour_from_another_season_is_refused(self) -> None:
        # Forecasting a tour the run knows nothing about would attach numbers to
        # the wrong season rather than fail loudly, so it is rejected outright.
        with self.engine.begin() as connection:
            other_tour = connection.execute(
                text(
                    """
                    INSERT INTO seasons
                        (competition_id, fantasy_season_id, stat_season_id, name,
                         is_active, starts_at)
                    VALUES ((SELECT competition_id FROM seasons LIMIT 1),
                            '99', 'rfpl_99-00', '2099/2100', false,
                            '2099-07-01T00:00:00Z')
                    RETURNING id
                    """
                )
            ).scalar_one()
            tour_id = connection.execute(
                text(
                    """
                    INSERT INTO fantasy_tours
                        (season_id, fantasy_tour_id, name, status, starts_at)
                    VALUES (:s, '9901', '1 тур', 'NOT_STARTED',
                            '2099-08-01T00:00:00Z')
                    RETURNING id
                    """
                ),
                {"s": other_tour},
            ).scalar_one()

        self.assertEqual(
            0,
            ensure_tour_forecasts(
                self.session_factory, run_id=self.run_id, tour_id=tour_id
            ),
        )

    def test_a_failed_build_is_swallowed(self) -> None:
        # A tour with no fixture cannot be forecast. Reporting that as an
        # exception would turn a missing projection into a broken player list.
        with self.engine.begin() as connection:
            tour_id = connection.execute(
                text(
                    """
                    INSERT INTO fantasy_tours
                        (season_id, fantasy_tour_id, name, status, starts_at)
                    VALUES (:s, '9902', '99 тур', 'NOT_STARTED', NULL)
                    RETURNING id
                    """
                ),
                {"s": self.season_id},
            ).scalar_one()

        messages: list[str] = []
        self.assertEqual(
            0,
            ensure_tour_forecasts(
                self.session_factory,
                run_id=self.run_id,
                tour_id=tour_id,
                on_progress=messages.append,
            ),
        )
        self.assertTrue(any("could not be built" in note for note in messages))

    def test_next_tour_is_chosen_when_none_is_given(self) -> None:
        # The fixture's only tour is finished, so there is nothing to project.
        self.assertEqual(
            0, ensure_next_tour_forecasts(self.session_factory, run_id=self.run_id)
        )

        with self.engine.begin() as connection:
            upcoming = connection.execute(
                text(
                    """
                    INSERT INTO fantasy_tours
                        (season_id, fantasy_tour_id, name, status, starts_at,
                         transfers_deadline_at, total_transfers,
                         max_same_team_players)
                    VALUES (:s, '1802', '2 тур', 'OPENED',
                            '2026-08-20T16:00:00Z', '2026-08-19T10:00:00Z', 3, 3)
                    RETURNING id
                    """
                ),
                {"s": self.season_id},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO matches
                        (season_id, tour_id, stat_match_id, scheduled_at,
                         home_club_id, away_club_id)
                    VALUES (:s, :t, '950777', '2026-08-20T16:00:00Z',
                            (SELECT club_id FROM season_clubs WHERE season_id = :s
                             ORDER BY id LIMIT 1),
                            (SELECT club_id FROM season_clubs WHERE season_id = :s
                             ORDER BY id DESC LIMIT 1))
                    """
                ),
                {"s": self.season_id, "t": upcoming},
            )

        self.assertGreater(
            ensure_next_tour_forecasts(self.session_factory, run_id=self.run_id), 0
        )
        self.assertTrue(
            has_tour_forecasts(
                self.session_factory, run_id=self.run_id, tour_id=upcoming
            )
        )

    def test_player_list_fills_the_projection_on_first_read(self) -> None:
        # The whole point: a snapshot published without forecasts must not serve
        # an empty forecast column.
        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
            )
        )
        response = client.get(
            f"/players?season_id={self.season_id}&tour_id={self.tour_id}"
            "&model=poisson_events"
        )
        self.assertEqual(200, response.status_code)
        items = response.json()["items"]
        self.assertTrue(items)
        self.assertTrue(any(item["projection"] is not None for item in items))
        self.assertGreater(self._stored(), 0)

    def test_player_list_without_a_tour_stays_untouched(self) -> None:
        # No tour means no projection was asked for, so nothing should be built.
        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
            )
        )
        response = client.get(f"/players?season_id={self.season_id}")
        self.assertEqual(200, response.status_code)
        self.assertEqual(0, self._stored())


if __name__ == "__main__":
    unittest.main()
