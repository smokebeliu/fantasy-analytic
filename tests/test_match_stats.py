import json
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fantasy_analytics.client import ClientConfig, SportsGraphQLClient
from fantasy_analytics.match_stats import (
    CoverageAccumulator,
    MatchRef,
    MatchStatsError,
    MatchStatsOptions,
    fetch_match_stats,
    iter_leaf_paths,
    run_match_stats_discovery,
    select_match_sample,
)
from fantasy_analytics.queries import MATCH_STATS_QUERY


def _side(score: int, *, starters: int = 11, bench: int = 1, xg=None):
    lineup = []
    for index in range(starters):
        lineup.append(
            {
                "player": {"id": f"player_{score}_{index}", "name": f"Player {index}"},
                "jerseyNumber": str(index + 1),
                "position": "CENTRAL_MIDFIELDER",
                "lineupOrder": index + 1,
                "lineupStarting": True,
                "lineupCurrent": True,
                "isCaptain": index == 0,
                "type": "PLAYER",
                "mark": 60,
                "stat": {
                    "minutesPlayed": 90,
                    "goalsScored": 1 if index == 0 else 0,
                    "assists": None,
                    "chancesCreated": 0,
                    "yellowCards": 0,
                    "xG": None,
                },
            }
        )
    for index in range(bench):
        lineup.append(
            {
                "player": {"id": f"bench_{score}_{index}", "name": f"Bench {index}"},
                "jerseyNumber": str(50 + index),
                "position": None,
                "lineupOrder": starters + index + 1,
                "lineupStarting": False,
                "lineupCurrent": False,
                "isCaptain": False,
                "type": "PLAYER",
                "mark": None,
                "stat": {
                    "minutesPlayed": None,
                    "goalsScored": 0,
                    "assists": None,
                    "chancesCreated": 0,
                    "yellowCards": 0,
                    "xG": None,
                },
            }
        )
    return {
        "team": {"id": "fc_team", "name": "Team", "abbreviation": "TEA"},
        "score": score,
        "xG": xg,
        "formation": {"code": "4-3-3"},
        "manager": {"id": "coach", "name": "Coach"},
        "stat": {
            "shotsTotal": 12,
            "shotsOnTarget": 4,
            "ballPossession": 55,
            "cornerKicks": 5,
            "fouls": 10,
            "yellowCards": 1,
            "totalRedCards": None,
            "offsides": None,
        },
        "lineup": lineup,
    }


def _match_payload(match_id: str, home_score: int, away_score: int) -> dict:
    return {
        "data": {
            "statQueries": {
                "football": {
                    "match": {
                        "id": match_id,
                        "matchStatus": "CLOSED",
                        "scheduledAt": "2025-07-18T17:30:00Z",
                        "attendance": 15000,
                        "hasDetailStat": True,
                        "hasLineups": True,
                        "hasEvents": True,
                        "hasPersonStat": True,
                        "hasXG": False,
                        "venue": {"id": "venue_1", "name": "Arena"},
                        "home": _side(home_score),
                        "away": _side(away_score),
                        "events": [
                            {
                                "id": "1",
                                "time": "2025-07-18T18:00:00Z",
                                "unix_time": 1752861600,
                                "type": "SCORE_CHANGE",
                                "outcome": "",
                                "team": "HOME",
                            }
                        ],
                    }
                }
            }
        }
    }


def _fake_client(payload_for_id) -> SportsGraphQLClient:
    def transport(request, _timeout):
        body = json.loads(request.data)
        match_id = body["variables"]["id"]
        return json.dumps(payload_for_id(match_id)).encode("utf-8")

    return SportsGraphQLClient(ClientConfig(attempts=1), transport=transport)


class LeafPathTest(unittest.TestCase):
    def test_collapses_home_away_and_list_indices(self) -> None:
        payload = {
            "home": {"stat": {"shotsTotal": 10}, "lineup": [{"stat": {"goals": 1}}]},
            "away": {"stat": {"shotsTotal": 5}, "lineup": [{"stat": {"goals": 0}}]},
        }
        paths = dict(iter_leaf_paths(payload))
        # home/away collapse to a single "side" prefix, list indices to "[]".
        self.assertIn("side.stat.shotsTotal", paths)
        self.assertIn("side.lineup[].stat.goals", paths)
        self.assertNotIn("home.stat.shotsTotal", paths)


class CoverageTest(unittest.TestCase):
    def test_fill_rate_and_decision_aggregate_both_sides(self) -> None:
        acc = CoverageAccumulator()
        acc.add_match(
            {
                "hasDetailStat": True,
                "home": {"stat": {"shotsTotal": 10, "offsides": None}},
                "away": {"stat": {"shotsTotal": 8, "offsides": 2}},
            }
        )
        table = {row["path"]: row for row in acc.table()}

        self.assertEqual(2, table["side.stat.shotsTotal"]["observed"])
        self.assertEqual(2, table["side.stat.shotsTotal"]["non_null"])
        self.assertEqual("ADOPT", table["side.stat.shotsTotal"]["decision"])

        self.assertEqual(2, table["side.stat.offsides"]["observed"])
        self.assertEqual(1, table["side.stat.offsides"]["non_null"])
        self.assertEqual("CONDITIONAL", table["side.stat.offsides"]["decision"])

    def test_all_null_field_is_excluded(self) -> None:
        acc = CoverageAccumulator()
        acc.add_match({"home": {"stat": {"xG": None}}, "away": {"stat": {"xG": None}}})
        table = {row["path"]: row for row in acc.table()}
        self.assertEqual("EXCLUDE", table["side.stat.xG"]["decision"])
        self.assertEqual("null", table["side.stat.xG"]["type"])


