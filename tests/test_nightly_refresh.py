"""Unit and integration tests for the nightly refresh of imported active seasons.

The unit tests pin the clock-math (when the loop sleeps, when it catches up
after a late deploy) and the environment-variable parsing. The integration
tests drive a real PostgreSQL database: only a league whose imported season is
``is_active`` is eligible, a sweep enqueues ``trigger_type=scheduled`` jobs
with ``use_current_season``, a second sweep the same local day is a no-op, and
a league that already has a pending refresh is skipped rather than conflicting.
They are skipped automatically when no database is reachable.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import text

from fantasy_analytics.api import create_app
from fantasy_analytics.competitions import normalize_catalogue, persist_catalogue
from fantasy_analytics.db import (
    IngestionJobRepository,
    create_db_engine,
    create_session_factory,
    migration,
    session_scope,
)
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.nightly_refresh import (
    DEFAULT_TIMEZONE,
    SCHEDULED_TRIGGER,
    SKIP_ALREADY_RAN_TODAY,
    SKIP_ALREADY_RUNNING,
    EligibleLeague,
    NightlyRefreshSettings,
    enqueue_nightly_jobs,
    list_imported_active_leagues,
    next_run_at,
    resolve_timezone,
    run_nightly_loop,
    scheduler_status,
    seconds_until_next_run,
    start_of_local_day,
)
from fantasy_analytics.quality import run_quality_checks

from test_competitions import _catalogue_payload
from test_ingestion import FakeClient
from test_quality import _consistent_fixture

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)

MOSCOW = timezone(timedelta(hours=3))


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


def _at(hour: int, minute: int = 0) -> datetime:
    """A fixed Moscow wall-clock time on 2026-08-18, returned as UTC."""
    return datetime(2026, 8, 18, hour, minute, tzinfo=MOSCOW)


class TimezoneResolutionTest(unittest.TestCase):
    def test_known_zone_or_moscow_offset(self) -> None:
        # Slim images may lack tzdata; either result is UTC+3 and that is enough.
        zone = resolve_timezone("Europe/Moscow")
        noon = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        self.assertEqual(15, noon.astimezone(zone).hour)

    def test_unknown_zone_falls_back_to_utc(self) -> None:
        self.assertIs(timezone.utc, resolve_timezone("Not/AZone"))


class SettingsFromEnvTest(unittest.TestCase):
    def test_defaults(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("NIGHTLY_REFRESH_")
        }
        with patch.dict(os.environ, env, clear=True):
            settings = NightlyRefreshSettings.from_env()
        self.assertTrue(settings.enabled)
        self.assertEqual(3, settings.hour)
        self.assertEqual(0, settings.minute)
        self.assertEqual(DEFAULT_TIMEZONE, settings.timezone_name)
        self.assertEqual(3, settings.catchup_hours)

    def test_parses_overrides(self) -> None:
        env = {
            "NIGHTLY_REFRESH_ENABLED": "false",
            "NIGHTLY_REFRESH_HOUR": "2",
            "NIGHTLY_REFRESH_MINUTE": "15",
            "NIGHTLY_REFRESH_TZ": "UTC",
            "NIGHTLY_REFRESH_CATCHUP_HOURS": "1",
        }
        with patch.dict(os.environ, env, clear=False):
            settings = NightlyRefreshSettings.from_env()
        self.assertFalse(settings.enabled)
        self.assertEqual(2, settings.hour)
        self.assertEqual(15, settings.minute)
        self.assertEqual("UTC", settings.timezone_name)
        self.assertEqual(1, settings.catchup_hours)

    def test_rejects_an_hour_outside_the_clock(self) -> None:
        with patch.dict(os.environ, {"NIGHTLY_REFRESH_HOUR": "24"}, clear=False):
            with self.assertRaises(ValueError):
                NightlyRefreshSettings.from_env()


class ScheduleMathTest(unittest.TestCase):
    def setUp(self) -> None:
        # Europe/Moscow is UTC+3 year-round, with or without tzdata on the image.
        self.settings = NightlyRefreshSettings(
            hour=3, minute=0, timezone_name="Europe/Moscow", catchup_hours=3
        )

    def test_before_the_hour_sleeps_until_tonight(self) -> None:
        delay = seconds_until_next_run(_at(2, 0), self.settings)
        self.assertEqual(3600, delay)

    def test_after_the_hour_without_catchup_waits_until_tomorrow(self) -> None:
        delay = seconds_until_next_run(
            _at(3, 5), self.settings, allow_catchup=False
        )
        self.assertEqual(23 * 3600 + 55 * 60, delay)

    def test_catchup_window_runs_immediately(self) -> None:
        self.assertEqual(
            0.0,
            seconds_until_next_run(_at(4, 0), self.settings, allow_catchup=True),
        )

    def test_after_catchup_window_waits_until_tomorrow(self) -> None:
        delay = seconds_until_next_run(
            _at(7, 0), self.settings, allow_catchup=True
        )
        self.assertEqual(20 * 3600, delay)

    def test_start_of_local_day_is_midnight_moscow_in_utc(self) -> None:
        start = start_of_local_day(_at(15, 30), MOSCOW)
        self.assertEqual(datetime(2026, 8, 17, 21, 0, tzinfo=UTC), start)

    def test_next_run_at_is_timezone_aware(self) -> None:
        stamp = next_run_at(_at(2, 0), self.settings)
        self.assertEqual(timezone.utc, stamp.tzinfo)
        self.assertEqual(_at(3, 0).astimezone(UTC), stamp)


class NightlyLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_first_iteration_may_catch_up_then_waits(self) -> None:
        calls: list[str] = []
        sleeps: list[float] = []

        async def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            raise asyncio.CancelledError

        settings = NightlyRefreshSettings(
            hour=3, minute=0, timezone_name="Europe/Moscow", catchup_hours=3
        )
        clock_times = [_at(4, 0), _at(4, 0)]

        def now() -> datetime:
            return clock_times[min(len(sleeps), len(clock_times) - 1)]

        with self.assertRaises(asyncio.CancelledError):
            await run_nightly_loop(
                lambda: calls.append("ran"),
                settings,
                sleep=fake_sleep,
                now_fn=now,
            )

        self.assertEqual(["ran"], calls)
        # First tick is inside the catch-up window so there is no initial sleep;
        # after the sweep the loop waits for tomorrow's 03:00.
        self.assertEqual([23 * 3600], sleeps)


@requires_database
class NightlyRefreshIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)
        self.settings = NightlyRefreshSettings(
            hour=3, minute=0, timezone_name="Europe/Moscow"
        )

        report = run_ingestion(
            FakeClient(_consistent_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        run_quality_checks(self.session_factory, run_id=report["run_id"])
        with session_scope(self.session_factory) as session:
            persist_catalogue(session, normalize_catalogue(_catalogue_payload()))

    def _tonight(self, hour: int = 3, minute: int = 0) -> datetime:
        """Wall-clock time tonight in the scheduler timezone, on the real date.

        Job ``created_at`` is written by the database clock, so the once-a-day
        guard must be evaluated against *today*, not a fixture date.
        """
        return datetime.now(self.settings.tz).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    def _exec(self, sql: str, **params):
        with self.engine.begin() as connection:
            return connection.execute(text(sql), params)

    def _mark_imported_season_active(self, *, active: bool = True) -> None:
        self._exec("UPDATE seasons SET is_active = :active", active=active)

    def _insert_second_active_league(self) -> None:
        """A second imported active season without a full second ingestion."""
        self._exec(
            """
            INSERT INTO competitions
                (fantasy_tournament_id, slug, name, sort_order)
            VALUES ('15', 'italy', 'Италия', 1)
            ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
            """
        )
        competition_id = self._exec(
            "SELECT id FROM competitions WHERE slug = 'italy'"
        ).scalar_one()
        self._exec(
            """
            INSERT INTO seasons
                (competition_id, fantasy_season_id, stat_season_id, name, is_active)
            VALUES (:cid, '69', 'serie_a_25-26', '2025/2026', true)
            ON CONFLICT (fantasy_season_id) DO NOTHING
            """,
            cid=competition_id,
        )

    def test_finished_import_is_not_eligible(self) -> None:
        with session_scope(self.session_factory) as session:
            self.assertEqual([], list_imported_active_leagues(session))

    def test_active_imported_season_is_eligible(self) -> None:
        self._mark_imported_season_active()
        with session_scope(self.session_factory) as session:
            eligible = list_imported_active_leagues(session)
        self.assertEqual(["russia"], [item.tournament_slug for item in eligible])
        self.assertIsInstance(eligible[0], EligibleLeague)
        self.assertEqual("2025/2026", eligible[0].season_name)

    def test_sweep_enqueues_current_season_scheduled_jobs(self) -> None:
        self._mark_imported_season_active()
        self._insert_second_active_league()
        spawned: list[int] = []

        report = enqueue_nightly_jobs(
            self.session_factory,
            spawned.append,
            settings=self.settings,
            now=self._tonight(),
        )

        self.assertEqual(
            ["russia", "italy"],
            [item["tournament_slug"] for item in report["eligible"]],
        )
        self.assertEqual(
            ["russia", "italy"],
            [item["tournament_slug"] for item in report["enqueued"]],
        )
        self.assertEqual([], report["skipped"])
        self.assertEqual(2, len(spawned))
        for job in report["enqueued"]:
            self.assertEqual(SCHEDULED_TRIGGER, job["trigger_type"])
            self.assertTrue(job["use_current_season"])
            self.assertEqual("pending", job["status"])

    def test_second_sweep_the_same_day_is_skipped(self) -> None:
        self._mark_imported_season_active()
        spawned: list[int] = []
        enqueue_nightly_jobs(
            self.session_factory,
            spawned.append,
            settings=self.settings,
            now=self._tonight(),
        )
        again = enqueue_nightly_jobs(
            self.session_factory,
            spawned.append,
            settings=self.settings,
            now=self._tonight(4, 30),
        )

        self.assertEqual([], again["enqueued"])
        self.assertEqual(
            [
                {
                    "tournament_slug": "russia",
                    "reason": SKIP_ALREADY_RAN_TODAY,
                }
            ],
            again["skipped"],
        )
        self.assertEqual(1, len(spawned))

    def test_force_bypasses_the_once_a_day_guard_after_the_job_finishes(self) -> None:
        self._mark_imported_season_active()
        spawned: list[int] = []
        first = enqueue_nightly_jobs(
            self.session_factory,
            spawned.append,
            settings=self.settings,
            now=self._tonight(),
        )
        with session_scope(self.session_factory) as session:
            repo = IngestionJobRepository(session)
            repo.mark_succeeded(repo.get(first["enqueued"][0]["id"]), result={})

        forced = enqueue_nightly_jobs(
            self.session_factory,
            spawned.append,
            settings=self.settings,
            now=self._tonight(4, 0),
            force=True,
        )
        self.assertEqual(1, len(forced["enqueued"]))
        self.assertEqual(2, len(spawned))
        self.assertNotEqual(first["enqueued"][0]["id"], forced["enqueued"][0]["id"])

    def test_active_manual_job_is_skipped(self) -> None:
        self._mark_imported_season_active()
        with session_scope(self.session_factory) as session:
            IngestionJobRepository(session).enqueue(tournament_slug="russia")

        spawned: list[int] = []
        report = enqueue_nightly_jobs(
            self.session_factory,
            spawned.append,
            settings=self.settings,
            now=self._tonight(),
        )
        self.assertEqual([], report["enqueued"])
        self.assertEqual([], spawned)
        self.assertEqual(
            [
                {
                    "tournament_slug": "russia",
                    "reason": SKIP_ALREADY_RUNNING,
                }
            ],
            report["skipped"],
        )

    def test_status_lists_eligible_leagues_and_todays_jobs(self) -> None:
        self._mark_imported_season_active()
        enqueue_nightly_jobs(
            self.session_factory,
            lambda job_id: None,
            settings=self.settings,
            now=self._tonight(),
        )
        status = scheduler_status(
            self.session_factory, self.settings, now=self._tonight(3, 5)
        )
        self.assertEqual(["russia"], [item["tournament_slug"] for item in status["eligible"]])
        self.assertEqual(1, len(status["scheduled_today"]))
        self.assertEqual(SCHEDULED_TRIGGER, status["scheduled_today"][0]["trigger_type"])
        self.assertTrue(status["next_run_at"])


@requires_database
class NightlyRefreshApiTest(unittest.TestCase):
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
        run_quality_checks(self.session_factory, run_id=report["run_id"])
        with self.engine.begin() as connection:
            connection.execute(text("UPDATE seasons SET is_active = true"))

    def _client(self, spawned: list[int] | None = None) -> TestClient:
        jobs = spawned if spawned is not None else []
        return TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=jobs.append,
                ensure_forecasts=lambda *a, **k: 0,
                nightly_refresh=NightlyRefreshSettings(
                    enabled=False, hour=3, timezone_name="Europe/Moscow"
                ),
            )
        )

    def test_get_reports_eligible_league(self) -> None:
        body = self._client().get("/admin/ingestion/nightly").json()
        self.assertFalse(body["enabled"])
        self.assertEqual("Europe/Moscow", body["timezone"])
        self.assertEqual(3, body["hour"])
        self.assertEqual(["russia"], [item["tournament_slug"] for item in body["eligible"]])
        self.assertEqual([], body["scheduled_today"])

    def test_post_enqueues_scheduled_jobs(self) -> None:
        spawned: list[int] = []
        response = self._client(spawned).post("/admin/ingestion/nightly")
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(1, len(body["enqueued"]))
        self.assertEqual("scheduled", body["enqueued"][0]["trigger_type"])
        self.assertTrue(body["enqueued"][0]["use_current_season"])
        self.assertEqual(spawned, [body["enqueued"][0]["id"]])

    def test_second_post_without_force_is_a_no_op(self) -> None:
        spawned: list[int] = []
        client = self._client(spawned)
        self.assertEqual(200, client.post("/admin/ingestion/nightly").status_code)
        again = client.post("/admin/ingestion/nightly?force=false")
        self.assertEqual(200, again.status_code)
        self.assertEqual([], again.json()["enqueued"])
        self.assertEqual(SKIP_ALREADY_RAN_TODAY, again.json()["skipped"][0]["reason"])
        self.assertEqual(1, len(spawned))


if __name__ == "__main__":
    unittest.main()
