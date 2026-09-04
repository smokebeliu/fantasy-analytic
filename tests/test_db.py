"""Unit and integration tests for the persistence layer.

Integration tests require a reachable PostgreSQL instance. Configure it through
``TEST_DATABASE_URL`` (preferred) or ``DATABASE_URL``. When neither points to a
reachable database, the integration tests are skipped while the pure unit tests
still run.
"""

import os
import unittest

from sqlalchemy import inspect, text

from fantasy_analytics.db import (
    IngestionRepository,
    create_db_engine,
    create_session_factory,
    session_scope,
)
from fantasy_analytics.db import migration
from fantasy_analytics.db.repository import compute_response_hash

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


class ResponseHashTest(unittest.TestCase):
    def test_hash_is_stable_regardless_of_key_order(self) -> None:
        first = compute_response_hash({"a": 1, "b": [1, 2, 3]})
        second = compute_response_hash({"b": [1, 2, 3], "a": 1})

        self.assertEqual(first, second)

    def test_hash_differs_for_different_payloads(self) -> None:
        self.assertNotEqual(
            compute_response_hash({"a": 1}),
            compute_response_hash({"a": 2}),
        )


@requires_database
class MigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        # Ensure a known baseline for every test.
        migration.downgrade(TEST_DATABASE_URL)

    def tearDown(self) -> None:
        # Leave the database migrated for the remaining test classes.
        migration.upgrade(TEST_DATABASE_URL)

    def _domain_tables(self) -> set[str]:
        inspector = inspect(self.engine)
        return set(inspector.get_table_names()) - {"alembic_version"}

    def test_upgrade_creates_all_domain_tables(self) -> None:
        migration.upgrade(TEST_DATABASE_URL)

        tables = self._domain_tables()
        self.assertIn("ingestion_runs", tables)
        self.assertIn("raw_api_responses", tables)
        self.assertGreaterEqual(len(tables), 17)

    def test_downgrade_removes_all_domain_tables(self) -> None:
        migration.upgrade(TEST_DATABASE_URL)
        migration.downgrade(TEST_DATABASE_URL)

        self.assertEqual(set(), self._domain_tables())


@requires_database
class IngestionRepositoryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migration.upgrade(TEST_DATABASE_URL)

    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        # Each test runs inside a single transaction that is rolled back.
        self.connection = self.engine.connect()
        self.transaction = self.connection.begin()
        self.session_factory = create_session_factory(self.connection)
        self.session = self.session_factory()
        self.repository = IngestionRepository(self.session)

        def _cleanup() -> None:
            self.session.close()
            if self.transaction.is_active:
                self.transaction.rollback()
            self.connection.close()

        self.addCleanup(_cleanup)

    def test_create_run_assigns_identity_and_defaults(self) -> None:
        run = self.repository.create_run(
            tournament_slug="russia",
            requested_season_id="59",
        )

        self.assertIsNotNone(run.id)
        self.assertEqual("pending", run.status)
        self.assertEqual("manual", run.trigger_type)
        self.assertIsNotNone(run.started_at)
        self.assertIsNone(run.finished_at)

    def test_status_transition_sets_finished_at_for_terminal_state(self) -> None:
        run = self.repository.create_run(tournament_slug="russia")

        self.repository.mark_running(run)
        self.assertEqual("running", run.status)
        self.assertIsNone(run.finished_at)

        self.repository.mark_succeeded(run, report={"counts": {"players": 590}})
        self.assertEqual("succeeded", run.status)
        self.assertIsNotNone(run.finished_at)
        self.assertEqual({"counts": {"players": 590}}, run.report)

    def test_save_raw_response_is_idempotent(self) -> None:
        run = self.repository.create_run(tournament_slug="russia")
        payload = {"data": {"fantasyQueries": {"tournament": {"id": "1"}}}}

        first = self.repository.save_raw_response(
            run,
            operation_name="Tournament",
            variables={"id": "russia"},
            response=payload,
        )
        second = self.repository.save_raw_response(
            run,
            operation_name="Tournament",
            variables={"id": "russia"},
            response=payload,
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(1, self.repository.count_raw_responses(run))

    def test_save_raw_response_distinguishes_payloads(self) -> None:
        run = self.repository.create_run(tournament_slug="russia")

        self.repository.save_raw_response(
            run,
            operation_name="Players",
            variables={"pageNum": 1},
            response={"page": 1},
        )
        self.repository.save_raw_response(
            run,
            operation_name="Players",
            variables={"pageNum": 2},
            response={"page": 2},
        )

        self.assertEqual(2, self.repository.count_raw_responses(run))

    def test_invalid_status_is_rejected(self) -> None:
        run = self.repository.create_run(tournament_slug="russia")

        with self.assertRaises(ValueError):
            self.repository.update_status(run, "unknown")


@requires_database
class TransactionBoundaryTest(unittest.TestCase):
    """Verify commit persistence and rollback isolation end to end."""

    @classmethod
    def setUpClass(cls) -> None:
        migration.upgrade(TEST_DATABASE_URL)

    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        self.session_factory = create_session_factory(self.engine)
        self._created_run_ids: list[int] = []

        def _cleanup() -> None:
            if not self._created_run_ids:
                return
            with session_scope(self.session_factory) as session:
                for run_id in self._created_run_ids:
                    run = IngestionRepository(session).get_run(run_id)
                    if run is not None:
                        session.delete(run)

        self.addCleanup(_cleanup)

    def test_committed_run_and_raw_response_survive_new_session(self) -> None:
        with session_scope(self.session_factory) as session:
            repository = IngestionRepository(session)
            run = repository.create_run(tournament_slug="russia")
            repository.save_raw_response(
                run,
                operation_name="Season",
                variables={"seasonID": "59"},
                response={"data": {"season": {"id": "59"}}},
            )
            run_id = run.id
            self._created_run_ids.append(run_id)

        with session_scope(self.session_factory) as session:
            repository = IngestionRepository(session)
            reloaded = repository.get_run(run_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual("pending", reloaded.status)
            self.assertEqual(1, repository.count_raw_responses(reloaded))

    def test_failed_transaction_rolls_back(self) -> None:
        class InjectedError(RuntimeError):
            pass

        captured_run_id: dict[str, int] = {}
        with self.assertRaises(InjectedError):
            with session_scope(self.session_factory) as session:
                repository = IngestionRepository(session)
                run = repository.create_run(tournament_slug="russia")
                session.flush()
                captured_run_id["id"] = run.id
                raise InjectedError("boom")

        with session_scope(self.session_factory) as session:
            reloaded = IngestionRepository(session).get_run(captured_run_id["id"])
            self.assertIsNone(reloaded)


if __name__ == "__main__":
    unittest.main()