class SampleSelectionTest(unittest.TestCase):
    def _catalog(self, count: int) -> list[MatchRef]:
        clubs = [f"club_{i}" for i in range(16)]
        base = datetime(2025, 7, 18, tzinfo=UTC)
        matches = []
        for i in range(count):
            matches.append(
                MatchRef(
                    stat_match_id=str(1000 + i),
                    tour_name=f"Tour {i // 8 + 1}",
                    tour_order=i // 8 + 1,
                    home_club=clubs[i % 16],
                    away_club=clubs[(i + 1) % 16],
                    scheduled_at=base + timedelta(days=i),
                    home_score=1,
                    away_score=0,
                )
            )
        return matches

    def test_returns_all_when_below_target(self) -> None:
        catalog = self._catalog(20)
        sample = select_match_sample(catalog, sample_size=40)
        self.assertEqual(20, len(sample))

    def test_enforces_minimum_and_covers_all_clubs(self) -> None:
        catalog = self._catalog(240)
        sample = select_match_sample(catalog, sample_size=40)
        self.assertGreaterEqual(len(sample), 30)
        covered = {club for match in sample for club in match.clubs}
        self.assertEqual(16, len(covered))

    def test_is_deterministic(self) -> None:
        catalog = self._catalog(240)
        first = [m.stat_match_id for m in select_match_sample(catalog, 40)]
        second = [m.stat_match_id for m in select_match_sample(catalog, 40)]
        self.assertEqual(first, second)

    def test_ignores_unfinished_matches(self) -> None:
        catalog = self._catalog(5)
        catalog[0] = MatchRef(
            stat_match_id="unfinished",
            tour_name="Tour 1",
            tour_order=1,
            home_club="a",
            away_club="b",
            scheduled_at=datetime(2025, 7, 18, tzinfo=UTC),
            home_score=None,
            away_score=None,
        )
        sample = select_match_sample(catalog, sample_size=40)
        self.assertNotIn("unfinished", {m.stat_match_id for m in sample})


class FetchContractTest(unittest.TestCase):
    def test_query_targets_the_documented_operation(self) -> None:
        self.assertIn("statQueries", MATCH_STATS_QUERY)
        self.assertIn("football", MATCH_STATS_QUERY)
        self.assertIn("match(id: $id", MATCH_STATS_QUERY)

    def test_fetch_returns_stat_match(self) -> None:
        client = _fake_client(lambda mid: _match_payload(mid, 2, 1))
        payload = fetch_match_stats(client, "12345")
        match = payload["data"]["statQueries"]["football"]["match"]
        self.assertEqual("12345", match["id"])
        self.assertTrue(match["hasLineups"])

    def test_fetch_rejects_missing_match(self) -> None:
        client = _fake_client(
            lambda _mid: {"data": {"statQueries": {"football": {"match": None}}}}
        )
        with self.assertRaises(MatchStatsError):
            fetch_match_stats(client, "404")


class DiscoveryRunTest(unittest.TestCase):
    def test_end_to_end_builds_report_and_fixtures(self) -> None:
        clubs = [f"club_{i}" for i in range(16)]
        base = datetime(2025, 7, 18, tzinfo=UTC)
        catalog = [
            MatchRef(
                stat_match_id=str(2000 + i),
                tour_name=f"Tour {i // 8 + 1}",
                tour_order=i // 8 + 1,
                home_club=clubs[i % 16],
                away_club=clubs[(i + 1) % 16],
                scheduled_at=base + timedelta(days=i),
                home_score=2,
                away_score=1,
            )
            for i in range(60)
        ]
        client = _fake_client(lambda mid: _match_payload(mid, 2, 1))

        with tempfile.TemporaryDirectory() as directory:
            options = MatchStatsOptions(
                output_dir=Path(directory), sample_size=30
            )
            report = run_match_stats_discovery(client, catalog, options)

            self.assertGreaterEqual(report["sample"]["matches"], 30)
            # Every sampled match reconciles with the catalog score.
            self.assertEqual(
                report["sample"]["matches"],
                report["availability"]["score_matches_catalog"],
            )
            self.assertEqual(
                report["sample"]["matches"],
                report["availability"]["eleven_home_starters"],
            )
            coverage = {row["path"]: row for row in report["coverage"]}
            self.assertEqual("ADOPT", coverage["side.stat.shotsTotal"]["decision"])
            self.assertEqual("EXCLUDE", coverage["side.lineup[].stat.xG"]["decision"])

            self.assertTrue((Path(directory) / "report.json").exists())
            self.assertTrue((Path(directory) / "field-coverage.md").exists())
            self.assertTrue((Path(directory) / "field-coverage.json").exists())
            raw_files = list((Path(directory) / "raw").glob("match-*.json"))
            self.assertEqual(report["sample"]["matches"], len(raw_files))


@unittest.skipUnless(
    os.environ.get("RUN_LIVE_MATCH_STATS") == "1",
    "live contract test; set RUN_LIVE_MATCH_STATS=1 to enable",
)
class LiveContractTest(unittest.TestCase):
    def test_known_match_exposes_core_fields(self) -> None:
        client = SportsGraphQLClient()
        payload = fetch_match_stats(client, "102703803")
        match = payload["data"]["statQueries"]["football"]["match"]
        for flag in ("hasDetailStat", "hasLineups", "hasEvents", "hasPersonStat"):
            self.assertTrue(match[flag], f"expected {flag} to be true")
        self.assertIsNotNone(match["home"]["stat"]["shotsTotal"])
        self.assertGreaterEqual(len(match["home"]["lineup"]), 11)


if __name__ == "__main__":
    unittest.main()
