"""Tests for the one-button full refresh (every league, both seasons, odds).

The planner is pinned without a database. The orchestrator is driven against
a real PostgreSQL database with a fake worker that finishes jobs in-process
and a fake odds refresh, so the order of the steps, the waiting on jobs, the
tolerance of a failing league and the HTTP surface are all exercised without
the network or a subprocess. The database tests are skipped automatically
when no database is reachable.
"""

from __future__ import annotations

import asyncio
import os
import time
import unittest
from typing import Any

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
from fantasy_analytics.full_refresh import (
    FULL_REFRESH_TRIGGER,
    STATUS_FAILED,
    STATUS_SKIPPED,
    STATUS_SUCCEEDED,
    STEP_CURRENT_SEASON,
    STEP_LATEST_COMPLETED,
    STEP_ODDS,
    FullRefreshBusy,
    FullRefreshManager,
    plan_full_refresh,
)
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.nightly_refresh import NightlyRefreshSettings
from fantasy_analytics.quality import run_quality_checks

from test_competitions import _catalogue_payload
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


requires_database = unittest.skipUnless(
    _database_available(),
    "PostgreSQL is not reachable via TEST_DATABASE_URL/DATABASE_URL",
)


def _competition(slug: str, *, imported: bool = True, active: bool = True) -> dict[str, Any]:
    return {
        "slug": slug,
        "name": slug.title(),
        "is_imported": imported,
        "has_active_season": active,
    }


class PlanTest(unittest.TestCase):
    def test_every_imported_league_gets_both_seasons_then_odds_in_order(self) -> None:
        steps = plan_full_refresh(
            [_competition("russia"), _competition("portugal", imported=False), _competition("spain")]
        )
        self.assertEqual(
            [
                ("russia", STEP_LATEST_COMPLETED),
                ("russia", STEP_CURRENT_SEASON),
                ("russia", STEP_ODDS),
                ("spain", STEP_LATEST_COMPLETED),
                ("spain", STEP_CURRENT_SEASON),
                ("spain", STEP_ODDS),
            ],
            [(step.tournament_slug, step.kind) for step in steps],
        )
        self.assertTrue(all(step.status == "pending" for step in steps))

    def test_a_league_without_an_active_season_skips_the_current_import(self) -> None:
        steps = plan_full_refresh([_competition("russia", active=False)])
        current = next(step for step in steps if step.kind == STEP_CURRENT_SEASON)
        self.assertEqual(STATUS_SKIPPED, current.status)
        self.assertIn("активного сезона", current.detail or "")

    def test_nothing_imported_means_no_steps(self) -> None:
        self.assertEqual([], plan_full_refresh([_competition("russia", imported=False)]))


