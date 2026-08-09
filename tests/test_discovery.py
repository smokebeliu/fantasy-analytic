import tempfile
import unittest
from pathlib import Path

from fantasy_analytics.discovery import (
    DiscoveryOptions,
    _derive_team_match_stats,
    _flatten_matches,
    _is_match_finished,
    _normalize_team_season_stats,
    _select_history_samples,
    _select_season,
    _write_json,
)


class SelectSeasonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tournament = {
            "seasons": [
                {
                    "id": "75",
                    "isActive": True,
                    "statObject": {"name": "2026/2027"},
                },
                {
                    "id": "44",
                    "isActive": False,
                    "statObject": {"name": "2024/2025"},
                },
                {
                    "id": "59",
                    "isActive": False,
                    "statObject": {"name": "2025/2026"},
                },
            ]
        }

    def test_selects_latest_completed_season_by_name(self) -> None:
        selected = _select_season(
            self.tournament,
            DiscoveryOptions(output_dir=Path("unused")),
        )

        self.assertEqual("59", selected["id"])

    def test_selects_current_season(self) -> None:
        selected = _select_season(
            self.tournament,
            DiscoveryOptions(output_dir=Path("unused"), use_current_season=True),
        )

        self.assertEqual("75", selected["id"])

    def test_selects_exact_season_id(self) -> None:
        selected = _select_season(
            self.tournament,
            DiscoveryOptions(output_dir=Path("unused"), season_id="44"),
        )

        self.assertEqual("44", selected["id"])


class DerivedTeamStatsTest(unittest.TestCase):
    def test_derives_home_away_results_and_clean_sheets(self) -> None:
        teams = [
            {
                "name": "Home",
                "statObject": {"id": "home"},
            },
            {
                "name": "Away",
                "statObject": {"id": "away"},
            },
        ]
        matches = [
            {
                "match_status": "CLOSED",
                "home": {"score": 2, "team": {"id": "home"}},
                "away": {"score": 0, "team": {"id": "away"}},
            },
            {
                "match_status": "CLOSED",
                "home": {"score": 1, "team": {"id": "away"}},
                "away": {"score": 1, "team": {"id": "home"}},
            },
        ]

        result = {
            item["stat_team_id"]: item
            for item in _derive_team_match_stats(teams, matches)
        }

        self.assertEqual(1, result["home"]["wins"])
        self.assertEqual(1, result["home"]["draws"])
        self.assertEqual(1, result["home"]["clean_sheets"])
        self.assertEqual(3, result["home"]["goals_for"])
        self.assertEqual(1, result["home"]["goals_against"])
        self.assertEqual(1, result["away"]["home_matches"])
        self.assertEqual(1, result["away"]["away_matches"])

    def test_ignores_unfinished_matches(self) -> None:
        teams = [{"name": "Home", "statObject": {"id": "home"}}]
        matches = [
            {
                "match_status": "CLOSED",
                "home": {"score": None, "team": {"id": "home"}},
                "away": {"score": None, "team": {"id": "unknown"}},
            }
        ]

        result = _derive_team_match_stats(teams, matches)

        self.assertEqual(0, result[0]["matches"])

    def test_ignores_not_started_matches_with_zero_scores(self) -> None:
        """An unplayed fixture returns 0:0 (not null); it must be ignored."""
        teams = [
            {"name": "Home", "statObject": {"id": "home"}},
            {"name": "Away", "statObject": {"id": "away"}},
        ]
        matches = [
            {
                "match_status": "NOT_STARTED",
                "home": {"score": 0, "team": {"id": "home"}},
                "away": {"score": 0, "team": {"id": "away"}},
            }
        ]

        result = {
            item["stat_team_id"]: item
            for item in _derive_team_match_stats(teams, matches)
        }

        self.assertEqual(0, result["home"]["matches"])
        self.assertEqual(0, result["home"]["draws"])
        self.assertEqual(0, result["away"]["matches"])


class MatchFinishedTest(unittest.TestCase):
    def test_only_closed_matches_are_finished(self) -> None:
        self.assertTrue(_is_match_finished("CLOSED"))
        self.assertTrue(_is_match_finished("closed"))
        self.assertFalse(_is_match_finished("NOT_STARTED"))
        self.assertFalse(_is_match_finished("LIVE"))
        self.assertFalse(_is_match_finished(None))
        self.assertFalse(_is_match_finished(""))

    def test_flatten_matches_carries_match_status(self) -> None:
        season = {
            "tours": [
                {
                    "id": "1",
                    "name": "1 тур",
                    "status": "OPENED",
                    "matches": [
                        {
                            "id": "900001",
                            "scheduledAt": "2026-07-24T17:00:00Z",
                            "matchStatus": "NOT_STARTED",
                            "home": {"score": 0, "team": {"id": "home"}},
                            "away": {"score": 0, "team": {"id": "away"}},
                        }
                    ],
                }
            ]
        }

        flattened = _flatten_matches(season)

        self.assertEqual(1, len(flattened))
        self.assertEqual("NOT_STARTED", flattened[0]["match_status"])
        self.assertFalse(_is_match_finished(flattened[0]["match_status"]))


class NormalizeTeamStatsTest(unittest.TestCase):
    def test_maps_dynamic_aliases_back_to_team_ids(self) -> None:
        payload = {
            "data": {
                "stat_season": [
                    {
                        "team0": {
                            "MatchesPlayed": 30,
                            "GoalsScored": 60,
                        }
                    }
                ]
            }
        }
        teams = [
            {
                "id": "10",
                "name": "Краснодар",
                "statObject": {"id": "fc_krasnodar"},
            }
        ]

        result = _normalize_team_season_stats(payload, teams)

        self.assertEqual("10", result[0]["fantasy_team_id"])
        self.assertEqual("fc_krasnodar", result[0]["stat_team_id"])
        self.assertEqual(60, result[0]["stats"]["GoalsScored"])


class HistorySampleTest(unittest.TestCase):
    def test_selects_top_player_with_minutes_for_each_role(self) -> None:
        players = [
            {
                "id": "gk-low",
                "role": "GOALKEEPER",
                "team": {"id": "1"},
                "gameStat": {"points": 10, "fieldMinutes": 90},
            },
            {
                "id": "gk-high",
                "role": "GOALKEEPER",
                "team": {"id": "1"},
                "gameStat": {"points": 20, "fieldMinutes": 90},
            },
            {
                "id": "def-no-minutes",
                "role": "DEFENDER",
                "team": {"id": "1"},
                "gameStat": {"points": 100, "fieldMinutes": 0},
            },
            {
                "id": "def",
                "role": "DEFENDER",
                "team": {"id": "1"},
                "gameStat": {"points": 15, "fieldMinutes": 90},
            },
        ]

        result = _select_history_samples(players, 1)

        self.assertEqual(["gk-high", "def"], [player["id"] for player in result])


class JsonWriterTest(unittest.TestCase):
    def test_writes_utf8_json_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "data.json"

            _write_json(path, {"name": "Россия"})

            self.assertIn("Россия", path.read_text(encoding="utf-8"))
            self.assertFalse(path.with_suffix(".json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
