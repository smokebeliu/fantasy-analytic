"""Unit and integration tests for the admin ingestion API (steps 5 and 17).

The unit tests cover the request-model validation. The integration tests drive
the FastAPI app with a real PostgreSQL database, injecting a synchronous worker
so the full refresh → status flow (including a real import) runs end to end.
A dedicated set covers the step-17 status endpoint, which the admin UI polls
without knowing a job id — the property that makes a page reload (or a second
tab) recover an in-flight refresh. They are skipped automatically when no
database is reachable.
"""

from __future__ import annotations

import os
import unittest

import pydantic
from fastapi.testclient import TestClient
from sqlalchemy import text

from fantasy_analytics.api import RefreshRequest, create_app
from fantasy_analytics.db import (
    IngestionJobRepository,
    create_db_engine,
    create_session_factory,
    migration,
    session_scope,
)
from fantasy_analytics.ingestion_worker import execute_job

# Reuse the fetch fixture and fake client from the ingestion tests.
from test_ingestion import FakeClient, _build_fixture

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)

REFRESH_URL = "/admin/ingestion/rpl/refresh"
STATUS_URL = "/admin/ingestion/rpl/status"


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


class RefreshRequestTest(unittest.TestCase):
    def test_defaults_are_empty(self) -> None:
        request = RefreshRequest()
        self.assertIsNone(request.season_id)
        self.assertIsNone(request.season_name)
        self.assertFalse(request.current)

    def test_rejects_multiple_selectors(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            RefreshRequest(season_id="59", current=True)

    def test_allows_single_selector(self) -> None:
        request = RefreshRequest(season_name="2025/2026")
        self.assertEqual("2025/2026", request.season_name)


@requires_database
class ApiIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)

    def _app(self, spawn_worker=None):
        spawned: list[int] = []

        def record(job_id: int) -> None:
            spawned.append(job_id)

        app = create_app(
            session_factory=self.session_factory,
            spawn_worker=spawn_worker or record,
        )
        return TestClient(app), spawned

    def test_refresh_returns_202_and_enqueues_one_worker(self) -> None:
        client, spawned = self._app()

        response = client.post(REFRESH_URL)

        self.assertEqual(202, response.status_code)
        body = response.json()
        self.assertEqual("pending", body["status"])
        self.assertEqual("russia", body["tournament_slug"])
        self.assertIsInstance(body["id"], int)
        self.assertEqual([body["id"]], spawned)

    def test_concurrent_refresh_conflicts_without_second_worker(self) -> None:
        client, spawned = self._app()

        first = client.post(REFRESH_URL)
        second = client.post(REFRESH_URL)

        self.assertEqual(202, first.status_code)
        self.assertEqual(409, second.status_code)
        conflict = second.json()["error"]
        self.assertEqual("conflict", conflict["type"])
        self.assertEqual(first.json()["id"], conflict["details"]["job"]["id"])
        # Only the first request may launch a worker.
        self.assertEqual([first.json()["id"]], spawned)

    def test_get_run_returns_job_and_404_for_unknown(self) -> None:
        client, _ = self._app()
        job_id = client.post(REFRESH_URL).json()["id"]

        found = client.get(f"/admin/ingestion/runs/{job_id}")
        self.assertEqual(200, found.status_code)
        self.assertEqual("pending", found.json()["status"])

        missing = client.get("/admin/ingestion/runs/999999")
        self.assertEqual(404, missing.status_code)

    def test_status_survives_api_restart(self) -> None:
        client, _ = self._app()
        job_id = client.post(REFRESH_URL).json()["id"]

        # Simulate an API restart with a brand-new engine and session factory.
        engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(engine.dispose)
        restarted = TestClient(
            create_app(
                session_factory=create_session_factory(engine),
                spawn_worker=lambda job_id: None,
            )
        )

        response = restarted.get(f"/admin/ingestion/runs/{job_id}")
        self.assertEqual(200, response.status_code)
        self.assertEqual("pending", response.json()["status"])

    def test_bad_request_body_is_rejected(self) -> None:
        client, spawned = self._app()

        response = client.post(REFRESH_URL, json={"season_id": "59", "current": True})

        self.assertEqual(422, response.status_code)
        self.assertEqual([], spawned)

    def test_end_to_end_refresh_runs_import_and_publishes(self) -> None:
        def spawn(job_id: int) -> None:
            execute_job(
                self.engine,
                self.session_factory,
                job_id,
                client=FakeClient(_build_fixture()),
            )

        client, _ = self._app(spawn_worker=spawn)
        job_id = client.post(REFRESH_URL).json()["id"]

        response = client.get(f"/admin/ingestion/runs/{job_id}")
        body = response.json()
        self.assertEqual("succeeded", body["status"])
        self.assertIsNotNone(body["ingestion_run_id"])
        self.assertIsNotNone(body["data_freshness"])
        self.assertTrue(body["result"]["snapshot_active"])
        self.assertEqual(2, body["result"]["ingestion"]["counts"]["clubs"])

        # After completion a new refresh is allowed again (no active job).
        again = client.post(REFRESH_URL)
        self.assertEqual(202, again.status_code)
        self.assertNotEqual(job_id, again.json()["id"])


