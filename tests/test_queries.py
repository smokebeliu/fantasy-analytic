import unittest

from fantasy_analytics.queries import SQUAD_QUERY, build_team_stats_query


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


class SquadQueryTest(unittest.TestCase):
    def test_asks_for_the_current_tour_roster(self) -> None:
        self.assertIn("squads(input: { squadID: $squadID })", SQUAD_QUERY)
        self.assertIn("currentTourInfo", SQUAD_QUERY)
        self.assertIn("seasonPlayer", SQUAD_QUERY)
        self.assertIn("webName", SQUAD_QUERY)


if __name__ == "__main__":
    unittest.main()
