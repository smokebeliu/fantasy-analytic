"""Unit and integration tests for multi-league support (development-plan step 22).

The unit tests cover the catalogue: turning a ``tournamentsList`` response into
competition refs, labelling seasons uniquely inside a competition (the Champions
League publishes two seasons per year with the same name) and picking the season a
default import would target.

The integration tests build a two-league database — one league imported and
published, one only catalogued — and prove the properties multi-league support
rests on:

* ``/competitions`` separates a league the read API can serve from one that has
  only been catalogued;
* the admin refresh and status endpoints are scoped by tournament slug, so two
  leagues can be refreshed at the same time while one league still cannot;
* ``rpl`` remains an alias for the ``russia`` slug the original endpoints used;
* a season resolves to the right league when the caller names the competition,
  which is what stops the optimizer answering for the wrong one; and
* two seasons may share a stat season id, which the Champions League requires.

They are skipped automatically when no database is reachable.
"""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient
from sqlalchemy import text

from fantasy_analytics.api import create_app
from fantasy_analytics.competitions import (
    CompetitionCatalogueError,
    catalogue_seasons,
    normalize_catalogue,
    persist_catalogue,
    season_labels,
)
from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
    session_scope,
)
from fantasy_analytics.features import FeaturesError, resolve_run
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.quality import run_quality_checks
from fantasy_analytics.read_repository import ReadRepository

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


def _season(fantasy_id: str, stat_id: str, name: str, active: bool = False) -> dict:
    return {
        "id": fantasy_id,
        "isActive": active,
        "statObject": {"id": stat_id, "name": name},
    }


def _catalogue_payload() -> dict:
    return {
        "data": {
            "fantasyQueries": {
                "tournamentsList": [
                    {
                        "id": "1",
                        "name": "Россия",
                        "webName": "russia",
                        "currentSeason": {"id": "75"},
                        "seasons": [
                            _season("75", "rfpl_26-27", "2026/2027", active=True),
                            _season("59", "rfpl_25-26", "2025/2026"),
                        ],
                    },
                    {
                        "id": "15",
                        "name": "Италия",
                        "webName": "italy",
                        "currentSeason": None,
                        "seasons": [_season("69", "serie_a_25-26", "2025/2026")],
                    },
                    {
                        # Two fantasy seasons for one stat season: the league
                        # phase and the knockout stage.
                        "id": "17",
                        "name": "Лига чемпионов",
                        "webName": "champions-league",
                        "currentSeason": None,
                        "seasons": [
                            _season("70", "champions_league_25-26", "2025/2026"),
                            _season("72", "champions_league_25-26", "2025/2026"),
                        ],
                    },
                ]
            }
        }
    }


class SeasonLabelTest(unittest.TestCase):
    def test_unique_names_are_left_alone(self) -> None:
        seasons = [
            _season("59", "rfpl_25-26", "2025/2026"),
            _season("75", "rfpl_26-27", "2026/2027"),
        ]
        self.assertEqual(["2025/2026", "2026/2027"], season_labels(seasons))

    def test_repeated_name_gains_the_fantasy_id(self) -> None:
        # Both Champions League seasons are called 2025/2026, so the label has to
        # carry the only identifier that differs.
        seasons = [
            _season("70", "champions_league_25-26", "2025/2026"),
            _season("72", "champions_league_25-26", "2025/2026"),
        ]
        self.assertEqual(["2025/2026 (#70)", "2025/2026 (#72)"], season_labels(seasons))

    def test_nameless_season_falls_back_to_its_id(self) -> None:
        self.assertEqual(["сезон 5"], season_labels([{"id": "5"}]))


class CatalogueSeasonsTest(unittest.TestCase):
    def test_seasons_are_ordered_oldest_first(self) -> None:
        seasons = catalogue_seasons(
            {
                "seasons": [
                    _season("75", "rfpl_26-27", "2026/2027", active=True),
                    _season("59", "rfpl_25-26", "2025/2026"),
                ]
            }
        )
        self.assertEqual(["59", "75"], [s.fantasy_season_id for s in seasons])
        self.assertTrue(seasons[-1].is_active)

    def test_seasons_without_an_id_are_dropped(self) -> None:
        self.assertEqual([], catalogue_seasons({"seasons": [{"statObject": {}}]}))
        self.assertEqual([], catalogue_seasons({}))


