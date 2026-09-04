"""Unit tests for importing a Sports.ru team from its public URL."""

from __future__ import annotations

import unittest

from fantasy_analytics.client import GraphQLRequestError
from fantasy_analytics.squad_import import (
    SquadImportError,
    extract_remote_squad,
    import_squad_from_url,
    parse_squad_url,
    slugs_match,
)


def _remote_payload(
    *,
    squad_id: str = "588960",
    name: str = "бегим",
    slug: str = "portugal",
    league_name: str = "Португалия",
    season_id: str = "77",
    player_ids: tuple[str, ...] = ("68704", "68789"),
    players: list[dict] | None = None,
    include_tour: bool = True,
) -> dict:
    if players is None:
        players = [
            {
                "isCaptain": index == 1,
                "isViceCaptain": False,
                "isStarting": True,
                "substitutePriority": None,
                "seasonPlayer": {
                    "id": player_id,
                    "name": f"Игрок {player_id}",
                    "price": 6.5,
                    "role": "FORWARD" if index else "GOALKEEPER",
                    "team": {"id": "700", "name": "Порту"},
                },
            }
            for index, player_id in enumerate(player_ids)
        ]
    tour_info = {
        "tour": {"id": "2349", "name": "3 тур", "status": "OPENED"} if include_tour else None,
        "totalPrice": 97.5,
        "currentBalance": 2.5,
        "transfersLeft": 3,
        "transfersDone": 0,
        "players": players,
    }
    return {
        "data": {
            "fantasyQueries": {
                "squads": [
                    {
                        "id": squad_id,
                        "name": name,
                        "season": {
                            "id": season_id,
                            "isActive": True,
                            "tournament": {
                                "id": "13",
                                "webName": slug,
                                "name": league_name,
                            },
                        },
                        "currentTourInfo": tour_info,
                    }
                ]
            }
        }
    }


class ParseSquadUrlTest(unittest.TestCase):
    def test_parses_the_public_portugal_team_page(self) -> None:
        parsed = parse_squad_url(
            "https://www.sports.ru/fantasy/football/portugal/588960/"
        )
        self.assertEqual("588960", parsed.squad_id)
        self.assertEqual("portugal", parsed.slug)

    def test_accepts_http_no_www_and_no_trailing_slash(self) -> None:
        parsed = parse_squad_url("http://sports.ru/fantasy/football/russia/12345")
        self.assertEqual("12345", parsed.squad_id)
        self.assertEqual("russia", parsed.slug)

    def test_accepts_a_path_without_a_scheme(self) -> None:
        parsed = parse_squad_url("www.sports.ru/fantasy/football/portugal/588960/")
        self.assertEqual("588960", parsed.squad_id)
        self.assertEqual("portugal", parsed.slug)

    def test_accepts_a_site_relative_path(self) -> None:
        parsed = parse_squad_url("/fantasy/football/portugal/588960/")
        self.assertEqual("588960", parsed.squad_id)
        self.assertEqual("portugal", parsed.slug)

    def test_accepts_a_bare_squad_id(self) -> None:
        parsed = parse_squad_url(" 588960 ")
        self.assertEqual("588960", parsed.squad_id)
        self.assertIsNone(parsed.slug)

    def test_rejects_a_league_ratings_url(self) -> None:
        with self.assertRaises(SquadImportError) as ctx:
            parse_squad_url(
                "https://www.sports.ru/fantasy/football/portugal/443964/ratings/32301/"
            )
        self.assertEqual("invalid_squad_url", ctx.exception.type_)

    def test_rejects_a_foreign_host(self) -> None:
        with self.assertRaises(SquadImportError) as ctx:
            parse_squad_url("https://example.com/fantasy/football/portugal/1/")
        self.assertEqual("invalid_squad_url", ctx.exception.type_)

    def test_rejects_an_empty_string(self) -> None:
        with self.assertRaises(SquadImportError):
            parse_squad_url("   ")


class SlugMatchTest(unittest.TestCase):
    def test_rpl_is_an_alias_of_russia(self) -> None:
        self.assertTrue(slugs_match("rpl", "russia"))
        self.assertTrue(slugs_match("Russia", "RPL"))

    def test_distinct_leagues_do_not_match(self) -> None:
        self.assertFalse(slugs_match("portugal", "russia"))
        self.assertFalse(slugs_match("", "russia"))


