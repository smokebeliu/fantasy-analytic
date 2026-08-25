"""Unit and integration tests for match betting odds in the event forecast.

Odds must change *expected points* (favourite vs weak defence → attacking
upside, priced shutout → clean-sheet upside) and must never be consulted by
the optimizer itself. The integration tests persist a 1x2 line into a real
PostgreSQL database, rebuild the forecast, and check the points moved.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

from sqlalchemy import text

from fantasy_analytics.api import create_app
from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
    session_scope,
)
from fantasy_analytics.db.models import MatchOdds, Season
from fantasy_analytics.forecast import (
    MODEL_EVENT,
    build_forecast_dataset,
    clean_sheet_probability,
    forecast_event_model,
    team_goal_means,
)
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.odds import (
    DEFAULT_ODDS_WEIGHT,
    blend_goal_means,
    implied_1x2,
    invert_poisson_means,
    line_to_expected_goals,
    parse_line1x2,
    poisson_match_probs,
)
from fantasy_analytics.odds_refresh import (
    extract_match_odds,
    flatten_calendar_matches,
    hub_tournament_id,
    is_day_before_tour,
    refresh_league_odds,
    season_slug_from_name,
)
from fantasy_analytics.quality import run_quality_checks
from fantasy_analytics.queries import CALENDAR_ODDS_QUERY, TOURNAMENT_HUB_QUERY

from fastapi.testclient import TestClient

from test_forecast import _feature_row
from test_ingestion import FakeClient
from test_quality import _consistent_fixture

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)
MOSCOW = timezone(timedelta(hours=3))


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


def _calendar_payload(match_id: str = "900002") -> dict:
    return {
        "data": {
            "statQueries": {
                "football": {
                    "tournament": {
                        "currentSeason": {"id": "rfpl_25-26", "name": "2025/2026"},
                        "seasonBySlug": {
                            "id": "rfpl_25-26",
                            "name": "2025/2026",
                            "groupMatches": [
                                {
                                    "stageName": "2 тур",
                                    "days": [
                                        {
                                            "date": "2025-07-25",
                                            "matches": [
                                                {
                                                    "id": match_id,
                                                    "matchStatus": "NOT_STARTED",
                                                    "scheduledAt": "2025-07-25T16:00:00Z",
                                                    "home": {
                                                        "team": {
                                                            "id": "club_a",
                                                            "name": "Клуб A",
                                                        }
                                                    },
                                                    "away": {
                                                        "team": {
                                                            "id": "club_b",
                                                            "name": "Клуб B",
                                                        }
                                                    },
                                                    "bettingOdds": [
                                                        {
                                                            "bookmaker": {
                                                                "lead": "winline.ru"
                                                            },
                                                            "line1x2": {
                                                                "h": 1.40,
                                                                "x": 4.50,
                                                                "a": 8.00,
                                                            },
                                                        }
                                                    ],
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                }
            }
        }
    }


class OddsMathTest(unittest.TestCase):
    def test_implied_probabilities_remove_the_overround(self) -> None:
        home, draw, away = implied_1x2(2.0, 3.5, 4.0)
        self.assertAlmostEqual(home + draw + away, 1.0, places=9)
        self.assertGreater(home, away)
        self.assertGreater(home, draw)

    def test_implied_rejects_even_money_or_worse(self) -> None:
        with self.assertRaises(ValueError):
            implied_1x2(1.0, 3.0, 5.0)

    def test_poisson_match_probs_are_symmetric(self) -> None:
        home, draw, away = poisson_match_probs(1.2, 1.2)
        self.assertAlmostEqual(home, away, places=6)
        self.assertAlmostEqual(home + draw + away, 1.0, delta=0.02)

    def test_invert_recovers_known_poisson_means(self) -> None:
        p_home, p_draw, p_away = poisson_match_probs(1.8, 0.9)
        lam_h, lam_a = invert_poisson_means(p_home, p_draw, p_away)
        self.assertAlmostEqual(lam_h, 1.8, delta=0.15)
        self.assertAlmostEqual(lam_a, 0.9, delta=0.15)
        self.assertGreater(lam_h, lam_a)

    def test_heavy_favourite_implies_more_home_goals(self) -> None:
        parsed = line_to_expected_goals(1.25, 6.0, 12.0)
        self.assertGreater(parsed["expected_home_goals"], parsed["expected_away_goals"])
        self.assertGreater(parsed["implied_home"], 0.7)

    def test_blend_without_odds_is_a_no_op(self) -> None:
        gf, ga, scale = blend_goal_means(1.4, 1.1, None, None)
        self.assertEqual((gf, ga, scale), (1.4, 1.1, 1.0))

    def test_blend_pulls_towards_the_market(self) -> None:
        gf, ga, scale = blend_goal_means(1.0, 1.0, 2.0, 0.5, weight=0.6)
        self.assertAlmostEqual(gf, 0.4 * 1.0 + 0.6 * 2.0, places=4)
        self.assertAlmostEqual(ga, 0.4 * 1.0 + 0.6 * 0.5, places=4)
        self.assertGreater(scale, 1.0)

    def test_parse_line1x2_reads_the_widget_shape(self) -> None:
        parsed = parse_line1x2(
            {
                "bookmaker": {"lead": "winline.ru , Реклама 18+"},
                "line1x2": {"h": 4.7, "x": 4.2, "a": 1.67},
            }
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["home_odds"], 4.7)
        self.assertGreater(parsed["expected_away_goals"], parsed["expected_home_goals"])
        self.assertIn("winline", parsed["bookmaker"] or "")

    def test_parse_skips_a_missing_line(self) -> None:
        self.assertIsNone(parse_line1x2({"bookmaker": {"lead": "x"}}))
        self.assertIsNone(parse_line1x2(None))


class ForecastOddsSignalTest(unittest.TestCase):
    """The two squad signals fall out of expected_points, not the solver."""

    def test_favourite_lifts_attacking_points(self) -> None:
        base = forecast_event_model(
            _feature_row(role="FORWARD", goals_per90=0.5, assists_per90=0.25)
        )
        boosted = forecast_event_model(
            _feature_row(
                role="FORWARD",
                goals_per90=0.5,
                assists_per90=0.25,
                odds_goals_for=2.4,
                odds_goals_against=0.6,
                odds_weight=DEFAULT_ODDS_WEIGHT,
            )
        )
        self.assertGreater(
            boosted["components"]["goals"], base["components"]["goals"]
        )
        self.assertGreater(
            boosted["components"]["assists"], base["components"]["assists"]
        )
        self.assertGreater(boosted["expected_points"], base["expected_points"])
        self.assertGreater(boosted["expected"]["per_fixture"][0]["attack_scale"], 1.0)

    def test_priced_shutout_lifts_clean_sheet_points(self) -> None:
        hist_for, hist_against = team_goal_means(1.0, 1.0, 1.0, 1.0)
        base = forecast_event_model(_feature_row(role="GOALKEEPER"))
        shutout = forecast_event_model(
            _feature_row(
                role="GOALKEEPER",
                odds_goals_for=1.6,
                odds_goals_against=0.4,
                odds_weight=DEFAULT_ODDS_WEIGHT,
            )
        )
        self.assertGreater(
            shutout["components"]["clean_sheet"],
            base["components"]["clean_sheet"],
        )
        self.assertLess(
            shutout["expected"]["per_fixture"][0]["team_goals_against"],
            hist_against,
        )
        self.assertGreater(
            shutout["expected"]["per_fixture"][0]["clean_sheet_probability"],
            clean_sheet_probability(hist_against),
        )

    def test_underdog_attack_is_scaled_down(self) -> None:
        base = forecast_event_model(
            _feature_row(role="FORWARD", goals_per90=0.5)
        )
        dog = forecast_event_model(
            _feature_row(
                role="FORWARD",
                goals_per90=0.5,
                odds_goals_for=0.5,
                odds_goals_against=2.2,
            )
        )
        self.assertLess(dog["components"]["goals"], base["components"]["goals"])
        self.assertLess(dog["expected_points"], base["expected_points"])

    def test_no_odds_keeps_the_historical_event_model(self) -> None:
        row = _feature_row(role="MIDFIELDER", goals_per90=0.4, assists_per90=0.2)
        self.assertEqual(forecast_event_model(row), forecast_event_model(row))
        result = forecast_event_model(row)
        self.assertEqual(result["expected"]["per_fixture"][0]["attack_scale"], 1.0)
        self.assertIsNone(result["expected"]["per_fixture"][0]["odds_goals_for"])


class CalendarParseTest(unittest.TestCase):
    def test_hub_id_strips_the_season_suffix(self) -> None:
        self.assertEqual(hub_tournament_id("rfpl_26-27"), "rfpl")
        self.assertEqual(hub_tournament_id("serie_a_25-26"), "serie_a")
        self.assertEqual(hub_tournament_id("rfpl"), "rfpl")

    def test_season_slug_uses_a_hyphen(self) -> None:
        self.assertEqual(season_slug_from_name("2026/2027"), "2026-2027")

    def test_flatten_and_extract_calendar_matches(self) -> None:
        matches = flatten_calendar_matches(_calendar_payload())
        self.assertEqual(1, len(matches))
        extracted = extract_match_odds(matches[0])
        self.assertIsNotNone(extracted)
        assert extracted is not None
        self.assertEqual(extracted["stat_match_id"], "900002")
        self.assertEqual(extracted["home_stat_team_id"], "club_a")
        self.assertGreater(
            extracted["expected_home_goals"], extracted["expected_away_goals"]
        )

    def test_calendar_query_asks_for_the_1x2_line(self) -> None:
        self.assertIn("bettingOdds", CALENDAR_ODDS_QUERY)
        self.assertIn("line1x2", CALENDAR_ODDS_QUERY)
        self.assertIn("SPORTS_TAG", CALENDAR_ODDS_QUERY)
        self.assertIn("sportsTag", TOURNAMENT_HUB_QUERY)


class DayBeforeTourTest(unittest.TestCase):
    def test_true_on_the_local_day_before_kickoff(self) -> None:
        tour = Mock(
            starts_at=datetime(2026, 8, 29, 16, 0, tzinfo=UTC),
            transfers_deadline_at=None,
        )
        now = datetime(2026, 8, 28, 12, 0, tzinfo=MOSCOW)
        self.assertTrue(is_day_before_tour(tour, now, MOSCOW))

    def test_false_two_days_out(self) -> None:
        tour = Mock(
            starts_at=datetime(2026, 8, 29, 16, 0, tzinfo=UTC),
            transfers_deadline_at=None,
        )
        now = datetime(2026, 8, 27, 12, 0, tzinfo=MOSCOW)
        self.assertFalse(is_day_before_tour(tour, now, MOSCOW))

    def test_catchup_on_the_start_day(self) -> None:
        tour = Mock(
            starts_at=datetime(2026, 8, 29, 16, 0, tzinfo=UTC),
            transfers_deadline_at=None,
        )
        now = datetime(2026, 8, 29, 8, 0, tzinfo=MOSCOW)
        self.assertTrue(is_day_before_tour(tour, now, MOSCOW, include_start_day=True))
        self.assertFalse(is_day_before_tour(tour, now, MOSCOW, include_start_day=False))


class OddsFakeClient:
    """Route hub + calendar queries to canned payloads."""

    def __init__(self, calendar: dict, tag: str = "1363803") -> None:
        self.calendar = calendar
        self.tag = tag
        self.calls: list[str] = []

    def execute(self, query: str, variables=None, *, operation_name=None) -> dict:
        if "TournamentHub" in query or "SPORTS_HUB" in query and "sportsTag" in query:
            self.calls.append("hub")
            return {
                "data": {
                    "statQueries": {
                        "football": {
                            "tournament": {
                                "id": "rfpl",
                                "name": "РПЛ",
                                "ubersetzer": {"sportsTag": self.tag},
                                "currentSeason": {"id": "rfpl_25-26", "name": "2025/2026"},
                            }
                        }
                    }
                }
            }
        self.calls.append("calendar")
        return self.calendar


@requires_database
class OddsIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)
        run_ingestion(
            FakeClient(_consistent_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        run_id = self._scalar("SELECT id FROM ingestion_runs LIMIT 1")
        run_quality_checks(self.session_factory, run_id=run_id)
        self._add_future_tour()

    def _exec(self, sql: str, **params):
        with self.engine.begin() as connection:
            return connection.execute(text(sql), params)

    def _scalar(self, sql: str, **params):
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar_one()

    def _add_future_tour(self) -> None:
        season_id = self._scalar("SELECT id FROM seasons LIMIT 1")
        club_a = self._scalar(
            "SELECT club_id FROM season_clubs WHERE fantasy_team_id = '10'"
        )
        club_b = self._scalar(
            "SELECT club_id FROM season_clubs WHERE fantasy_team_id = '20'"
        )
        tour_id = self._exec(
            """
            INSERT INTO fantasy_tours
                (season_id, fantasy_tour_id, name, status, starts_at,
                 transfers_deadline_at)
            VALUES (:season, '1773', '2 тур', 'SCHEDULED',
                    '2025-07-25T16:00:00Z', '2025-07-24T16:00:00Z')
            RETURNING id
            """,
            season=season_id,
        ).scalar_one()
        self._exec(
            """
            INSERT INTO matches
                (season_id, tour_id, stat_match_id, scheduled_at,
                 home_club_id, away_club_id, home_score, away_score)
            VALUES (:season, :tour, '900002', '2025-07-25T16:00:00Z',
                    :home, :away, NULL, NULL)
            """,
            season=season_id,
            tour=tour_id,
            home=club_a,
            away=club_b,
        )

    def test_persisted_odds_move_expected_points(self) -> None:
        without = build_forecast_dataset(self.session_factory, tour_ref="1773")
        without_fwd = next(
            row
            for row in without["rows"]
            if row["model_name"] == MODEL_EVENT and row["role"] == "FORWARD"
        )

        with session_scope(self.session_factory) as session:
            match = session.execute(
                text("SELECT id FROM matches WHERE stat_match_id = '900002'")
            ).scalar_one()
            season = session.execute(select_season()).scalar_one()
            line = line_to_expected_goals(1.30, 5.50, 10.0)
            session.add(
                MatchOdds(
                    season_id=season,
                    match_id=match,
                    stat_match_id="900002",
                    home_stat_team_id="club_a",
                    away_stat_team_id="club_b",
                    bookmaker="test",
                    home_odds=line["home_odds"],
                    draw_odds=line["draw_odds"],
                    away_odds=line["away_odds"],
                    implied_home=line["implied_home"],
                    implied_draw=line["implied_draw"],
                    implied_away=line["implied_away"],
                    expected_home_goals=line["expected_home_goals"],
                    expected_away_goals=line["expected_away_goals"],
                    captured_at=datetime.now(UTC),
                    raw={},
                )
            )

        with_odds = build_forecast_dataset(self.session_factory, tour_ref="1773")
        with_fwd = next(
            row
            for row in with_odds["rows"]
            if row["model_name"] == MODEL_EVENT and row["role"] == "FORWARD"
        )
        self.assertGreater(
            float(with_fwd["expected_points"]),
            float(without_fwd["expected_points"]),
        )
        self.assertGreater(
            with_fwd["params"]["expected"]["per_fixture"][0]["attack_scale"],
            1.0,
        )

        with_gk = next(
            row
            for row in with_odds["rows"]
            if row["model_name"] == MODEL_EVENT and row["role"] == "GOALKEEPER"
        )
        without_gk = next(
            row
            for row in without["rows"]
            if row["model_name"] == MODEL_EVENT and row["role"] == "GOALKEEPER"
        )
        # The keeper is on the away side of a heavy home favourite, so the
        # priced shutout belongs to the home club and the away clean-sheet
        # component must fall.
        self.assertLess(
            float(with_gk["components"]["clean_sheet"]),
            float(without_gk["components"]["clean_sheet"]),
        )

    def test_refresh_league_odds_links_the_calendar_and_rebuilds(self) -> None:
        client = OddsFakeClient(_calendar_payload("900002"))
        report = refresh_league_odds(
            client, self.session_factory, tournament_slug="russia"
        )
        self.assertEqual(report["stored"], 1)
        self.assertEqual(report["linked"], 1)
        self.assertGreater(report["forecast_rows"], 0)
        self.assertIn("hub", client.calls)
        self.assertIn("calendar", client.calls)

        with session_scope(self.session_factory) as session:
            season = session.execute(select_season()).scalar_one()
            self.assertIsNotNone(
                session.get(Season, season).odds_synced_at
            )
            odds = session.execute(
                text("SELECT match_id FROM match_odds WHERE stat_match_id = '900002'")
            ).scalar_one()
            self.assertIsNotNone(odds)

    def test_refresh_does_not_link_by_club_pair(self) -> None:
        """A later season's calendar id must not attach to this season's fixture.

        The same home/away pairing repeats every year; joining on clubs would
        stamp a 2026/2027 1x2 onto a 2025/2026 match.
        """
        client = OddsFakeClient(_calendar_payload("999999"))
        report = refresh_league_odds(
            client, self.session_factory, tournament_slug="russia"
        )
        self.assertEqual(report["stored"], 1)
        self.assertEqual(report["linked"], 0)
        self.assertEqual(report["unmatched"], 1)
        with session_scope(self.session_factory) as session:
            match_id = session.execute(
                text("SELECT match_id FROM match_odds WHERE stat_match_id = '999999'")
            ).scalar_one()
            self.assertIsNone(match_id)

    def test_admin_odds_endpoint_uses_the_injected_refresher(self) -> None:
        captured: list[str] = []

        def fake_refresh(_factory, *, tournament_slug: str) -> dict:
            captured.append(tournament_slug)
            return {
                "tournament_slug": tournament_slug,
                "competition_name": "Россия",
                "season_id": 1,
                "season_name": "2025/2026",
                "sports_tag_id": "1363803",
                "synced_at": "2026-08-25T12:00:00Z",
                "calendar_matches": 1,
                "fetched": 1,
                "stored": 1,
                "linked": 1,
                "unmatched": 0,
                "tour_id": 2,
                "tour_name": "2 тур",
                "run_id": 1,
                "forecast_rows": 6,
            }

        app = create_app(
            session_factory=self.session_factory,
            spawn_worker=lambda job_id: None,
            refresh_odds=fake_refresh,
        )
        response = TestClient(app).post("/admin/ingestion/russia/odds")
        self.assertEqual(200, response.status_code)
        self.assertEqual(["russia"], captured)
        self.assertEqual(1, response.json()["linked"])
        self.assertEqual(6, response.json()["forecast_rows"])

        status = TestClient(app).get("/admin/ingestion/russia/status")
        self.assertEqual(200, status.status_code)
        # No odds have been persisted in this particular call, so the block
        # is still present (season exists) with matches=0 until a real fetch.
        self.assertIn("odds", status.json())


def select_season():
    from sqlalchemy import select

    return select(Season.id)


if __name__ == "__main__":
    unittest.main()