class NormalizeCatalogueTest(unittest.TestCase):
    def test_competitions_keep_the_site_order(self) -> None:
        refs = normalize_catalogue(_catalogue_payload())

        self.assertEqual(
            ["russia", "italy", "champions-league"], [ref.slug for ref in refs]
        )
        self.assertEqual([0, 1, 2], [ref.sort_order for ref in refs])
        self.assertEqual("1", refs[0].fantasy_tournament_id)
        self.assertEqual("75", refs[0].current_season_id)

    def test_active_season_is_reported_per_league(self) -> None:
        refs = {ref.slug: ref for ref in normalize_catalogue(_catalogue_payload())}

        self.assertTrue(refs["russia"].has_active_season)
        # Serie A's newest season is already finished, so "refresh the active
        # season" has nothing to target.
        self.assertFalse(refs["italy"].has_active_season)

    def test_latest_season_prefers_the_newest_completed_one(self) -> None:
        refs = {ref.slug: ref for ref in normalize_catalogue(_catalogue_payload())}

        # A season in progress carries partial statistics, so a default import
        # targets the newest finished one instead.
        self.assertEqual("59", refs["russia"].latest_season.fantasy_season_id)
        self.assertEqual("69", refs["italy"].latest_season.fantasy_season_id)

    def test_empty_catalogue_is_an_error(self) -> None:
        with self.assertRaises(CompetitionCatalogueError):
            normalize_catalogue({"data": {"fantasyQueries": {"tournamentsList": []}}})


