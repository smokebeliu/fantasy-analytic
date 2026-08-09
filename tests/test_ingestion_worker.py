"""Unit and integration tests for the manual ingestion worker (step 5).

The unit tests exercise the pure helpers. The integration tests drive
:func:`execute_job` against a real PostgreSQL database with the synthetic season
and fake GraphQL client from the ingestion tests, covering the success path, a
safe failure and advisory-lock mutual exclusion. They are skipped automatically
when no database is reachable via ``TEST_DATABASE_URL``/``DATABASE_URL``.
"""

from __future__ import annotations

import os
import unittest

from sqlalchemy import text

from fantasy_analytics.db import (
    IngestionJobRepository,
    advisory_lock_key,
    create_db_engine,
    create_session_factory,
    migration,
    session_scope,
)
from fantasy_analytics.ingestion_worker import execute_job, safe_error_message

# Reuse the fetch fixture and fake client from the ingestion tests; the tests
# directory is on sys.path during unittest discovery.
from test_ingestion import FakeClient, _build_fixture

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


class SafeErrorMessageTest(unittest.TestCase):
    def test_redacts_connection_credentials(self) -> None:
        message = safe_error_message(
            RuntimeError(
                "connect failed postgresql+psycopg://user:secret@db:5432/x"
            )
        )
        self.assertNotIn("secret", message)
        self.assertIn("postgresql+psycopg://***@db:5432/x", message)
        self.assertTrue(message.startswith("RuntimeError:"))

    def test_message_is_bounded(self) -> None:
        message = safe_error_message(ValueError("x" * 1000))
        self.assertLessEqual(len(message), 500)


@requires_database
class WorkerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)

    def _enqueue(self) -> int:
        with session_scope(self.session_factory) as session:
            job, created = IngestionJobRepository(session).enqueue(
                tournament_slug="russia"
            )
            self.assertTrue(created)
            return job.id

    def _count(self, table: str) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table}")
                ).scalar_one()
            )

    def _job(self, job_id: int):
        with session_scope(self.session_factory) as session:
            job = IngestionJobRepository(session).get(job_id)
            session.expunge(job)
            return job

    def test_progress_is_recorded_and_ends_at_full_completion(self) -> None:
        """Step 17: the UI polls the job row, so the worker must write stages."""
        job_id = self._enqueue()
        seen: list[str] = []

        status = execute_job(
            self.engine,
            self.session_factory,
            job_id,
            client=FakeClient(_build_fixture()),
            on_progress=seen.append,
        )

        self.assertEqual("succeeded", status)
        job = self._job(job_id)
        self.assertEqual("finished", job.progress_stage)
        self.assertEqual(100, job.progress_percent)
        self.assertIsNotNone(job.progress_updated_at)
        self.assertIn("succeeded", job.progress_message)
        # The quality gate is announced as its own stage before it runs.
        self.assertTrue(
            any(message.startswith("Running quality checks") for message in seen),
            seen,
        )

    def test_failed_job_keeps_the_stage_it_died_in(self) -> None:
        job_id = self._enqueue()

        execute_job(
            self.engine,
            self.session_factory,
            job_id,
            client=FakeClient(_build_fixture(), fail_on_history=True),
        )

        job = self._job(job_id)
        self.assertEqual("failed", job.status)
        # The import dies while fetching the per-player history, which is what
        # the UI should show next to the error.
        self.assertEqual("fetch_history", job.progress_stage)
        self.assertIn("simulated history failure", job.progress_message)

    def test_successful_job_publishes_snapshot(self) -> None:
        job_id = self._enqueue()

        status = execute_job(
            self.engine,
            self.session_factory,
            job_id,
            client=FakeClient(_build_fixture()),
        )

        self.assertEqual("succeeded", status)
        job = self._job(job_id)
        self.assertEqual("succeeded", job.status)
        self.assertIsNotNone(job.ingestion_run_id)
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertTrue(job.result["snapshot_active"])
        self.assertIsNotNone(job.result["data_freshness"])
        self.assertEqual(2, job.result["ingestion"]["counts"]["clubs"])

        # The snapshot became the active one for its season.
        with self.engine.connect() as connection:
            active = connection.execute(
                text("SELECT is_active FROM ingestion_runs WHERE id = :id"),
                {"id": job.ingestion_run_id},
            ).scalar_one()
        self.assertTrue(active)

    def test_failed_job_records_safe_message_and_no_partial_data(self) -> None:
        job_id = self._enqueue()

        status = execute_job(
            self.engine,
            self.session_factory,
            job_id,
            client=FakeClient(_build_fixture(), fail_on_history=True),
        )

        self.assertEqual("failed", status)
        job = self._job(job_id)
        self.assertEqual("failed", job.status)
        self.assertIsNotNone(job.error_message)
        self.assertIsNone(job.result)
        self.assertEqual(0, self._count("clubs"))
        self.assertEqual(0, self._count("matches"))

    def test_advisory_lock_blocks_concurrent_worker(self) -> None:
        job_id = self._enqueue()
        key = advisory_lock_key("russia")

        holder = self.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})

            status = execute_job(
                self.engine,
                self.session_factory,
                job_id,
                client=FakeClient(_build_fixture()),
            )
        finally:
            holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
            holder.close()

        self.assertEqual("failed", status)
        job = self._job(job_id)
        self.assertEqual("failed", job.status)
        self.assertIn("already running", job.error_message)
        # The blocked worker never touched the domain tables.
        self.assertEqual(0, self._count("clubs"))

    def test_non_pending_job_is_skipped(self) -> None:
        job_id = self._enqueue()
        with session_scope(self.session_factory) as session:
            repo = IngestionJobRepository(session)
            repo.mark_running(repo.get(job_id))

        status = execute_job(
            self.engine,
            self.session_factory,
            job_id,
            client=FakeClient(_build_fixture()),
        )

        self.assertEqual("skipped", status)
        self.assertEqual(0, self._count("clubs"))


if __name__ == "__main__":
    unittest.main()
