"""Unit and integration tests for the full historical import (step 2).

The unit tests exercise the pure helpers and the fetch stage through an
in-memory fake GraphQL client. The integration tests replay a small synthetic
season into a real PostgreSQL database to verify idempotency and the atomic
"no partial snapshot on failure" guarantee. They are skipped automatically when
no database is reachable via ``TEST_DATABASE_URL``/``DATABASE_URL``.
"""

from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text

from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.ingestion import (
    IngestionOptions,
    _map_stats,
    _parse_dt,
    _to_decimal,
    _to_int,
    fetch_all,
    run_ingestion,
)
from fantasy_analytics.queries import (
    PLAYERS_QUERY,
    PLAYER_HISTORY_QUERY,
    SEASON_QUERY,
    TOURNAMENT_QUERY,
)
from fantasy_analytics.quality import run_quality_checks

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


def _game_stat(minutes: int, points: int) -> dict:
    return {
        "points": points,
        "goals": 1,
        "assists": 2,
        "saves": 0,
        "penaltiesMissed": 0,
        "penaltiesPost": 0,
        "penaltiesTarget": 0,
        "penaltiesSaved": 0,
        "fieldMinutes": minutes,
        "yellowCards": 1,
        "redCards": 0,
        "goalsConceded": 0,
        "penaltyGoalsConceded": 0,
        "penaltiesFaced": 0,
        "penaltyConceded": 0,
        "ownGoals": 0,
        "ballRecovery": 5,
    }