@requires_database
class MultiLeagueIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)

        # One league imported and published, the rest merely catalogued.
        report = run_ingestion(
            FakeClient(_consistent_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        self.run_id = report["run_id"]
        run_quality_checks(self.session_factory, run_id=self.run_id)
        with session_scope(self.session_factory) as session:
            persist_catalogue(session, normalize_catalogue(_catalogue_payload()))

    def _exec(self, sql: str, **params):
        with self.engine.begin() as connection:
            return connection.execute(text(sql), params)

    def _scalar(self, sql: str, **params):
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar_one()

    def _client(self) -> TestClient:
        return TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
                ensure_forecasts=lambda *a, **k: 0,
            )
        )

    def test_competitions_separate_imported_from_catalogued(self) -> None:
        client = self._client()

        every = client.get("/competitions").json()["items"]
        self.assertEqual(
            ["russia", "italy", "champions-league"],
            [item["slug"] for item in every],
        )
        by_slug = {item["slug"]: item for item in every}
        self.assertTrue(by_slug["russia"]["is_imported"])
        self.assertIsNotNone(by_slug["russia"]["snapshot"])
        # Catalogued but never imported: offerable for import, not readable.
        self.assertFalse(by_slug["italy"]["is_imported"])
        self.assertIsNone(by_slug["italy"]["snapshot"])
        self.assertEqual([], by_slug["italy"]["seasons"])
        self.assertEqual(
            ["69"],
            [s["fantasy_season_id"] for s in by_slug["italy"]["available_seasons"]],
        )

        imported = client.get("/competitions?imported_only=true").json()["items"]
        self.assertEqual(["russia"], [item["slug"] for item in imported])

    def test_unknown_competition_is_404(self) -> None:
        response = self._client().get("/competitions/atlantis")

        self.assertEqual(404, response.status_code)
        self.assertEqual("not_found", response.json()["error"]["type"])

    def test_refresh_is_scoped_to_one_league(self) -> None:
        spawned: list[int] = []
        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=spawned.append,
            )
        )

        first = client.post("/admin/ingestion/russia/refresh")
        self.assertEqual(202, first.status_code)
        self.assertEqual("russia", first.json()["tournament_slug"])

        # A second refresh of the *same* league still conflicts...
        self.assertEqual(409, client.post("/admin/ingestion/russia/refresh").status_code)

        # ...but another league is unaffected, so leagues import in parallel.
        italy = client.post("/admin/ingestion/italy/refresh")
        self.assertEqual(202, italy.status_code)
        self.assertEqual("italy", italy.json()["tournament_slug"])
        self.assertEqual([first.json()["id"], italy.json()["id"]], spawned)

    def test_rpl_path_remains_an_alias_for_russia(self) -> None:
        client = self._client()

        refresh = client.post("/admin/ingestion/rpl/refresh")
        self.assertEqual(202, refresh.status_code)
        self.assertEqual("russia", refresh.json()["tournament_slug"])

        status = client.get("/admin/ingestion/rpl/status").json()
        self.assertEqual("russia", status["tournament_slug"])
        self.assertEqual("russia", status["competition"]["slug"])

    def test_unknown_refresh_slug_lists_the_known_ones(self) -> None:
        response = self._client().post("/admin/ingestion/atlantis/refresh")

        self.assertEqual(404, response.status_code)
        error = response.json()["error"]
        self.assertIn("russia", error["details"]["known_slugs"])

    def test_status_reports_only_its_own_league(self) -> None:
        client = self._client()

        russia = client.get("/admin/ingestion/russia/status").json()
        self.assertEqual("2025/2026", russia["season"]["name"])
        self.assertIsNotNone(russia["snapshot"])

        # Serie A has been catalogued but never imported, so it must not inherit
        # the Russian snapshot the way a globally-resolved status would.
        italy = client.get("/admin/ingestion/italy/status").json()
        self.assertEqual("italy", italy["tournament_slug"])
        self.assertIsNone(italy["season"])
        self.assertIsNone(italy["snapshot"])
        self.assertIsNone(italy["target_tour"])
        self.assertFalse(italy["is_refreshing"])

    def test_catalogue_sync_endpoint_reports_what_it_stored(self) -> None:
        def fake_sync(session_factory):
            with session_scope(session_factory) as session:
                return persist_catalogue(
                    session, normalize_catalogue(_catalogue_payload())
                )

        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
                sync_competitions=fake_sync,
            )
        )

        response = client.post("/admin/competitions/sync")

        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(3, body["competitions"])
        self.assertEqual(5, body["seasons"])
        self.assertIn("champions-league", body["slugs"])

    def _add_phase_seasons(self, stat_season_ids: tuple[str, str]) -> int:
        """Add the two same-named seasons a knockout tournament publishes."""
        competition_id = self._scalar(
            "SELECT id FROM competitions WHERE slug = 'champions-league'"
        )
        for fantasy_id, stat_id in zip(("70", "72"), stat_season_ids, strict=True):
            self._exec(
                """
                INSERT INTO seasons
                    (competition_id, fantasy_season_id, stat_season_id, name,
                     is_active, starts_at)
                VALUES (:c, :f, :stat, '2025/2026', false,
                        '2025-09-01T00:00:00Z')
                """,
                c=competition_id,
                f=fantasy_id,
                stat=stat_id,
            )
        return competition_id

    def test_seasons_can_be_filtered_and_labelled_per_league(self) -> None:
        competition_id = self._add_phase_seasons(
            ("champions_league_25-26_league", "champions_league_25-26_playoff")
        )

        client = self._client()
        scoped = client.get(f"/seasons?competition_id={competition_id}").json()["items"]

        self.assertEqual({"70", "72"}, {s["fantasy_season_id"] for s in scoped})
        # Both are named 2025/2026, so the labels must still tell them apart.
        self.assertEqual(
            {"2025/2026 (#70)", "2025/2026 (#72)"}, {s["label"] for s in scoped}
        )
        self.assertEqual({"champions-league"}, {s["competition_slug"] for s in scoped})

        # The published Russian season keeps its plain label.
        everything = client.get("/seasons").json()["items"]
        russia = next(s for s in everything if s["competition_slug"] == "russia")
        self.assertEqual("2025/2026", russia["label"])

    def test_two_seasons_may_share_one_stat_season(self) -> None:
        # The Champions League's league phase and knockout stage are separate
        # fantasy seasons pointing at a single stat season, which the UNIQUE
        # constraint dropped in revision 0007 used to forbid.
        #
        # That revision's downgrade refuses to restore the constraint over these
        # rows, and the shared reset in setUp downgrades to base, so they are
        # removed again however this test ends.
        self.addCleanup(
            self._exec,
            "DELETE FROM seasons WHERE stat_season_id = 'champions_league_25-26'",
        )
        competition_id = self._add_phase_seasons(
            ("champions_league_25-26", "champions_league_25-26")
        )

        stored = self._scalar(
            "SELECT count(*) FROM seasons WHERE competition_id = :c "
            "AND stat_season_id = 'champions_league_25-26'",
            c=competition_id,
        )
        self.assertEqual(2, stored)

    def test_run_resolves_to_the_named_league(self) -> None:
        with session_scope(self.session_factory) as session:
            resolved = resolve_run(session, None, None, "russia")
            self.assertEqual(self.run_id, resolved.id)

            # Naming a league with no published snapshot must fail loudly rather
            # than silently fall back to another league's data.
            with self.assertRaises(FeaturesError) as caught:
                resolve_run(session, None, None, "italy")
            self.assertIn("italy", str(caught.exception))

    def test_list_competitions_can_skip_unimported_leagues(self) -> None:
        with session_scope(self.session_factory) as session:
            repo = ReadRepository(session)
            self.assertEqual(3, len(repo.list_competitions()))
            imported = repo.list_competitions(imported_only=True)
        self.assertEqual(["russia"], [item["slug"] for item in imported])


if __name__ == "__main__":
    unittest.main()
