"""Unit and integration tests for the admin ingestion API (step 5).

The unit tests cover the request-model validation. The integration tests drive
the FastAPI app with a real PostgreSQL database, injecting a synchronous worker
so the full refresh → status flow (including a real import) runs end to end.
They are skipped automatically when no database is reachable.
"""

from __future__ import annotations

import os
import unittest

import pydantic
from fastapi.testclient import TestClient
from sqlalchemy import text

from fantasy_analytics.api import RefreshRequest, create_app
from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.ingestion_worker import execute_job

# Reuse the fetch fixture and fake client from the ingestion tests.
from test_ingestion import FakeClient, _build_fixture

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)

REFRESH_URL = "/admin/ingestion/rpl/refresh"


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


if __name__ == "__main__":
    unittest.main()