def _build_fixture() -> dict:
    """Return a small but complete synthetic season across all query shapes."""
    tournament = {
        "data": {
            "fantasyQueries": {
                "tournament": {
                    "id": "1",
                    "name": "Россия",
                    "webName": "russia",
                    "seasons": [
                        {
                            "id": "75",
                            "isActive": True,
                            "statObject": {"id": "rfpl_26-27", "name": "2026/2027"},
                        },
                        {
                            "id": "59",
                            "isActive": False,
                            "statObject": {"id": "rfpl_25-26", "name": "2025/2026"},
                        },
                    ],
                }
            }
        }
    }

    season = {
        "data": {
            "fantasyQueries": {
                "season": {
                    "id": "59",
                    "isActive": False,
                    "rules": "<p>rules</p>",
                    "statObject": {"id": "rfpl_25-26", "name": "2025/2026"},
                    "info": {
                        "playerPrices": [],
                        "constraints": {
                            "totalBalance": 100,
                            "totalPlayersCount": 15,
                            "activePlayersCount": 11,
                            "fullRoster": [
                                {"role": "GOALKEEPER", "minCount": 2, "maxCount": 2}
                            ],
                            "startingRoster": [
                                {"role": "GOALKEEPER", "minCount": 1, "maxCount": 1}
                            ],
                        },
                        "teams": [
                            {
                                "id": "10",
                                "name": "Клуб A",
                                "statObject": {"id": "club_a", "name": "Клуб A"},
                            },
                            {
                                "id": "20",
                                "name": "Клуб B",
                                "statObject": {"id": "club_b", "name": "Клуб B"},
                            },
                        ],
                    },
                    "tours": [
                        {
                            "id": "1772",
                            "name": "1 тур",
                            "status": "FINISHED",
                            "startedAt": "2025-07-18T17:30:00Z",
                            "finishedAt": "2025-07-21T17:30:00Z",
                            "transfersStartedAt": "2025-07-11T00:00:00Z",
                            "transfersFinishedAt": "2025-07-18T17:50:00Z",
                            "constraints": {
                                "totalTransfers": 3,
                                "maxSameTeamPlayers": 3,
                            },
                            "matches": [
                                {
                                    "id": "900001",
                                    "scheduledAt": "2025-07-18T17:30:00Z",
                                    "matchStatus": "CLOSED",
                                    "home": {
                                        "score": 2,
                                        "team": {"id": "club_a", "name": "Клуб A"},
                                    },
                                    "away": {
                                        "score": 0,
                                        "team": {"id": "club_b", "name": "Клуб B"},
                                    },
                                }
                            ],
                        }
                    ],
                }
            }
        }
    }

    players = {
        "data": {
            "fantasyQueries": {
                "players": {
                    "pageInfo": {
                        "currentPage": 1,
                        "firstPage": 1,
                        "lastPage": 1,
                        "totalCount": 2,
                        "hasNextPage": False,
                    },
                    "list": [
                        {
                            "id": "111",
                            "name": "Игрок Один",
                            "price": 10,
                            "role": "FORWARD",
                            "statObject": {"id": "p_one", "name": "Игрок Один"},
                            "team": {
                                "id": "10",
                                "name": "Клуб A",
                                "statObject": {"id": "club_a", "name": "Клуб A"},
                            },
                            "status": {
                                "status": "FIT",
                                "description": "",
                                "selectedBy": 5.5,
                                "form": 20,
                            },
                            "seasonScoreInfo": {
                                "place": 1,
                                "score": 100,
                                "averageScore": 5.0,
                                "scoreForLastTour": 3,
                                "topPercent": None,
                            },
                            "gameStat": _game_stat(900, 100),
                        },
                        {
                            "id": "222",
                            "name": "Игрок Два",
                            "price": 8,
                            "role": "GOALKEEPER",
                            "statObject": {"id": "p_two", "name": "Игрок Два"},
                            "team": {
                                "id": "20",
                                "name": "Клуб B",
                                "statObject": {"id": "club_b", "name": "Клуб B"},
                            },
                            "status": {
                                "status": "FIT",
                                "description": "",
                                "selectedBy": 3.25,
                                "form": 10,
                            },
                            "seasonScoreInfo": {
                                "place": 2,
                                "score": 80,
                                "averageScore": 4.0,
                                "scoreForLastTour": 2,
                                "topPercent": 12.5,
                            },
                            "gameStat": _game_stat(900, 80),
                        },
                    ],
                }
            }
        }
    }

    team_stats = {
        "data": {
            "stat_season": [
                {
                    "id": "rfpl_25-26",
                    "name": "2025/2026",
                    "startedAt": "2025-07-18T00:00:00Z",
                    "endedAt": "2026-05-30T00:00:00Z",
                    "team0": {
                        "MatchesPlayed": 1,
                        "MatchesWon": 1,
                        "MatchesDrawn": 0,
                        "MatchesLost": 0,
                        "GoalsScored": 2,
                        "GoalsConceded": 0,
                        "YellowCards": 1,
                        "RedCards": 0,
                    },
                    "team1": {
                        "MatchesPlayed": 1,
                        "MatchesWon": 0,
                        "MatchesDrawn": 0,
                        "MatchesLost": 1,
                        "GoalsScored": 0,
                        "GoalsConceded": 2,
                        "YellowCards": 0,
                        "RedCards": 0,
                    },
                }
            ]
        }
    }

    def history(player_fantasy_id: str, team_id: str, stat_team: str, details):
        return {
            "data": {
                "fantasyQueries": {
                    "season": {
                        "players": {
                            "list": [
                                {
                                    "matches": {
                                        "pageInfo": {
                                            "currentPage": 1,
                                            "lastPage": 1,
                                            "totalCount": 1,
                                            "hasNextPage": False,
                                        },
                                        "matches": [
                                            {
                                                "match": {
                                                    "id": "900001",
                                                    "scheduledAt": (
                                                        "2025-07-18T17:30:00Z"
                                                    ),
                                                },
                                                "team": {
                                                    "id": team_id,
                                                    "name": "Клуб",
                                                    "statObject": {
                                                        "id": stat_team,
                                                        "name": "Клуб",
                                                    },
                                                },
                                                "tour": {
                                                    "id": "1772",
                                                    "name": "1 тур",
                                                    "status": "FINISHED",
                                                },
                                                "playerMatchInfo": _game_stat(78, 12),
                                                "statDetails": details,
                                            }
                                        ],
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        }

    histories = {
        "111": history("111", "10", "club_a", []),
        "222": history(
            "222", "20", "club_b", [{"score": 4, "reason": "CLEAN_SHEET"}]
        ),
    }

    return {
        "tournament": tournament,
        "season": season,
        "players": players,
        "team_stats": team_stats,
        "histories": histories,
    }


class FakeClient:
    """Route GraphQL calls to canned payloads keyed by the query constant."""

    def __init__(self, fixture: dict, fail_on_history: bool = False) -> None:
        self._fixture = copy.deepcopy(fixture)
        self._fail_on_history = fail_on_history
        self.calls: list[str] = []

    def execute(self, query: str, variables=None) -> dict:
        variables = dict(variables or {})
        if query is TOURNAMENT_QUERY:
            self.calls.append("Tournament")
            return copy.deepcopy(self._fixture["tournament"])
        if query is SEASON_QUERY:
            self.calls.append("Season")
            return copy.deepcopy(self._fixture["season"])
        if query is PLAYERS_QUERY:
            self.calls.append("Players")
            return copy.deepcopy(self._fixture["players"])
        if query is PLAYER_HISTORY_QUERY:
            self.calls.append("PlayerHistory")
            if self._fail_on_history:
                raise RuntimeError("simulated history failure")
            return copy.deepcopy(self._fixture["histories"][variables["playerID"]])
        if "stat_season" in query:
            self.calls.append("TeamSeasonStats")
            return copy.deepcopy(self._fixture["team_stats"])
        raise AssertionError(f"Unexpected query: {query[:40]!r}")


class HelperTest(unittest.TestCase):
    def test_map_stats_defaults_missing_to_zero(self) -> None:
        mapped = _map_stats({"points": 12, "ballRecovery": 3})

        self.assertEqual(12, mapped["points"])
        self.assertEqual(3, mapped["ball_recoveries"])
        self.assertEqual(0, mapped["goals"])
        self.assertEqual(0, mapped["field_minutes"])

    def test_map_stats_handles_none(self) -> None:
        self.assertEqual(0, _map_stats(None)["points"])

    def test_parse_dt_normalizes_z_suffix(self) -> None:
        parsed = _parse_dt("2025-07-18T17:30:00Z")

        self.assertEqual(
            datetime(2025, 7, 18, 17, 30, tzinfo=timezone.utc), parsed
        )
        self.assertIsNone(_parse_dt(None))
        self.assertIsNone(_parse_dt(""))

    def test_numeric_coercion(self) -> None:
        self.assertEqual(Decimal("5.5"), _to_decimal(5.5))
        self.assertIsNone(_to_decimal(None))
        self.assertEqual(7, _to_int("7"))
        self.assertIsNone(_to_int(None))


class FetchStageTest(unittest.TestCase):
    def test_fetch_selects_latest_completed_season_and_all_players(self) -> None:
        client = FakeClient(_build_fixture())

        result = fetch_all(client, IngestionOptions(history_workers=2))

        self.assertEqual("59", str(result.season["id"]))
        self.assertEqual(2, len(result.players))
        self.assertEqual(2, len(result.histories))
        self.assertEqual(2, len(result.stat_teams))
        operations = {name for name, _, _ in result.raw_payloads}
        self.assertEqual(
            {"Tournament", "Season", "Players", "TeamSeasonStats", "PlayerHistory"},
            operations,
        )
        self.assertIn("histories", result.timings)

    def test_fetch_skips_history_for_players_without_minutes(self) -> None:
        fixture = _build_fixture()
        fixture["players"]["data"]["fantasyQueries"]["players"]["list"][1][
            "gameStat"
        ]["fieldMinutes"] = 0
        client = FakeClient(fixture)

        result = fetch_all(client, IngestionOptions(history_workers=1))

        self.assertEqual(["111"], list(result.histories.keys()))


@requires_database
class IngestionIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)

    def _count(self, table: str) -> int:
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table}")
                ).scalar_one()
            )

    def test_full_import_persists_expected_entities(self) -> None:
        client = FakeClient(_build_fixture())

        report = run_ingestion(
            client, self.session_factory, IngestionOptions(history_workers=2)
        )

        counts = report["counts"]
        self.assertEqual(2, counts["clubs"])
        self.assertEqual(1, counts["tours"])
        self.assertEqual(1, counts["matches"])
        self.assertEqual(2, counts["players"])
        self.assertEqual(2, counts["player_seasons"])
        self.assertEqual(2, counts["player_match_stats"])
        self.assertEqual(2, counts["club_season_stats"])
        self.assertEqual(2, counts["club_match_stats"])
        self.assertEqual(1, counts["point_details"])

        self.assertEqual(2, self._count("clubs"))
        self.assertEqual(1, self._count("matches"))
        self.assertEqual(2, self._count("player_seasons"))
        self.assertEqual(2, self._count("player_match_stats"))
        self.assertEqual(1, self._count("fantasy_point_details"))
        self.assertIn("durations_seconds", report)

        with self.engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM ingestion_runs WHERE id = :id"),
                {"id": report["run_id"]},
            ).scalar_one()
        self.assertEqual("succeeded", status)

    def test_unplayed_season_stores_null_scores_and_passes_quality(self) -> None:
        """A not-yet-started season returns 0:0 fixtures with matchStatus
        NOT_STARTED; they must be stored without a score so they do not count as
        played draws, and the quality gate must still publish the snapshot even
        outside the 72h adjustment window."""
        fixture = _build_fixture()
        season = fixture["season"]["data"]["fantasyQueries"]["season"]
        match = season["tours"][0]["matches"][0]
        match["matchStatus"] = "NOT_STARTED"
        match["scheduledAt"] = "2026-08-10T17:00:00Z"
        match["home"]["score"] = 0
        match["away"]["score"] = 0
        # No player has minutes yet, so no match history is fetched.
        for player in fixture["players"]["data"]["fantasyQueries"]["players"]["list"]:
            player["gameStat"] = _game_stat(0, 0)
            player["seasonScoreInfo"] = {
                "place": None,
                "score": 0,
                "averageScore": 0,
                "scoreForLastTour": 0,
                "topPercent": None,
            }
        # The team season aggregate reports a not-started season (all zero).
        for alias in ("team0", "team1"):
            fixture["team_stats"]["data"]["stat_season"][0][alias] = {
                "MatchesPlayed": 0,
                "MatchesWon": 0,
                "MatchesDrawn": 0,
                "MatchesLost": 0,
                "GoalsScored": 0,
                "GoalsConceded": 0,
                "YellowCards": 0,
                "RedCards": 0,
            }

        report = run_ingestion(
            FakeClient(fixture), self.session_factory, IngestionOptions()
        )

        self.assertEqual(0, report["counts"]["player_match_stats"])
        self.assertEqual(2, report["counts"]["club_match_stats"])

        with self.engine.connect() as connection:
            home_score, away_score = connection.execute(
                text("SELECT home_score, away_score FROM matches")
            ).one()
            self.assertIsNone(home_score)
            self.assertIsNone(away_score)
            goals = [
                row[0]
                for row in connection.execute(
                    text("SELECT goals_scored FROM club_match_stats")
                )
            ]
            self.assertTrue(all(value is None for value in goals))

        # Evaluate well outside the 72h window to prove unplayed fixtures never
        # produce a blocking reconciliation mismatch.
        quality = run_quality_checks(
            self.session_factory,
            run_id=report["run_id"],
            now=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.assertTrue(quality["passed"])
        self.assertEqual(0, quality["counts"]["blocking"])

    def test_repeated_import_keeps_logical_entity_counts_stable(self) -> None:
        client = FakeClient(_build_fixture())
        run_ingestion(client, self.session_factory, IngestionOptions())

        catalog_before = {
            table: self._count(table)
            for table in (
                "clubs",
                "season_clubs",
                "fantasy_tours",
                "matches",
                "players",
                "player_seasons",
                "player_match_stats",
                "club_match_stats",
                "fantasy_point_details",
            )
        }

        run_ingestion(
            FakeClient(_build_fixture()), self.session_factory, IngestionOptions()
        )

        catalog_after = {
            table: self._count(table) for table in catalog_before
        }
        self.assertEqual(catalog_before, catalog_after)

        # Run-scoped snapshots accumulate one generation per run.
        self.assertEqual(4, self._count("fantasy_player_snapshots"))
        self.assertEqual(4, self._count("player_season_stats"))
        self.assertEqual(4, self._count("club_season_stats"))
        self.assertEqual(2, self._count("ingestion_runs"))

    def test_failed_fetch_publishes_no_partial_snapshot(self) -> None:
        client = FakeClient(_build_fixture(), fail_on_history=True)

        with self.assertRaises(RuntimeError):
            run_ingestion(client, self.session_factory, IngestionOptions())

        self.assertEqual(0, self._count("clubs"))
        self.assertEqual(0, self._count("matches"))
        self.assertEqual(0, self._count("player_seasons"))

        with self.engine.connect() as connection:
            statuses = [
                row[0]
                for row in connection.execute(
                    text("SELECT status FROM ingestion_runs")
                )
            ]
        self.assertEqual(["failed"], statuses)

        # A later successful run still works after a failure.
        report = run_ingestion(
            FakeClient(_build_fixture()), self.session_factory, IngestionOptions()
        )
        self.assertEqual(2, self._count("clubs"))
        self.assertEqual(2, report["counts"]["clubs"])


if __name__ == "__main__":
    unittest.main()


class BrokenPlayerPageTest(unittest.TestCase):
    """Sports.ru cannot serialise one player: the page is walked one by one."""

    class _Client(FakeClient):
        def __init__(self, fixture: dict, broken_position: int) -> None:
            super().__init__(fixture)
            self.broken_position = broken_position
            self.single_calls: list[int] = []

        def execute(self, query: str, variables=None) -> dict:
            variables = dict(variables or {})
            if query is PLAYERS_QUERY:
                from fantasy_analytics.client import GraphQLRequestError

                page_size = int(variables.get("pageSize") or 100)
                page = int(variables.get("pageNum") or 1)
                players = self._fixture["players"]["data"]["fantasyQueries"]["players"]
                listed = players["list"]
                if page_size > 1:
                    raise GraphQLRequestError('got nil for non-null "statPlayer"')
                self.single_calls.append(page)
                if page == self.broken_position:
                    raise GraphQLRequestError('got nil for non-null "statPlayer"')
                chunk = listed[page - 1 : page]
                return {
                    "data": {
                        "fantasyQueries": {
                            "players": {
                                "pageInfo": {"hasNextPage": page < len(listed)},
                                "list": copy.deepcopy(chunk),
                            }
                        }
                    }
                }
            return super().execute(query, variables)

    def test_the_broken_player_is_skipped_and_reported(self) -> None:
        client = self._Client(_build_fixture(), broken_position=1)

        result = fetch_all(client, IngestionOptions(history_workers=1))

        self.assertEqual(["222"], [str(p["id"]) for p in result.players])
        self.assertEqual(1, len(result.skipped_players))
        self.assertEqual(1, result.skipped_players[0]["position"])
        self.assertIn("statPlayer", result.skipped_players[0]["error"])
        # Positions after the broken one are still read.
        self.assertEqual([1, 2], client.single_calls)

    def test_a_clean_single_walk_reports_nothing(self) -> None:
        client = self._Client(_build_fixture(), broken_position=99)

        result = fetch_all(client, IngestionOptions(history_workers=1))

        self.assertEqual(2, len(result.players))
        self.assertEqual([], result.skipped_players)