@requires_database
class FullRefreshIntegrationTest(unittest.TestCase):
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
        with session_scope(self.session_factory) as session:
            persist_catalogue(session, normalize_catalogue(_catalogue_payload()))
        self.spawned: list[int] = []
        self.odds_calls: list[str] = []

    def _finish_job(self, job_id: int, *, succeed: bool = True) -> None:
        with session_scope(self.session_factory) as session:
            repo = IngestionJobRepository(session)
            job = repo.get(job_id)
            repo.mark_running(job)
            if succeed:
                repo.mark_succeeded(
                    job,
                    {"data_freshness": "2026-09-04T10:00:00+00:00", "quality": {"passed": True}},
                )
            else:
                repo.mark_failed(job, "collector exploded")

    def _worker(self, *, fail_job_ids: set[int] | None = None):
        def spawn(job_id: int) -> None:
            self.spawned.append(job_id)
            self._finish_job(job_id, succeed=job_id not in (fail_job_ids or set()))

        return spawn

    def _odds(self, *, fail_for: str | None = None):
        def refresh(session_factory, *, tournament_slug: str) -> dict[str, Any]:
            self.odds_calls.append(tournament_slug)
            if tournament_slug == fail_for:
                raise RuntimeError("calendar widget is down")
            return {"fetched": 9, "linked": 9, "forecast_rows": 500}

        return refresh

    def _manager(self, **kwargs: Any) -> FullRefreshManager:
        return FullRefreshManager(
            self.session_factory,
            kwargs.pop("spawn_worker", self._worker()),
            kwargs.pop("refresh_odds", self._odds()),
            poll_interval=0.01,
            **kwargs,
        )

    def _run(self, manager: FullRefreshManager) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            await manager.start()
            await manager.wait()
            return manager.status()

        return asyncio.run(go())

    def test_runs_latest_then_current_then_odds_and_records_the_jobs(self) -> None:
        status = self._run(self._manager())
        run = status["run"]
        self.assertFalse(status["is_running"])
        self.assertEqual(STATUS_SUCCEEDED, run["status"])
        kinds = [(s["tournament_slug"], s["kind"], s["status"]) for s in run["steps"]]
        self.assertEqual(
            [
                ("russia", STEP_LATEST_COMPLETED, STATUS_SUCCEEDED),
                ("russia", STEP_CURRENT_SEASON, STATUS_SUCCEEDED),
                ("russia", STEP_ODDS, STATUS_SUCCEEDED),
            ],
            kinds,
        )
        # Two imports were spawned in order, and the odds came after them.
        self.assertEqual(2, len(self.spawned))
        self.assertEqual(["russia"], self.odds_calls)
        with session_scope(self.session_factory) as session:
            repo = IngestionJobRepository(session)
            first, second = (repo.get(job_id) for job_id in self.spawned)
            self.assertFalse(first.use_current_season)
            self.assertTrue(second.use_current_season)
            self.assertEqual(FULL_REFRESH_TRIGGER, first.trigger_type)
        self.assertEqual(self.spawned, [s["job_id"] for s in run["steps"][:2]])
        self.assertIn("снапшот опубликован", run["steps"][0]["detail"])
        self.assertIn("линий 9", run["steps"][2]["detail"])
        self.assertEqual(3, run["completed_steps"])
        self.assertEqual(0, run["failed_steps"])

    def test_a_failing_step_is_recorded_and_the_run_carries_on(self) -> None:
        # The first job fails in its worker; the current-season import and the
        # odds still run, and the run ends as failed with the reason on the step.
        failing: set[int] = set()

        def spawn(job_id: int) -> None:
            if not self.spawned:
                failing.add(job_id)
            self.spawned.append(job_id)
            self._finish_job(job_id, succeed=job_id not in failing)

        status = self._run(self._manager(spawn_worker=spawn, refresh_odds=self._odds(fail_for="russia")))
        run = status["run"]
        self.assertEqual(STATUS_FAILED, run["status"])
        statuses = [s["status"] for s in run["steps"]]
        self.assertEqual([STATUS_FAILED, STATUS_SUCCEEDED, STATUS_FAILED], statuses)
        self.assertIn("collector exploded", run["steps"][0]["error"])
        self.assertIn("calendar widget", run["steps"][2]["error"])
        self.assertEqual(2, run["failed_steps"])
        self.assertEqual(2, len(self.spawned))

    def test_waits_for_a_job_that_is_already_running_instead_of_failing(self) -> None:
        # A manual refresh of the same league is in flight when the run reaches
        # it: the lock refuses a second job, so the run adopts the running one.
        with session_scope(self.session_factory) as session:
            job, created = IngestionJobRepository(session).enqueue(tournament_slug="russia")
            self.assertTrue(created)
            running_id = job.id

        async def go() -> dict[str, Any]:
            manager = self._manager()
            await manager.start()
            await asyncio.sleep(0.05)
            self.assertTrue(manager.status()["is_running"])
            self.assertEqual(running_id, manager.run.steps[0].job_id)
            self._finish_job(running_id)
            await manager.wait()
            return manager.status()

        status = asyncio.run(go())
        self.assertEqual(STATUS_SUCCEEDED, status["run"]["status"])
        self.assertIn("уже запущенного", status["run"]["steps"][0]["detail"])
        # Only the current-season import was spawned by the run itself.
        self.assertEqual(1, len(self.spawned))

    def test_a_second_start_while_running_is_refused(self) -> None:
        with session_scope(self.session_factory) as session:
            IngestionJobRepository(session).enqueue(tournament_slug="russia")

        async def go() -> None:
            manager = self._manager()
            await manager.start()
            with self.assertRaises(FullRefreshBusy):
                await manager.start()
            manager._task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await manager.wait()

        asyncio.run(go())

    def test_http_surface(self) -> None:
        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=self._worker(),
                refresh_odds=self._odds(),
                ensure_forecasts=lambda *a, **k: 0,
                nightly_refresh=NightlyRefreshSettings(enabled=False),
            )
        )
        with client:
            idle = client.get("/admin/ingestion/full-refresh").json()
            self.assertEqual({"is_running": False, "run": None}, idle)
            started = client.post("/admin/ingestion/full-refresh")
            self.assertEqual(202, started.status_code)
            body = started.json()
            self.assertEqual(3, body["run"]["total_steps"])
            # The fake worker finishes jobs inline, so the run is over quickly.
            for _ in range(200):
                final = client.get("/admin/ingestion/full-refresh").json()
                if not final["is_running"]:
                    break
                time.sleep(0.01)
            self.assertFalse(final["is_running"])
            self.assertEqual(STATUS_SUCCEEDED, final["run"]["status"])
            self.assertEqual(["russia"], self.odds_calls)


if __name__ == "__main__":
    unittest.main()
