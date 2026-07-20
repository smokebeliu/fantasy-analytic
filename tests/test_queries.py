import unittest

from fantasy_analytics.queries import build_team_stats_query


class TeamStatsQueryTest(unittest.TestCase):
    def test_builds_one_typed_alias_per_team(self) -> None:
        query = build_team_stats_query(2)

        self.assertIn("$seasonID: [String!]!", query)
        self.assertIn("$team0: ID!", query)
        self.assertIn("$team1: ID!", query)
        self.assertIn("team0: stats(id: $team0, source: SPORTS_HUB)", query)
        self.assertIn("team1: stats(id: $team1, source: SPORTS_HUB)", query)
        self.assertIn("GoalsScored", query)

    def test_rejects_empty_team_list(self) -> None:
        with self.assertRaises(ValueError):
            build_team_stats_query(0)


if __name__ == "__main__":
    unittest.main()
