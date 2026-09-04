"""Unit and integration tests for the user REST API (development-plan step 9).

The unit tests cover the request contracts, the unified error envelope, the
optimizer endpoints (with the heavy solver patched) and the generated OpenAPI
schema. The integration tests drive the FastAPI app against a real PostgreSQL
database: a small synthetic season is imported and published through the same
worker the admin API uses, forecasts are persisted, and then every read endpoint
is exercised end to end. They are skipped automatically when no database is
reachable.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

import pydantic
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from fantasy_analytics.api import api_error, create_app
from fantasy_analytics.api_schemas import (
    ImportSquadRequest,
    MAX_PAGE_LIMIT,
    SquadRequest,
    TransfersRequest,
)
from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
    session_scope,
)
from fantasy_analytics.db.models import FantasyTour, PlayerSeason, Season
from fantasy_analytics.forecast import MODEL_EVENT, run_forecast
from fantasy_analytics.ingestion_worker import execute_job
from fantasy_analytics.openapi_cli import build_openapi
from fantasy_analytics.optimizer import OptimizerError

# Reuse the ingestion fixture and fake client.
from test_ingestion import FakeClient, _build_fixture

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)

OFFLINE_DB_URL = "postgresql+psycopg://unused:unused@localhost:5432/unused"


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


# ---------------------------------------------------------------------------
# Unit tests (no database).
# ---------------------------------------------------------------------------
class RequestContractTest(unittest.TestCase):
    def test_squad_request_defaults(self) -> None:
        request = SquadRequest()
        self.assertEqual("poisson_events", request.model)
        self.assertIsNone(request.run_id)

    def test_transfers_request_requires_current_squad(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            TransfersRequest()

    def test_transfers_request_rejects_empty_squad(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            TransfersRequest(current_squad=[])

    def test_transfers_request_rejects_negative_max_transfers(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            TransfersRequest(current_squad=["111"], max_transfers=-1)

    def test_transfers_request_rejects_a_non_positive_budget(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            TransfersRequest(current_squad=["111"], budget=0)
        self.assertEqual(101.5, TransfersRequest(current_squad=["111"], budget=101.5).budget)
        self.assertIsNone(TransfersRequest(current_squad=["111"]).budget)

    def test_invalid_model_is_rejected(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            SquadRequest(model="not-a-model")

    def test_lock_and_formation_default_to_unconstrained(self) -> None:
        request = SquadRequest()
        self.assertEqual([], request.locked)
        self.assertEqual([], request.locked_starters)
        self.assertIsNone(request.formation)

    def test_formation_must_look_like_a_formation(self) -> None:
        self.assertEqual("4-4-2", SquadRequest(formation="4-4-2").formation)
        for bad in ("442", "4-4", "4-4-2-1", "four-4-2"):
            with self.assertRaises(pydantic.ValidationError):
                SquadRequest(formation=bad)

    def test_transfers_request_inherits_locks(self) -> None:
        request = TransfersRequest(
            current_squad=["111"], locked=["222"], formation="3-5-2"
        )
        self.assertEqual(["222"], request.locked)
        self.assertEqual("3-5-2", request.formation)


class ErrorHelperTest(unittest.TestCase):
    def test_api_error_wraps_detail(self) -> None:
        error = api_error(404, "gone")
        self.assertEqual(404, error.status_code)
        self.assertEqual("not_found", error.detail["type"])
        self.assertEqual("gone", error.detail["message"])

    def test_api_error_custom_type(self) -> None:
        error = api_error(422, "boom", type_="optimizer_error")
        self.assertEqual("optimizer_error", error.detail["type"])


class OfflineAppTest(unittest.TestCase):
    """Endpoints that do not need a database, with the solver patched."""

    def _client(self) -> TestClient:
        app = create_app(database_url=OFFLINE_DB_URL, spawn_worker=lambda job_id: None)
        return TestClient(app, raise_server_exceptions=False)

    def test_health(self) -> None:
        response = self._client().get("/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"status": "ok"}, response.json())

    def test_query_validation_uses_error_envelope(self) -> None:
        # limit above the maximum is rejected with the unified error shape.
        response = self._client().get(f"/players?season_id=1&limit={MAX_PAGE_LIMIT + 1}")
        self.assertEqual(422, response.status_code)
        body = response.json()
        self.assertEqual("validation_error", body["error"]["type"])
        self.assertIsInstance(body["error"]["details"], list)

    def test_optimizer_squad_success(self) -> None:
        canned = {
            "optimizer_version": "1.0.0",
            "model": "poisson_events",
            "mode": "squad",
            "generated_at": "2026-07-21T00:00:00+00:00",
            "run_id": 1,
            "season_id": 1,
            "season": {"name": "2025/2026"},
            "tour": {"tour_id": 5, "name": "5 тур"},
            "cutoff": "2025-11-08T11:00:00+00:00",
            "rules": {"total_budget": 100.0},
            "counts": {"candidates": 200},
            "solution": {"status": "OPTIMAL", "objective_expected_points": 76.3},
            "valid": True,
        }
        with mock.patch(
            "fantasy_analytics.api.build_squad_optimization", return_value=canned
        ) as builder:
            response = self._client().post("/optimizer/squad", json={"tour": "1786"})
        self.assertEqual(200, response.status_code)
        self.assertEqual(76.3, response.json()["solution"]["objective_expected_points"])
        # current_squad must be None in squad mode.
        self.assertIsNone(builder.call_args.kwargs["current_squad"])

    def test_optimizer_squad_forwards_locks_and_formation(self) -> None:
        with mock.patch(
            "fantasy_analytics.api.build_squad_optimization",
            return_value={
                "optimizer_version": "1.1.0",
                "model": "poisson_events",
                "mode": "squad",
                "generated_at": "2026-08-09T00:00:00+00:00",
                "run_id": 1,
                "season_id": 1,
                "season": {},
                "tour": {},
                "rules": {},
                "counts": {"locked": 2},
                "solution": {"status": "OPTIMAL"},
                "valid": True,
            },
        ) as builder:
            response = self._client().post(
                "/optimizer/squad",
                json={
                    "tour": "1786",
                    "locked": ["111", "222"],
                    "locked_starters": ["111"],
                    "formation": "3-5-2",
                },
            )
        self.assertEqual(200, response.status_code)
        kwargs = builder.call_args.kwargs
        self.assertEqual(["111", "222"], kwargs["locked"])
        self.assertEqual(["111"], kwargs["locked_starters"])
        self.assertEqual("3-5-2", kwargs["formation"])

    def test_optimizer_forwards_the_fixture_conflict_weight(self) -> None:
        with mock.patch(
            "fantasy_analytics.api.build_squad_optimization",
            return_value={
                "optimizer_version": "1.2.0",
                "model": "poisson_events",
                "mode": "squad",
                "generated_at": "2026-08-09T00:00:00+00:00",
                "run_id": 1,
                "season_id": 1,
                "season": {},
                "tour": {},
                "rules": {},
                "counts": {"clashes": 0},
                "solution": {"status": "OPTIMAL", "fixture_penalty": 0.0},
                "valid": True,
            },
        ) as builder:
            response = self._client().post(
                "/optimizer/squad",
                json={"tour": "1786", "fixture_conflict_weight": 0},
            )
        self.assertEqual(200, response.status_code)
        self.assertEqual(0.0, builder.call_args.kwargs["fixture_conflict_weight"])

    def test_optimizer_rejects_negative_fixture_conflict_weight(self) -> None:
        response = self._client().post(
            "/optimizer/squad",
            json={"tour": "1786", "fixture_conflict_weight": -1},
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("validation_error", response.json()["error"]["type"])

    def test_optimizer_defaults_the_fixture_conflict_weight_to_none(self) -> None:
        # Omitting the setting must let the optimizer apply its own default.
        with mock.patch(
            "fantasy_analytics.api.build_squad_optimization",
            return_value={
                "optimizer_version": "1.2.0",
                "model": "poisson_events",
                "mode": "squad",
                "generated_at": "2026-08-09T00:00:00+00:00",
                "run_id": 1,
                "season_id": 1,
                "season": {},
                "tour": {},
                "rules": {},
                "counts": {},
                "solution": {"status": "OPTIMAL"},
                "valid": True,
            },
        ) as builder:
            self._client().post("/optimizer/squad", json={"tour": "1786"})
        self.assertIsNone(builder.call_args.kwargs["fixture_conflict_weight"])

    def test_optimizer_rejects_malformed_formation(self) -> None:
        response = self._client().post(
            "/optimizer/squad", json={"tour": "1786", "formation": "4x4x2"}
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("validation_error", response.json()["error"]["type"])

    def test_optimizer_error_maps_to_422(self) -> None:
        with mock.patch(
            "fantasy_analytics.api.build_squad_optimization",
            side_effect=OptimizerError("infeasible"),
        ):
            response = self._client().post("/optimizer/squad", json={"tour": "1786"})
        self.assertEqual(422, response.status_code)
        body = response.json()
        self.assertEqual("optimizer_error", body["error"]["type"])
        self.assertIn("infeasible", body["error"]["message"])

    def test_optimizer_transfers_requires_squad(self) -> None:
        response = self._client().post("/optimizer/transfers", json={"tour": "1786"})
        self.assertEqual(422, response.status_code)
        self.assertEqual("validation_error", response.json()["error"]["type"])

    def test_optimizer_transfers_passes_the_squad_budget_through(self) -> None:
        # The imported team's own money (value plus bank) replaces the season's
        # opening budget; omitting it leaves the optimizer on the season's.
        stub = {
            "optimizer_version": "1.6.0",
            "model": "poisson_events",
            "mode": "transfers",
            "generated_at": "2026-09-04T00:00:00+00:00",
            "run_id": 1,
            "season_id": 1,
            "season": {},
            "tour": {},
            "rules": {},
            "counts": {},
            "solution": {"status": "OPTIMAL"},
            "valid": True,
        }
        with mock.patch(
            "fantasy_analytics.api.build_squad_optimization", return_value=stub
        ) as builder:
            response = self._client().post(
                "/optimizer/transfers",
                json={"tour": "1786", "current_squad": ["111"], "budget": 101.5},
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual(101.5, builder.call_args.kwargs["budget"])
            self._client().post(
                "/optimizer/transfers", json={"tour": "1786", "current_squad": ["111"]}
            )
            self.assertIsNone(builder.call_args.kwargs["budget"])

    def test_import_squad_rejects_an_invalid_url_without_a_database(self) -> None:
        response = self._client().post(
            "/squads/import",
            json={
                "url": "https://example.com/not-a-team",
                "season_id": 1,
                "competition": "portugal",
            },
        )
        self.assertEqual(400, response.status_code)
        self.assertEqual("invalid_squad_url", response.json()["error"]["type"])

    def test_import_squad_rejects_a_league_mismatch_from_the_url(self) -> None:
        response = self._client().post(
            "/squads/import",
            json={
                "url": "https://www.sports.ru/fantasy/football/portugal/588960/",
                "season_id": 1,
                "competition": "russia",
            },
        )
        self.assertEqual(409, response.status_code)
        body = response.json()
        self.assertEqual("league_mismatch", body["error"]["type"])
        self.assertIn("portugal", body["error"]["message"])

    def test_import_squad_request_requires_a_url(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            ImportSquadRequest(season_id=1)


class OpenApiTest(unittest.TestCase):
    def test_schema_documents_read_and_optimizer_paths(self) -> None:
        schema = build_openapi()
        paths = schema["paths"]
        for path in (
            "/seasons",
            "/seasons/{season_id}",
            "/tours",
            "/matches",
            "/players",
            "/players/{player_season_id}",
            "/optimizer/squad",
            "/optimizer/transfers",
            "/squads/import",
        ):
            self.assertIn(path, paths)
        # Request/response models are materialised as components.
        schemas = schema["components"]["schemas"]
        self.assertIn("PlayerListResponse", schemas)
        self.assertIn("TransfersRequest", schemas)
        self.assertIn("ImportSquadRequest", schemas)
        self.assertIn("ImportSquadResponse", schemas)


# ---------------------------------------------------------------------------
# Integration tests (real database).
# ---------------------------------------------------------------------------
@requires_database
class ReadApiIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)

        # Import and publish a snapshot through the real worker path.
        job_id = self._enqueue_and_run()
        self.job_id = job_id

        # Persist forecasts for the (finished) tour so projections are available.
        with session_scope(self.session_factory) as session:
            self.season_id = session.execute(select(Season.id)).scalar_one()
            self.tour = session.execute(select(FantasyTour)).scalars().first()
            self.tour_id = self.tour.id
            self.fantasy_tour_id = self.tour.fantasy_tour_id
            self.player_ids = list(
                session.execute(select(PlayerSeason.id).order_by(PlayerSeason.id))
                .scalars()
                .all()
            )
        run_forecast(
            self.session_factory,
            tour_ref=self.fantasy_tour_id,
            persist=True,
        )

        self.client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
            ),
            raise_server_exceptions=False,
        )

    def _enqueue_and_run(self) -> int:
        def spawn(job_id: int) -> None:
            execute_job(
                self.engine,
                self.session_factory,
                job_id,
                client=FakeClient(_build_fixture()),
            )

        app = create_app(session_factory=self.session_factory, spawn_worker=spawn)
        client = TestClient(app)
        response = client.post("/admin/ingestion/rpl/refresh")
        job_id = response.json()["id"]
        status = client.get(f"/admin/ingestion/runs/{job_id}").json()["status"]
        self.assertEqual("succeeded", status)
        return job_id

    def test_seasons_expose_snapshot_time(self) -> None:
        response = self.client.get("/seasons")
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(1, body["pagination"]["total"])
        season = body["items"][0]
        self.assertEqual("2025/2026", season["name"])
        self.assertIsNotNone(season["snapshot"])
        self.assertIsNotNone(season["snapshot"]["data_freshness"])

    def test_season_detail_includes_rules(self) -> None:
        response = self.client.get(f"/seasons/{self.season_id}")
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertIsNotNone(body["rules"])
        self.assertEqual(15, body["rules"]["total_players"])

    def test_unknown_season_returns_404_envelope(self) -> None:
        response = self.client.get("/seasons/999999")
        self.assertEqual(404, response.status_code)
        self.assertEqual("not_found", response.json()["error"]["type"])

    def test_tours_filter_by_status(self) -> None:
        response = self.client.get(f"/tours?season_id={self.season_id}&status=FINISHED")
        self.assertEqual(200, response.status_code)
        items = response.json()["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("FINISHED", items[0]["status"])

        empty = self.client.get(f"/tours?season_id={self.season_id}&status=OPENED")
        self.assertEqual(0, len(empty.json()["items"]))

    def test_matches_filter_by_club(self) -> None:
        all_matches = self.client.get(f"/matches?season_id={self.season_id}").json()
        self.assertEqual(1, all_matches["pagination"]["total"])
        match = all_matches["items"][0]
        self.assertEqual(2, match["home_score"])
        self.assertEqual(0, match["away_score"])

        by_club = self.client.get(
            f"/matches?club_id={match['home_club_id']}"
        ).json()
        self.assertEqual(1, by_club["pagination"]["total"])

    def test_match_detail_and_404(self) -> None:
        match_id = self.client.get(f"/matches?season_id={self.season_id}").json()[
            "items"
        ][0]["match_id"]
        found = self.client.get(f"/matches/{match_id}")
        self.assertEqual(200, found.status_code)
        self.assertIsNotNone(found.json()["home_club_name"])
        self.assertEqual(404, self.client.get("/matches/999999").status_code)

    def test_players_list_carries_snapshot_and_projection(self) -> None:
        response = self.client.get(
            f"/players?season_id={self.season_id}&tour_id={self.tour_id}"
            f"&model={MODEL_EVENT}"
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertIsNotNone(body["snapshot"])
        self.assertEqual(2, body["pagination"]["total"])
        # Every player has a price from the snapshot and a joined projection.
        for player in body["items"]:
            self.assertIsNotNone(player["price"])
            self.assertIsNotNone(player["projection"])
            self.assertEqual(MODEL_EVENT, player["projection"]["model_name"])
            self.assertIn("components", player["projection"])
            # The projection exposes its cross-season provenance (step 14).
            self.assertEqual(
                "current_season", player["projection"]["stat_source"]
            )
            self.assertIn("has_history", player["projection"])

    def test_players_filter_by_role(self) -> None:
        response = self.client.get(
            f"/players?season_id={self.season_id}&role=GOALKEEPER"
        )
        items = response.json()["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("GOALKEEPER", items[0]["role"])

    def test_players_filter_by_price_range(self) -> None:
        response = self.client.get(
            f"/players?season_id={self.season_id}&min_price=9"
        )
        items = response.json()["items"]
        self.assertEqual(1, len(items))
        self.assertGreaterEqual(items[0]["price"], 9)

    def test_players_pagination_is_bounded_and_stable(self) -> None:
        first = self.client.get(
            f"/players?season_id={self.season_id}&limit=1&offset=0&order=name"
        ).json()
        second = self.client.get(
            f"/players?season_id={self.season_id}&limit=1&offset=1&order=name"
        ).json()
        self.assertEqual(1, first["pagination"]["count"])
        self.assertEqual(2, first["pagination"]["total"])
        self.assertNotEqual(
            first["items"][0]["player_season_id"],
            second["items"][0]["player_season_id"],
        )

    def test_player_detail_has_history_and_projection(self) -> None:
        player_id = self.player_ids[0]
        response = self.client.get(
            f"/players/{player_id}?tour_id={self.tour_id}&model={MODEL_EVENT}"
        )
        self.assertEqual(200, response.status_code)
        body = response.json()
        self.assertEqual(player_id, body["player_season_id"])
        self.assertIsNotNone(body["snapshot"])
        self.assertIsNotNone(body["projection"])
        self.assertIsInstance(body["history"], list)
        self.assertGreaterEqual(len(body["history"]), 1)

    def test_player_detail_404(self) -> None:
        response = self.client.get("/players/999999")
        self.assertEqual(404, response.status_code)
        self.assertEqual("not_found", response.json()["error"]["type"])

    def test_optimizer_infeasible_returns_error_envelope(self) -> None:
        # The tiny fixture cannot fill a 15-player squad, so the real solver
        # reports an infeasible problem through the unified error envelope.
        response = self.client.post(
            "/optimizer/squad", json={"tour": self.fantasy_tour_id}
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual("optimizer_error", response.json()["error"]["type"])

    def test_import_squad_resolves_fixture_players(self) -> None:
        def fetch_squad(_squad_id: str) -> dict:
            return {
                "data": {
                    "fantasyQueries": {
                        "squads": [
                            {
                                "id": "9",
                                "name": "Тест",
                                "season": {
                                    "id": "59",
                                    "isActive": False,
                                    "tournament": {
                                        "id": "1",
                                        "webName": "russia",
                                        "name": "Россия",
                                    },
                                },
                                "currentTourInfo": {
                                    "tour": {
                                        "id": self.fantasy_tour_id,
                                        "name": "1 тур",
                                        "status": "FINISHED",
                                    },
                                    "players": [
                                        {
                                            "isCaptain": True,
                                            "isViceCaptain": False,
                                            "isStarting": True,
                                            "substitutePriority": None,
                                            "seasonPlayer": {
                                                "id": "111",
                                                "name": "Игрок Один",
                                                "role": "GOALKEEPER",
                                            },
                                        },
                                        {
                                            "isCaptain": False,
                                            "isViceCaptain": False,
                                            "isStarting": True,
                                            "substitutePriority": None,
                                            "seasonPlayer": {
                                                "id": "222",
                                                "name": "Игрок Два",
                                                "role": "FORWARD",
                                            },
                                        },
                                        {
                                            "isCaptain": False,
                                            "isViceCaptain": False,
                                            "isStarting": False,
                                            "substitutePriority": 1,
                                            "seasonPlayer": {
                                                "id": "missing",
                                                "name": "Ушёл",
                                                "role": "MIDFIELDER",
                                            },
                                        },
                                    ],
                                },
                            }
                        ]
                    }
                }
            }

        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
                fetch_squad=fetch_squad,
                fetch_league=lambda _id: {
                    "data": {"fantasyQueries": {"league": None}}
                },
            ),
            raise_server_exceptions=False,
        )
        response = client.post(
            "/squads/import",
            json={
                "url": "https://www.sports.ru/fantasy/football/russia/9/",
                "season_id": self.season_id,
                "tour_id": self.tour_id,
                "competition": "russia",
                "model": MODEL_EVENT,
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        body = response.json()
        self.assertEqual("Тест", body["squad_name"])
        self.assertEqual(
            ["111", "222"],
            [player["fantasy_player_id"] for player in body["players"]],
        )
        self.assertEqual(
            ["missing"],
            [player["fantasy_player_id"] for player in body["missing"]],
        )
        self.assertIsNotNone(body["players"][0].get("projection"))


if __name__ == "__main__":
    unittest.main()
