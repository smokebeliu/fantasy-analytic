"""Unit and integration tests for the baseline forecast (development-plan step 7).

The unit tests exercise the pure, interpretable pieces of the model in isolation
(Poisson helpers, the team-goal blend, the appearance split, the event model's
component algebra and the two baselines). The integration tests import a small
synthetic season into a real PostgreSQL database, publish it through the quality
gate, add a future tour and then build and persist forecasts to prove that every
player with a fixture is forecast, that the additive components always sum to the
expected points, that recomputing on the same snapshot is deterministic and that
persistence is idempotent. They are skipped automatically when no database is
reachable.
"""

from __future__ import annotations

import math
import os
import unittest
from datetime import datetime, timezone

from sqlalchemy import text

from fantasy_analytics.db import (
    ForecastRepository,
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.forecast import (
    MODEL_EVENT,
    MODEL_MEAN,
    MODEL_RECENT,
    MODEL_VERSION,
    SCORING,
    SCORING_VERSION,
    ForecastError,
    _forecast_rows_for_player,
    appearance_probabilities,
    build_forecast_dataset,
    clean_sheet_probability,
    expected_threshold_count,
    forecast_event_model,
    forecast_mean_baseline,
    forecast_recent_baseline,
    poisson_pmf,
    run_forecast,
    team_goal_means,
    tour_fixtures,
)
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.quality import run_quality_checks

# Reuse the fake client and internally consistent fixture from the siblings.
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


def _feature_row(**overrides) -> dict:
    """A feature row with sane defaults for a fit, ever-present midfielder."""
    row = {
        "player_season_id": 1,
        "fantasy_player_id": "111",
        "player_name": "Test Player",
        "role": "MIDFIELDER",
        "club_id": 1,
        "club_name": "Club A",
        "opponent_club_id": 2,
        "opponent_name": "Club B",
        "match_id": 10,
        "is_home": True,
        "is_available": True,
        "availability_status": "OK",
        "price": 10.0,
        "feature_version": "1.1.0",
        "p_appearance": 1.0,
        "expected_minutes": 90.0,
        "start_share": 1.0,
        "appearance_share": 1.0,
        "goals_per90": 0.0,
        "assists_per90": 0.0,
        "saves_per90": 0.0,
        "recoveries_per90": 0.0,
        "yellows_per90": 0.0,
        "club_attack": 1.0,
        "club_defense": 1.0,
        "opponent_attack": 1.0,
        "opponent_defense": 1.0,
        "total_appearances": 5,
        "total_points": 25.0,
        "points_avg_5": 6.0,
    }
    row.update(overrides)
    return row


class PoissonHelperTest(unittest.TestCase):
    def test_poisson_pmf_known_values(self) -> None:
        self.assertAlmostEqual(poisson_pmf(0, 2.0), math.exp(-2.0), places=6)
        self.assertAlmostEqual(poisson_pmf(1, 2.0), 2.0 * math.exp(-2.0), places=6)
        self.assertAlmostEqual(poisson_pmf(2, 2.0), 2.0 * math.exp(-2.0), places=6)
        self.assertEqual(poisson_pmf(0, 0.0), 1.0)
        self.assertEqual(poisson_pmf(1, 0.0), 0.0)

    def test_poisson_rejects_negative_mean(self) -> None:
        with self.assertRaises(ValueError):
            poisson_pmf(0, -1.0)

    def test_clean_sheet_probability_is_poisson_zero(self) -> None:
        self.assertEqual(clean_sheet_probability(0.0), 1.0)
        self.assertAlmostEqual(clean_sheet_probability(1.0), math.exp(-1.0), places=6)
        # A negative mean is clamped to zero -> certain clean sheet.
        self.assertEqual(clean_sheet_probability(-3.0), 1.0)

    def test_team_goal_means_blend_is_symmetric(self) -> None:
        goals_for, goals_against = team_goal_means(2.0, 1.0, 0.5, 1.5)
        self.assertEqual(goals_for, 0.5 * (2.0 + 1.5))
        self.assertEqual(goals_against, 0.5 * (1.0 + 0.5))

    def test_appearance_probabilities_split(self) -> None:
        p_full, p_sub = appearance_probabilities(1.0, 0.6, 1.0)
        self.assertAlmostEqual(p_full, 0.6)
        self.assertAlmostEqual(p_sub, 0.4)
        # No history -> nothing is credited as a full start.
        self.assertEqual(appearance_probabilities(0.8, 0.0, 0.0), (0.0, 0.8))


class ScoringTableTest(unittest.TestCase):
    def test_goal_rewards_match_position(self) -> None:
        self.assertEqual(SCORING["GOALKEEPER"].goal, 6)
        self.assertEqual(SCORING["DEFENDER"].goal, 6)
        self.assertEqual(SCORING["MIDFIELDER"].goal, 5)
        self.assertEqual(SCORING["FORWARD"].goal, 4)

    def test_clean_sheet_and_concede_are_defensive_only(self) -> None:
        self.assertEqual(SCORING["GOALKEEPER"].clean_sheet, 4)
        self.assertEqual(SCORING["DEFENDER"].clean_sheet, 4)
        self.assertEqual(SCORING["MIDFIELDER"].clean_sheet, 1)
        self.assertEqual(SCORING["FORWARD"].clean_sheet, 0)
        self.assertEqual(SCORING["GOALKEEPER"].conceded_per_two, -1)
        self.assertEqual(SCORING["MIDFIELDER"].conceded_per_two, 0)

    def test_only_outfield_players_are_paid_for_recoveries(self) -> None:
        self.assertEqual(SCORING["GOALKEEPER"].recovery_per_three, 0)
        for role in ("DEFENDER", "MIDFIELDER", "FORWARD"):
            self.assertEqual(SCORING[role].recovery_per_three, 1)


class ThresholdRewardTest(unittest.TestCase):
    """The per-N rewards are paid in whole blocks, not pro rata."""

    def test_zero_mean_pays_nothing(self) -> None:
        self.assertEqual(expected_threshold_count(0.0, 3), 0.0)

    def test_below_the_threshold_is_worth_much_less_than_the_ratio(self) -> None:
        # Two recoveries on average pay far less than two thirds of a point.
        self.assertLess(expected_threshold_count(2.0, 3), 2.0 / 3.0 - 0.2)

    def test_matches_a_brute_force_expectation(self) -> None:
        for mean in (0.5, 2.0, 3.0, 8.4):
            for step in (2, 3):
                brute = sum(
                    poisson_pmf(count, mean) * (count // step)
                    for count in range(0, 60)
                )
                self.assertAlmostEqual(
                    expected_threshold_count(mean, step), brute, places=8
                )

    def test_stays_below_the_linear_approximation(self) -> None:
        # The linear form is what the model used to charge, and it always
        # overpays, by roughly (step - 1) / (2 * step) of a block.
        for mean in (1.0, 3.0, 6.0, 9.0):
            self.assertLess(expected_threshold_count(mean, 3), mean / 3.0)

    def test_rejects_a_non_positive_step(self) -> None:
        with self.assertRaises(ValueError):
            expected_threshold_count(3.0, 0)


class EventModelTest(unittest.TestCase):
    def test_components_sum_to_expected_points(self) -> None:
        row = _feature_row(
            goals_per90=0.5,
            assists_per90=0.25,
            recoveries_per90=3.0,
            club_attack=2.0,
            opponent_defense=1.0,
            club_defense=1.0,
            opponent_attack=1.0,
        )
        result = forecast_event_model(row)
        self.assertAlmostEqual(
            sum(result["components"].values()), result["expected_points"], places=4
        )
        c = result["components"]
        self.assertEqual(c["appearance"], 2.0)  # full appearance
        self.assertAlmostEqual(c["goals"], 2.5, places=4)  # MID goal 5 * 0.5
        self.assertAlmostEqual(c["assists"], 0.75, places=4)  # 3 * 0.25
        # Three recoveries a match do not pay a whole point: the reward needs
        # three *completed*, and a match with two of them is worth nothing.
        self.assertAlmostEqual(
            c["recoveries"], expected_threshold_count(3.0, 3), places=4
        )
        self.assertLess(c["recoveries"], 1.0)
        # Clean sheet: MID(1) * p_full(1) * exp(-1.0).
        self.assertAlmostEqual(c["clean_sheet"], math.exp(-1.0), places=3)
        self.assertGreater(result["uncertainty"], 0.0)

    def test_block_rewards_are_conditional_on_playing(self) -> None:
        # A rotation player who plays half the time, a full match when he does:
        # his recoveries are the whole-block expectation of a *played* match,
        # weighted by the chance he plays — not the floor of a halved mean,
        # which is far less than half (1.4.0).
        half = _feature_row(
            role="DEFENDER", p_appearance=0.5, expected_minutes=45.0,
            start_share=0.5, appearance_share=0.5, recoveries_per90=6.0,
        )
        full = _feature_row(
            role="DEFENDER", p_appearance=1.0, expected_minutes=90.0, recoveries_per90=6.0,
        )
        half_pts = forecast_event_model(half)["components"]["recoveries"]
        full_pts = forecast_event_model(full)["components"]["recoveries"]
        self.assertAlmostEqual(half_pts, 0.5 * full_pts, places=4)
        self.assertAlmostEqual(full_pts, expected_threshold_count(6.0, 3), places=4)
        self.assertGreater(half_pts, expected_threshold_count(3.0, 3))

    def test_concession_penalty_scales_with_minutes_on_the_pitch(self) -> None:
        # Sports.ru only counts the goals conceded while the player is on the
        # pitch, so a 30-minute substitute faces a third of the match rate.
        starter = _feature_row(
            role="DEFENDER", p_appearance=1.0, expected_minutes=90.0,
            club_defense=3.0, opponent_attack=3.0,
        )
        substitute = _feature_row(
            role="DEFENDER", p_appearance=1.0, expected_minutes=30.0,
            start_share=0.0, appearance_share=1.0,
            club_defense=3.0, opponent_attack=3.0,
        )
        starter_pts = forecast_event_model(starter)["components"]["conceded"]
        sub_pts = forecast_event_model(substitute)["components"]["conceded"]
        self.assertAlmostEqual(starter_pts, -expected_threshold_count(3.0, 2), places=4)
        self.assertAlmostEqual(sub_pts, -expected_threshold_count(1.0, 2), places=4)
        self.assertGreater(sub_pts, starter_pts)
        # The exposure the optimizer prices follows the same share.
        self.assertAlmostEqual(
            forecast_event_model(substitute)["fixture"]["shutout_stake"],
            0.5 / 3.0,
            places=4,
        )

    def test_unavailable_player_scores_zero(self) -> None:
        row = _feature_row(is_available=False, goals_per90=1.0)
        result = forecast_event_model(row)
        self.assertEqual(result["expected_points"], 0.0)
        self.assertEqual(result["uncertainty"], 0.0)
        self.assertTrue(all(v == 0.0 for v in result["components"].values()))

    def test_zero_play_probability_scores_zero(self) -> None:
        row = _feature_row(p_appearance=0.0, expected_minutes=0.0, goals_per90=1.0)
        self.assertEqual(forecast_event_model(row)["expected_points"], 0.0)

    def test_goalkeeper_saves_and_clean_sheet(self) -> None:
        row = _feature_row(
            role="GOALKEEPER",
            saves_per90=3.0,
            opponent_attack=0.0,
            club_defense=0.0,
        )
        result = forecast_event_model(row)
        # opponent scores 0 on average -> clean-sheet probability 1.0 -> +4.
        self.assertAlmostEqual(result["components"]["clean_sheet"], 4.0, places=4)
        self.assertAlmostEqual(
            result["components"]["saves"], expected_threshold_count(3.0, 3), places=4
        )

    def test_goalkeeper_is_not_paid_for_ball_recoveries(self) -> None:
        # A keeper is credited with every claimed cross and collected back-pass,
        # around eight a match. Paying the outfield rate for them handed him ~2.3
        # points he never scored and had the optimizer filling squads with cheap
        # keepers instead of forwards.
        keeper = forecast_event_model(
            _feature_row(role="GOALKEEPER", recoveries_per90=9.0)
        )
        defender = forecast_event_model(
            _feature_row(role="DEFENDER", recoveries_per90=9.0)
        )
        self.assertEqual(keeper["components"]["recoveries"], 0.0)
        self.assertGreater(defender["components"]["recoveries"], 2.0)

    def test_deterministic(self) -> None:
        row = _feature_row(goals_per90=0.7, assists_per90=0.3)
        self.assertEqual(forecast_event_model(row), forecast_event_model(row))


class FixtureExposureTest(unittest.TestCase):
    """Step 16: the exposures the optimizer prices head-to-head clashes with."""

    def test_goal_upside_is_the_goal_and_assist_points(self) -> None:
        row = _feature_row(role="FORWARD", goals_per90=0.5, assists_per90=0.25)
        result = forecast_event_model(row)
        components = result["components"]
        self.assertAlmostEqual(
            result["fixture"]["goal_upside"],
            components["goals"] + components["assists"],
            places=4,
        )

    def test_shutout_stake_adds_the_concession_penalty(self) -> None:
        # A full-time defender: forfeits the clean sheet and -1 per 2 conceded.
        row = _feature_row(role="DEFENDER", p_appearance=1.0)
        result = forecast_event_model(row)
        self.assertAlmostEqual(
            result["fixture"]["shutout_stake"],
            result["components"]["clean_sheet"] + 0.5,
            places=4,
        )

    def test_forward_has_nothing_to_lose_to_a_conceded_goal(self) -> None:
        row = _feature_row(role="FORWARD", goals_per90=0.5)
        self.assertEqual(forecast_event_model(row)["fixture"]["shutout_stake"], 0.0)

    def test_unavailable_player_has_no_exposure(self) -> None:
        row = _feature_row(role="DEFENDER", is_available=False, goals_per90=1.0)
        fixture = forecast_event_model(row)["fixture"]
        self.assertEqual(fixture, {"goal_upside": 0.0, "shutout_stake": 0.0})

    def test_exposures_are_persisted_in_the_event_model_params(self) -> None:
        rows = _forecast_rows_for_player(_feature_row(role="DEFENDER"), "2025-07-24")
        event = next(row for row in rows if row["model_name"] == MODEL_EVENT)
        self.assertIn("fixture", event["params"])
        self.assertIn("goal_upside", event["params"]["fixture"])
        # The baselines do not decompose their points into events.
        for row in rows:
            if row["model_name"] != MODEL_EVENT:
                self.assertIsNone(row["params"])


class DoubleGameweekTest(unittest.TestCase):
    """A fantasy tour is a slice of the calendar, so a club can play twice."""

    def _double(self, **overrides) -> dict:
        row = _feature_row(**overrides)
        row["tour_fixtures"] = [
            {
                "match_id": 10,
                "is_home": True,
                "opponent_club_id": 2,
                "opponent_name": "Club B",
                "club_attack": row["club_attack"],
                "club_defense": row["club_defense"],
                "opponent_attack": row["opponent_attack"],
                "opponent_defense": row["opponent_defense"],
            },
            {
                "match_id": 11,
                "is_home": False,
                "opponent_club_id": 3,
                "opponent_name": "Club C",
                "club_attack": row["club_attack"],
                "club_defense": row["club_defense"],
                "opponent_attack": row["opponent_attack"],
                "opponent_defense": row["opponent_defense"],
            },
        ]
        row["fixture_count"] = 2
        return row

    def test_two_identical_fixtures_score_exactly_twice(self) -> None:
        single = forecast_event_model(_feature_row(goals_per90=0.5, assists_per90=0.25))
        double = forecast_event_model(self._double(goals_per90=0.5, assists_per90=0.25))
        self.assertAlmostEqual(
            2 * single["expected_points"], double["expected_points"], places=3
        )
        for key, value in single["components"].items():
            self.assertAlmostEqual(2 * value, double["components"][key], places=3)

    def test_a_row_without_the_list_is_a_one_match_tour(self) -> None:
        # Older rows, and every hand-built one, carry a single flattened fixture.
        row = _feature_row(goals_per90=0.5)
        self.assertEqual(1, len(forecast_event_model(row)["fixtures"]))
        self.assertEqual([10], [f["match_id"] for f in tour_fixtures(row)])

    def test_each_match_is_scored_against_its_own_opponent(self) -> None:
        row = self._double(role="DEFENDER")
        # A shutout is a near-certainty in the first match and hopeless in the
        # second, so the two clean sheets must differ.
        row["tour_fixtures"][0]["opponent_attack"] = 0.0
        row["tour_fixtures"][0]["club_defense"] = 0.0
        row["tour_fixtures"][1]["opponent_attack"] = 6.0
        row["tour_fixtures"][1]["club_defense"] = 6.0
        result = forecast_event_model(row)
        first, second = result["expected"]["per_fixture"]
        self.assertGreater(first["clean_sheet_probability"], 0.9)
        self.assertLess(second["clean_sheet_probability"], 0.01)
        self.assertGreater(first["expected_points"], second["expected_points"])

    def test_the_exposures_are_reported_per_match(self) -> None:
        result = forecast_event_model(self._double(goals_per90=1.0))
        exposures = result["fixtures"]
        self.assertEqual([10, 11], [f["match_id"] for f in exposures])
        self.assertAlmostEqual(
            result["fixture"]["goal_upside"],
            sum(f["goal_upside"] for f in exposures),
            places=4,
        )

    def test_the_uncertainty_grows_with_the_second_match(self) -> None:
        single = forecast_event_model(_feature_row(goals_per90=0.5))
        double = forecast_event_model(self._double(goals_per90=0.5))
        self.assertAlmostEqual(
            double["uncertainty"], math.sqrt(2) * single["uncertainty"], places=3
        )

    def test_both_baselines_double_up_too(self) -> None:
        single = _feature_row(total_appearances=5, total_points=25.0, points_avg_5=6.0)
        double = self._double(
            total_appearances=5, total_points=25.0, points_avg_5=6.0
        )
        self.assertAlmostEqual(
            2 * forecast_mean_baseline(single)["expected_points"],
            forecast_mean_baseline(double)["expected_points"],
            places=4,
        )
        self.assertAlmostEqual(
            2 * forecast_recent_baseline(single)["expected_points"],
            forecast_recent_baseline(double)["expected_points"],
            places=4,
        )

    def test_an_unavailable_player_still_scores_zero_twice_over(self) -> None:
        row = self._double(is_available=False, goals_per90=1.0)
        result = forecast_event_model(row)
        self.assertEqual(0.0, result["expected_points"])
        self.assertEqual(0.0, result["uncertainty"])
        self.assertTrue(all(f["goal_upside"] == 0.0 for f in result["fixtures"]))


class BaselineTest(unittest.TestCase):
    def test_mean_baseline(self) -> None:
        row = _feature_row(total_appearances=5, total_points=25.0, p_appearance=0.8)
        result = forecast_mean_baseline(row)
        # mean 5.0 * 0.8 play probability.
        self.assertAlmostEqual(result["expected_points"], 4.0, places=4)
        self.assertEqual(result["components"]["mean_points_per_appearance"], 5.0)

    def test_mean_baseline_no_history(self) -> None:
        row = _feature_row(total_appearances=0, total_points=0.0)
        self.assertEqual(forecast_mean_baseline(row)["expected_points"], 0.0)

    def test_recent_baseline(self) -> None:
        row = _feature_row(points_avg_5=6.0, p_appearance=0.5)
        self.assertAlmostEqual(
            forecast_recent_baseline(row)["expected_points"], 3.0, places=4
        )

    def test_baselines_respect_availability(self) -> None:
        row = _feature_row(is_available=False, total_points=25.0, points_avg_5=6.0)
        self.assertEqual(forecast_mean_baseline(row)["expected_points"], 0.0)
        self.assertEqual(forecast_recent_baseline(row)["expected_points"], 0.0)


@requires_database
class ForecastIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)
        self.run_id = self._import_and_publish()
        self._add_future_tour()

    def _import_and_publish(self) -> int:
        report = run_ingestion(
            FakeClient(_consistent_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        run_quality_checks(self.session_factory, run_id=report["run_id"])
        return report["run_id"]

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

    def _event_rows(self, report: dict) -> dict[str, dict]:
        return {
            r["fantasy_player_id"]: r
            for r in report["rows"]
            if r["model_name"] == MODEL_EVENT
        }

    def test_forecasts_all_players_with_a_fixture(self) -> None:
        report = build_forecast_dataset(self.session_factory, tour_ref="1773")

        self.assertEqual(report["tour"]["fantasy_tour_id"], "1773")
        self.assertEqual(report["model_version"], MODEL_VERSION)
        self.assertEqual(report["scoring_version"], SCORING_VERSION)
        # Two players, three models each.
        self.assertEqual(report["counts"]["players"], 2)
        self.assertEqual(report["counts"]["rows"], 6)
        model_names = {r["model_name"] for r in report["rows"]}
        self.assertEqual(model_names, {MODEL_EVENT, MODEL_MEAN, MODEL_RECENT})

    def test_components_sum_to_expected_points(self) -> None:
        report = build_forecast_dataset(self.session_factory, tour_ref="1773")
        for row in report["rows"]:
            if row["model_name"] != MODEL_EVENT:
                continue
            self.assertAlmostEqual(
                sum(row["components"].values()),
                float(row["expected_points"]),
                places=4,
            )

    def test_forward_event_forecast_matches_history(self) -> None:
        # Player 111 (forward) has one pre-cutoff match: 78', 1 goal, 2 assists,
        # 5 recoveries, 1 yellow. Expected counts recover the raw stat line, so
        # expected points ~= 2(app) + 4(goal) + 6(assists) + 1.33(rec) - 1(yc),
        # the recoveries being the expected number of completed blocks of three.
        report = build_forecast_dataset(self.session_factory, tour_ref="1773")
        row = self._event_rows(report)["111"]
        self.assertEqual(row["role"], "FORWARD")
        c = row["components"]
        self.assertEqual(c["appearance"], 2.0)
        self.assertAlmostEqual(c["goals"], 4.0, places=1)
        self.assertAlmostEqual(c["assists"], 6.0, places=1)
        self.assertAlmostEqual(
            c["recoveries"], expected_threshold_count(5.0, 3), places=1
        )
        self.assertAlmostEqual(c["yellow_cards"], -1.0, places=1)
        self.assertEqual(c["clean_sheet"], 0.0)  # forwards get no clean sheet
        self.assertAlmostEqual(float(row["expected_points"]), 12.33, delta=0.1)

    def test_baselines_match_history(self) -> None:
        report = build_forecast_dataset(self.session_factory, tour_ref="1773")
        by_model = {
            (r["fantasy_player_id"], r["model_name"]): r for r in report["rows"]
        }
        # One appearance of 12 points, always playing -> both baselines = 12.
        self.assertAlmostEqual(
            float(by_model[("111", MODEL_MEAN)]["expected_points"]), 12.0, places=4
        )
        self.assertAlmostEqual(
            float(by_model[("111", MODEL_RECENT)]["expected_points"]), 12.0, places=4
        )

    def test_run_forecast_persists_idempotently(self) -> None:
        first = run_forecast(self.session_factory, tour_ref="1773")
        self.assertEqual(first["persisted"], 6)

        tour_id = first["tour"]["tour_id"]
        with self.session_factory() as session:
            repo = ForecastRepository(session)
            self.assertEqual(
                repo.count_forecasts(run_id=self.run_id, tour_id=tour_id), 6
            )

        # Re-running replaces rather than accumulates.
        second = run_forecast(self.session_factory, tour_ref="1773")
        self.assertEqual(second["persisted"], 6)
        with self.session_factory() as session:
            repo = ForecastRepository(session)
            self.assertEqual(
                repo.count_forecasts(run_id=self.run_id, tour_id=tour_id), 6
            )
            stored = repo.list_forecasts(
                run_id=self.run_id, tour_id=tour_id, model_name=MODEL_EVENT
            )
            self.assertEqual(len(stored), 2)

    def test_recompute_is_deterministic(self) -> None:
        a = build_forecast_dataset(self.session_factory, tour_ref="1773")
        b = build_forecast_dataset(self.session_factory, tour_ref="1773")
        a.pop("generated_at")
        b.pop("generated_at")
        self.assertEqual(a["rows"], b["rows"])

    def test_finished_season_without_tour_raises(self) -> None:
        # Mark the future tour finished so no implicit "next tour" remains.
        self._exec("UPDATE fantasy_tours SET status = 'FINISHED'")
        with self.assertRaises(ForecastError):
            build_forecast_dataset(self.session_factory)

    def test_no_persist_leaves_table_empty(self) -> None:
        report = run_forecast(
            self.session_factory, tour_ref="1773", persist=False
        )
        self.assertEqual(report["persisted"], 0)
        with self.session_factory() as session:
            repo = ForecastRepository(session)
            self.assertEqual(
                repo.count_forecasts(
                    run_id=self.run_id, tour_id=report["tour"]["tour_id"]
                ),
                0,
            )


if __name__ == "__main__":
    unittest.main()