@requires_database
class IngestionStatusTest(unittest.TestCase):
    """The step-17 status endpoint: refresh state without a job id."""

    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)

    def _client(self, spawn_worker=None) -> TestClient:
        return TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=spawn_worker or (lambda job_id: None),
            )
        )

    def test_empty_database_reports_no_refresh_and_no_snapshot(self) -> None:
        response = self._client().get(STATUS_URL)

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual("russia", body["tournament_slug"])
        self.assertFalse(body["is_refreshing"])
        self.assertIsNone(body["active_job"])
        self.assertIsNone(body["latest_job"])
        self.assertIsNone(body["snapshot"])
        self.assertIsNone(body["target_tour"])
        # The stage vocabulary is always published so the UI can render it.
        self.assertEqual("queued", body["stages"][0]["stage"])
        self.assertEqual(100, body["stages"][-1]["percent"])

    def test_queued_job_is_discovered_without_a_job_id(self) -> None:
        client = self._client()
        job_id = client.post(REFRESH_URL).json()["id"]

        body = client.get(STATUS_URL).json()

        self.assertTrue(body["is_refreshing"])
        self.assertEqual(job_id, body["active_job"]["id"])
        self.assertEqual("pending", body["active_job"]["status"])

    def test_active_job_survives_an_api_restart(self) -> None:
        """A page reload hits a possibly restarted API and must still see it."""
        job_id = self._client().post(REFRESH_URL).json()["id"]

        engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(engine.dispose)
        restarted = TestClient(
            create_app(
                session_factory=create_session_factory(engine),
                spawn_worker=lambda job_id: None,
            )
        )

        body = restarted.get(STATUS_URL).json()
        self.assertTrue(body["is_refreshing"])
        self.assertEqual(job_id, body["active_job"]["id"])

    def test_progress_of_a_running_job_is_exposed(self) -> None:
        client = self._client()
        job_id = client.post(REFRESH_URL).json()["id"]
        with session_scope(self.session_factory) as session:
            repo = IngestionJobRepository(session)
            job = repo.get(job_id)
            repo.mark_running(job)
            repo.record_progress(
                job, stage="fetch_history", percent=45, message="Fetching match history"
            )

        progress = client.get(STATUS_URL).json()["active_job"]["progress"]
        self.assertEqual("fetch_history", progress["stage"])
        self.assertEqual(45, progress["percent"])
        self.assertEqual("Fetching match history", progress["message"])
        self.assertIsNotNone(progress["updated_at"])

    def test_after_a_successful_refresh_reports_snapshot_and_target_tour(self) -> None:
        def spawn(job_id: int) -> None:
            execute_job(
                self.engine,
                self.session_factory,
                job_id,
                client=FakeClient(_build_fixture()),
            )

        client = self._client(spawn_worker=spawn)
        job_id = client.post(REFRESH_URL).json()["id"]

        body = client.get(STATUS_URL).json()

        self.assertFalse(body["is_refreshing"])
        self.assertIsNone(body["active_job"])
        self.assertEqual(job_id, body["latest_successful_job"]["id"])
        self.assertEqual("finished", body["latest_successful_job"]["progress"]["stage"])
        # The published snapshot and the season/tour the UI shows.
        self.assertIsNotNone(body["snapshot"]["data_freshness"])
        self.assertEqual("2025/2026", body["season"]["name"])
        self.assertEqual("1 тур", body["target_tour"]["name"])
        # The polling payload carries the headline counts, not the full report.
        result = body["latest_successful_job"]["result"]
        self.assertTrue(result["snapshot_active"])
        self.assertEqual(2, result["counts"]["clubs"])
        self.assertTrue(result["quality"]["passed"])
        self.assertNotIn("checks", result["quality"])
        # The full report stays available on the per-job endpoint.
        full = client.get(f"/admin/ingestion/runs/{job_id}").json()
        self.assertIn("checks", full["result"]["quality"])

    def test_failed_job_is_reported_with_its_safe_message(self) -> None:
        def spawn(job_id: int) -> None:
            execute_job(
                self.engine,
                self.session_factory,
                job_id,
                client=FakeClient(_build_fixture(), fail_on_history=True),
            )

        client = self._client(spawn_worker=spawn)
        client.post(REFRESH_URL)

        body = client.get(STATUS_URL).json()
        self.assertFalse(body["is_refreshing"])
        self.assertEqual("failed", body["latest_job"]["status"])
        self.assertIn("simulated history failure", body["latest_job"]["error_message"])
        self.assertIsNone(body["latest_successful_job"])
        # Nothing was published, so no snapshot is offered to the UI.
        self.assertIsNone(body["snapshot"])


if __name__ == "__main__":
    unittest.main()