class ExtractRemoteSquadTest(unittest.TestCase):
    def test_reads_players_and_tournament(self) -> None:
        remote = extract_remote_squad(_remote_payload())
        assert remote is not None
        self.assertEqual("588960", remote.squad_id)
        self.assertEqual("бегим", remote.name)
        self.assertEqual("portugal", remote.slug)
        self.assertEqual("Португалия", remote.league_name)
        self.assertEqual("77", remote.season_id)
        self.assertEqual("2349", remote.tour["fantasy_tour_id"])
        self.assertEqual(["68704", "68789"], [p.fantasy_player_id for p in remote.players])
        self.assertTrue(remote.players[1].is_captain)

    def test_reads_the_bank_and_the_transfers_left(self) -> None:
        remote = extract_remote_squad(_remote_payload())
        assert remote is not None
        self.assertEqual(97.5, remote.total_price)
        self.assertEqual(2.5, remote.current_balance)
        self.assertEqual(100.0, remote.budget)
        self.assertEqual(3, remote.transfers_left)
        self.assertEqual(0, remote.transfers_done)

    def test_missing_money_fields_leave_the_budget_unknown(self) -> None:
        payload = _remote_payload()
        info = payload["data"]["fantasyQueries"]["squads"][0]["currentTourInfo"]
        del info["currentBalance"]
        info["transfersLeft"] = "not a number"
        remote = extract_remote_squad(payload)
        assert remote is not None
        self.assertEqual(97.5, remote.total_price)
        self.assertIsNone(remote.current_balance)
        self.assertIsNone(remote.budget)
        self.assertIsNone(remote.transfers_left)

    def test_empty_squads_list_is_missing(self) -> None:
        self.assertIsNone(
            extract_remote_squad({"data": {"fantasyQueries": {"squads": []}}})
        )

    def test_skips_duplicate_player_ids(self) -> None:
        payload = _remote_payload(
            players=[
                {
                    "isCaptain": False,
                    "isViceCaptain": False,
                    "isStarting": True,
                    "substitutePriority": None,
                    "seasonPlayer": {"id": "1", "name": "A", "role": "FORWARD"},
                },
                {
                    "isCaptain": True,
                    "isViceCaptain": False,
                    "isStarting": True,
                    "substitutePriority": None,
                    "seasonPlayer": {"id": "1", "name": "A", "role": "FORWARD"},
                },
            ]
        )
        remote = extract_remote_squad(payload)
        assert remote is not None
        self.assertEqual(1, len(remote.players))


class ImportSquadFromUrlTest(unittest.TestCase):
    def test_rejects_a_url_from_another_league_without_fetching(self) -> None:
        calls: list[str] = []

        def fetch(_squad_id: str) -> dict:
            calls.append("fetched")
            return _remote_payload()

        with self.assertRaises(SquadImportError) as ctx:
            import_squad_from_url(
                url="https://www.sports.ru/fantasy/football/portugal/588960/",
                expected_slug="russia",
                fetch_squad=fetch,
                resolve_players=lambda ids: [],
            )
        self.assertEqual("league_mismatch", ctx.exception.type_)
        self.assertEqual(409, ctx.exception.status)
        self.assertEqual([], calls)

    def test_rejects_when_the_remote_league_disagrees_with_a_bare_id(self) -> None:
        with self.assertRaises(SquadImportError) as ctx:
            import_squad_from_url(
                url="588960",
                expected_slug="russia",
                fetch_squad=lambda _id: _remote_payload(slug="portugal"),
                resolve_players=lambda ids: [],
            )
        self.assertEqual("league_mismatch", ctx.exception.type_)
        self.assertIn("Португалия", ctx.exception.message)

    def test_resolves_players_in_remote_order(self) -> None:
        local = [
            {"fantasy_player_id": "68789", "player_name": "Суарес", "role": "FORWARD"},
            {"fantasy_player_id": "68704", "player_name": "Кошта", "role": "GOALKEEPER"},
        ]

        result = import_squad_from_url(
            url="https://www.sports.ru/fantasy/football/portugal/588960/",
            expected_slug="portugal",
            fetch_squad=lambda _id: _remote_payload(),
            resolve_players=lambda ids: [p for p in local if p["fantasy_player_id"] in ids],
        )
        self.assertEqual("бегим", result["squad_name"])
        self.assertEqual(
            ["68704", "68789"],
            [player["fantasy_player_id"] for player in result["players"]],
        )
        self.assertEqual([], result["missing"])
        self.assertEqual("2349", result["remote_tour"]["fantasy_tour_id"])

    def test_reports_players_missing_from_the_snapshot(self) -> None:
        result = import_squad_from_url(
            url="588960",
            expected_slug="portugal",
            fetch_squad=lambda _id: _remote_payload(),
            resolve_players=lambda ids: [
                {"fantasy_player_id": "68704", "player_name": "Кошта", "role": "GOALKEEPER"}
            ],
        )
        self.assertEqual(["68704"], [p["fantasy_player_id"] for p in result["players"]])
        self.assertEqual(["68789"], [p["fantasy_player_id"] for p in result["missing"]])

    def test_explains_a_league_id_that_is_not_a_team(self) -> None:
        with self.assertRaises(SquadImportError) as ctx:
            import_squad_from_url(
                url="443964",
                expected_slug="portugal",
                fetch_squad=lambda _id: {"data": {"fantasyQueries": {"squads": []}}},
                fetch_league=lambda _id: {
                    "data": {
                        "fantasyQueries": {"league": {"id": "443964", "name": "ПОР"}}
                    }
                },
                resolve_players=lambda ids: [],
            )
        self.assertEqual("not_a_squad", ctx.exception.type_)
        self.assertIn("лигу", ctx.exception.message)

    def test_wraps_an_upstream_graphql_failure(self) -> None:
        def fetch(_squad_id: str) -> dict:
            raise GraphQLRequestError("timeout")

        with self.assertRaises(SquadImportError) as ctx:
            import_squad_from_url(
                url="588960",
                expected_slug="portugal",
                fetch_squad=fetch,
                resolve_players=lambda ids: [],
            )
        self.assertEqual("upstream_error", ctx.exception.type_)
        self.assertEqual(502, ctx.exception.status)

    def test_rejects_an_empty_current_tour_roster(self) -> None:
        with self.assertRaises(SquadImportError) as ctx:
            import_squad_from_url(
                url="588960",
                expected_slug="portugal",
                fetch_squad=lambda _id: _remote_payload(players=[]),
                resolve_players=lambda ids: [],
            )
        self.assertEqual("empty_squad", ctx.exception.type_)


if __name__ == "__main__":
    unittest.main()
