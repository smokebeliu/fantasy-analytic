"""Unit and integration tests for walk-forward backtesting (step 19).

The unit tests exercise the pure pieces in isolation: the accuracy metrics and
how they pool across tours, the hindsight best eleven, the leakage audit (both a
clean dataset and two deliberately corrupted ones), the squad simulation against
known actual points, the feature-stability ranking and the decision rule.

The integration test imports a two-tour synthetic season into a real PostgreSQL
database, publishes it through the quality gate and backtests it end to end, so
the walk-forward loop, the audit over real rows and the reproducibility of the
report are covered against the database. It is skipped automatically when no
database is reachable.
"""

from __future__ import annotations

import copy
import os
import unittest
from datetime import datetime, timezone

from sqlalchemy import text

from fantasy_analytics.backtest import (
    ACCEPT_MODEL,
    BACKTEST_VERSION,
    KEEP_BASELINE,
    PRIMARY_MODEL,
    REVISE_MODEL,
    BacktestError,
    _pooled,
    audit_tour,
    best_eleven_points,
    decide,
    error_metrics,
    feature_instability,
    hindsight_squad,
    role_metrics,
    run_backtest,
    simulate_squad,
)
from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.features import Appearance, STAT_SOURCE_PRIOR
from fantasy_analytics.forecast import MODEL_EVENT, MODEL_MEAN, MODEL_RECENT
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.quality import run_quality_checks

# Reuse the fake client, the fixture and the optimizer's synthetic pool.
from test_ingestion import FakeClient, _game_stat
from test_optimizer import _pool, _rpl_rules
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


class ErrorMetricsTest(unittest.TestCase):
    def test_known_values(self) -> None:
        metrics = error_metrics([(5.0, 3.0), (1.0, 2.0), (4.0, 4.0)])

        self.assertEqual(3, metrics["n"])
        self.assertAlmostEqual(1.0, metrics["mae"], places=4)  # (2 + 1 + 0) / 3
        self.assertAlmostEqual((5 / 3) ** 0.5, metrics["rmse"], places=4)
        self.assertAlmostEqual(1 / 3, metrics["bias"], places=4)  # over-predicts
        self.assertAlmostEqual(10 / 3, metrics["mean_predicted"], places=4)
        self.assertAlmostEqual(3.0, metrics["mean_actual"], places=4)

    def test_empty_input(self) -> None:
        metrics = error_metrics([])
        self.assertEqual(0, metrics["n"])
        self.assertIsNone(metrics["mae"])

    def test_role_metrics_split_by_position(self) -> None:
        by_role = role_metrics(
            [("FORWARD", 5.0, 3.0), ("DEFENDER", 1.0, 1.0), ("FORWARD", 2.0, 4.0)]
        )
        self.assertEqual(2, by_role["FORWARD"]["n"])
        self.assertAlmostEqual(2.0, by_role["FORWARD"]["mae"], places=4)
        self.assertEqual(0.0, by_role["DEFENDER"]["mae"])
        self.assertEqual(0, by_role["MIDFIELDER"]["n"])

    def test_pooling_equals_a_single_pass(self) -> None:
        first = [(5.0, 3.0), (1.0, 2.0)]
        second = [(4.0, 4.0), (0.0, 6.0), (2.0, 1.0)]

        pooled = _pooled([error_metrics(first), error_metrics(second)])
        single = error_metrics(first + second)

        self.assertEqual(single["n"], pooled["n"])
        self.assertAlmostEqual(single["mae"], pooled["mae"], places=4)
        self.assertAlmostEqual(single["rmse"], pooled["rmse"], places=4)
        self.assertAlmostEqual(single["bias"], pooled["bias"], places=4)

    def test_pooling_ignores_empty_tours(self) -> None:
        pooled = _pooled([error_metrics([]), error_metrics([(2.0, 1.0)])])
        self.assertEqual(1, pooled["n"])
        self.assertEqual(1.0, pooled["mae"])


class BestElevenTest(unittest.TestCase):
    def test_picks_the_best_legal_distribution(self) -> None:
        rules = _rpl_rules()
        # Five strong defenders and weak midfielders: the best eleven must use
        # the maximum of five defenders and the minimum of two midfielders.
        squad = (
            [("GOALKEEPER", 5.0), ("GOALKEEPER", 1.0)]
            + [("DEFENDER", 10.0)] * 5
            + [("MIDFIELDER", 1.0)] * 5
            + [("FORWARD", 8.0)] * 3
        )
        # 1 GK (5) + 5 DEF (50) + 2 MID (2) + 3 FWD (24) = 81.
        self.assertEqual(81.0, best_eleven_points(squad, rules))

    def test_respects_the_starting_minimum(self) -> None:
        rules = _rpl_rules()
        squad = (
            [("GOALKEEPER", 4.0), ("GOALKEEPER", 0.0)]
            + [("DEFENDER", 0.0)] * 5
            + [("MIDFIELDER", 9.0)] * 5
            + [("FORWARD", 9.0)] * 3
        )
        # The three worthless defenders still have to start (minimum 3), and only
        # one forward can be dropped: 4 + 0 + 45 + 18 = 67 with 3 DEF/5 MID/2 FWD.
        self.assertEqual(67.0, best_eleven_points(squad, rules))


class AuditTest(unittest.TestCase):
    def _features(self, **overrides) -> dict:
        row = {
            "player_season_id": 1,
            "stat_source": "current_season",
            # The blended totals are what the model consumes; the audit checks
            # the unweighted current-season ones, which are the only numbers the
            # cutoff can be held responsible for.
            "total_appearances": 4.5,
            "total_minutes": 320.0,
            "total_points": 33.0,
            "current_appearances": 2,
            "current_minutes": 150,
            "current_points": 15,
        }
        row.update(overrides)
        return {"cutoff": "2025-08-01T00:00:00+00:00", "rows": [row]}

    def _appearances(self) -> dict[int, list[Appearance]]:
        return {
            1: [
                Appearance(
                    match_id=10,
                    scheduled_at=datetime(2025, 7, 20, tzinfo=timezone.utc),
                    minutes=90,
                    points=9,
                    goals=1,
                    assists=0,
                ),
                Appearance(
                    match_id=11,
                    scheduled_at=datetime(2025, 7, 27, tzinfo=timezone.utc),
                    minutes=60,
                    points=6,
                    goals=0,
                    assists=1,
                ),
                # The tour being predicted: must never be counted.
                Appearance(
                    match_id=12,
                    scheduled_at=datetime(2025, 8, 3, tzinfo=timezone.utc),
                    minutes=90,
                    points=20,
                    goals=3,
                    assists=0,
                ),
            ]
        }

    def test_clean_dataset_passes(self) -> None:
        audit = audit_tour(
            self._features(),
            appearances=self._appearances(),
            tour_match_ids=frozenset({12}),
            first_kickoff=datetime(2025, 8, 3, tzinfo=timezone.utc),
        )
        self.assertEqual([], audit["violations"])
        self.assertEqual(1, audit["rows_checked"])

    def test_leaked_history_is_reported(self) -> None:
        # The row's totals include the target tour's match, which is exactly what
        # the audit must catch without trusting the feature builder.
        leaked = self._features(
            current_appearances=3, current_minutes=240, current_points=35
        )
        audit = audit_tour(
            leaked,
            appearances=self._appearances(),
            tour_match_ids=frozenset({12}),
            first_kickoff=datetime(2025, 8, 3, tzinfo=timezone.utc),
        )
        self.assertTrue(any("current_points" in item for item in audit["violations"]))
        self.assertTrue(
            any("current_appearances" in item for item in audit["violations"])
        )

    def test_an_unused_substitute_does_not_count_as_history(self) -> None:
        # A 0-minute matchday row is a bench place, not an appearance, so a row
        # that counts it is reporting history it does not have.
        appearances = self._appearances()
        appearances[1].append(
            Appearance(
                match_id=13,
                scheduled_at=datetime(2025, 7, 29, tzinfo=timezone.utc),
                minutes=0,
                points=0,
                goals=0,
                assists=0,
            )
        )
        audit = audit_tour(
            self._features(),
            appearances=appearances,
            tour_match_ids=frozenset({12}),
            first_kickoff=datetime(2025, 8, 3, tzinfo=timezone.utc),
        )
        self.assertEqual([], audit["violations"])

    def test_cutoff_after_kickoff_is_a_warning_not_a_violation(self) -> None:
        # The source sometimes dates a deadline after kickoff; the target tour's
        # matches are excluded by id anyway, which the recomputation proves.
        audit = audit_tour(
            self._features(),
            appearances=self._appearances(),
            tour_match_ids=frozenset({12}),
            first_kickoff=datetime(2025, 7, 31, tzinfo=timezone.utc),
        )
        self.assertEqual([], audit["violations"])
        self.assertTrue(
            any("after the tour's first kickoff" in item for item in audit["warnings"])
        )

    def test_prior_sourced_rows_are_counted_and_still_checked(self) -> None:
        # A row leaning on last season is reported, but its current-season half
        # is still held to the cutoff: blending in another season is not a way
        # to smuggle this one's future in.
        audit = audit_tour(
            self._features(stat_source=STAT_SOURCE_PRIOR, total_points=999),
            appearances=self._appearances(),
            tour_match_ids=frozenset({12}),
            first_kickoff=datetime(2025, 8, 3, tzinfo=timezone.utc),
        )
        self.assertEqual([], audit["violations"])
        self.assertEqual(1, audit["rows_checked"])
        self.assertEqual(1, audit["rows_cross_season"])

        leaked = audit_tour(
            self._features(stat_source=STAT_SOURCE_PRIOR, current_points=999),
            appearances=self._appearances(),
            tour_match_ids=frozenset({12}),
            first_kickoff=datetime(2025, 8, 3, tzinfo=timezone.utc),
        )
        self.assertTrue(leaked["violations"])


class SimulationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = _pool()
        self.rules = _rpl_rules()
        # Actual points mirror the projections so the squad is scored on a known
        # scale; one obvious pick blanks so the captain choice can be judged.
        self.actuals = {
            candidate.player_season_id: candidate.expected_points
            for candidate in self.candidates
        }

    def test_actual_points_are_the_eleven_plus_the_captain(self) -> None:
        result = simulate_squad(self.candidates, self.rules, self.actuals)

        starters = [p for p in result["squad"] if p["is_starter"]]
        self.assertEqual(11, len(starters))
        expected = sum(p["actual_points"] for p in starters)
        self.assertAlmostEqual(
            expected + result["captain"]["actual_points"],
            result["actual_points"],
            places=4,
        )
        self.assertAlmostEqual(
            expected, result["actual_starting_points"], places=4
        )

    def test_captain_hit_is_detected(self) -> None:
        # With actuals equal to the projections the captain is the top scorer.
        result = simulate_squad(self.candidates, self.rules, self.actuals)
        self.assertTrue(result["captain"]["was_best_starter"])

        # Blank the captain and the flag must flip.
        blanked = dict(self.actuals)
        blanked[result["captain"]["player_season_id"]] = 0.0
        missed = simulate_squad(self.candidates, self.rules, blanked)
        self.assertFalse(missed["captain"]["was_best_starter"])
        self.assertEqual(0.0, missed["captain"]["actual_points"])

    def test_absent_player_scores_zero(self) -> None:
        result = simulate_squad(self.candidates, self.rules, {})
        self.assertEqual(0.0, result["actual_points"])
        self.assertEqual(0.0, result["best_eleven_actual_points"])
        self.assertIsNone(result["lineup_efficiency"])

    def test_best_eleven_bounds_the_chosen_eleven(self) -> None:
        result = simulate_squad(self.candidates, self.rules, self.actuals)
        self.assertGreaterEqual(
            result["best_eleven_actual_points"] + 1e-9,
            result["actual_starting_points"],
        )

    def test_hindsight_squad_is_an_upper_bound(self) -> None:
        model = simulate_squad(self.candidates, self.rules, self.actuals)
        perfect = hindsight_squad(self.candidates, self.rules, self.actuals)
        self.assertGreaterEqual(
            perfect["actual_points"] + 1e-9, model["actual_points"]
        )

    def test_hindsight_beats_a_misled_model(self) -> None:
        # Invert the actuals so the projections are actively misleading; the
        # hindsight squad must then score strictly more.
        inverted = {
            candidate.player_season_id: 20.0 - candidate.expected_points
            for candidate in self.candidates
        }
        model = simulate_squad(self.candidates, self.rules, inverted)
        perfect = hindsight_squad(self.candidates, self.rules, inverted)
        self.assertGreater(perfect["actual_points"], model["actual_points"])


class FeatureInstabilityTest(unittest.TestCase):
    def test_ranks_a_jumping_feature_above_a_stable_one(self) -> None:
        tours = [
            [
                {"player_season_id": 1, "stable": 1.0, "jumpy": 0.0},
                {"player_season_id": 2, "stable": 5.0, "jumpy": 10.0},
            ],
            [
                {"player_season_id": 1, "stable": 1.0, "jumpy": 10.0},
                {"player_season_id": 2, "stable": 5.0, "jumpy": 0.0},
            ],
        ]
        ranking = feature_instability(tours, top=5)
        by_name = {item["feature"]: item for item in ranking}

        self.assertEqual("jumpy", ranking[0]["feature"])
        self.assertEqual(0.0, by_name["stable"]["mean_abs_change"])
        self.assertGreater(by_name["jumpy"]["volatility"], 1.0)
        self.assertEqual(2, by_name["jumpy"]["transitions"])

    def test_identifier_and_boolean_columns_are_ignored(self) -> None:
        tours = [
            [{"player_season_id": 1, "club_id": 7, "is_home": True, "value": 1.0}],
            [{"player_season_id": 1, "club_id": 9, "is_home": False, "value": 2.0}],
        ]
        names = {item["feature"] for item in feature_instability(tours)}
        self.assertNotIn("club_id", names)
        self.assertNotIn("is_home", names)
        self.assertNotIn("player_season_id", names)
        self.assertIn("value", names)

    def test_constant_feature_is_dropped(self) -> None:
        tours = [
            [{"player_season_id": 1, "value": 3.0}],
            [{"player_season_id": 1, "value": 3.0}],
        ]
        self.assertEqual([], feature_instability(tours))


class DecisionTest(unittest.TestCase):
    @staticmethod
    def _summary(
        mae: float, points: float | None, mae_played: float | None = None
    ) -> dict:
        summary: dict = {
            "metrics": {"mae": mae},
            "metrics_played": {"mae": mae if mae_played is None else mae_played},
        }
        if points is not None:
            summary["squad"] = {"actual_points_total": points}
        return summary

    @staticmethod
    def _by_criterion(verdict: dict) -> dict[str, dict]:
        return {item["criterion"]: item for item in verdict["criteria"]}

    def test_accepts_a_model_that_wins_every_criterion(self) -> None:
        verdict = decide(
            {
                PRIMARY_MODEL: self._summary(1.5, 900.0),
                MODEL_MEAN: self._summary(1.9, 820.0),
                MODEL_RECENT: self._summary(2.1, 800.0),
            }
        )
        self.assertEqual(ACCEPT_MODEL, verdict["decision"])
        criteria = self._by_criterion(verdict)
        self.assertEqual(3, len(criteria))
        self.assertTrue(all(item["won"] for item in criteria.values()))
        self.assertEqual(MODEL_MEAN, criteria["mae_all"]["best_baseline_model"])

    def test_revises_a_model_that_wins_only_some(self) -> None:
        # Better where it matters (players who played) and better squads, but a
        # worse average over the whole pool: an explicit "needs work" signal.
        verdict = decide(
            {
                PRIMARY_MODEL: self._summary(2.0, 900.0, mae_played=1.9),
                MODEL_MEAN: self._summary(1.5, 820.0, mae_played=2.2),
            }
        )
        self.assertEqual(REVISE_MODEL, verdict["decision"])
        criteria = self._by_criterion(verdict)
        self.assertTrue(criteria["mae_played"]["won"])
        self.assertTrue(criteria["squad_points"]["won"])
        self.assertFalse(criteria["mae_all"]["won"])
        self.assertIn("every selectable player", verdict["reason"])

    def test_keeps_the_baseline_when_it_wins_everything(self) -> None:
        verdict = decide(
            {
                PRIMARY_MODEL: self._summary(2.4, 700.0),
                MODEL_RECENT: self._summary(1.8, 900.0),
            }
        )
        self.assertEqual(KEEP_BASELINE, verdict["decision"])
        self.assertIn("baselines win on every criterion", verdict["reason"])

    def test_accuracy_only_run_decides_without_squads(self) -> None:
        verdict = decide(
            {
                PRIMARY_MODEL: self._summary(1.2, None),
                MODEL_MEAN: self._summary(1.4, None),
            }
        )
        self.assertEqual(ACCEPT_MODEL, verdict["decision"])
        self.assertEqual(
            {"mae_played", "mae_all"}, set(self._by_criterion(verdict))
        )

    def test_without_a_baseline_the_model_is_not_accepted(self) -> None:
        verdict = decide({PRIMARY_MODEL: self._summary(1.0, 100.0)})
        self.assertEqual(REVISE_MODEL, verdict["decision"])
        self.assertEqual([], verdict["criteria"])


def _two_tour_fixture() -> dict:
    """A synthetic season with two finished tours, so tour 2 has real history.

    Both players appear in both tours; the season aggregate is the sum of the two
    matches so the quality gate's reconciliation stays clean.
    """
    fixture = copy.deepcopy(_consistent_fixture())
    season = fixture["season"]["data"]["fantasyQueries"]["season"]

    first_tour = season["tours"][0]
    second_tour = copy.deepcopy(first_tour)
    second_tour.update(
        {
            "id": "1773",
            "name": "2 тур",
            "startedAt": "2025-07-25T17:30:00Z",
            "finishedAt": "2025-07-28T17:30:00Z",
            "transfersStartedAt": "2025-07-21T00:00:00Z",
            "transfersFinishedAt": "2025-07-25T17:00:00Z",
        }
    )
    second_tour["matches"] = [
        {
            "id": "900002",
            "scheduledAt": "2025-07-25T17:30:00Z",
            "matchStatus": "CLOSED",
            "home": {"score": 1, "team": {"id": "club_b", "name": "Клуб B"}},
            "away": {"score": 1, "team": {"id": "club_a", "name": "Клуб A"}},
        }
    ]
    season["tours"] = [first_tour, second_tour]

    # Two match rows per player: 12 points in tour 1 and 5 in tour 2.
    for player_id, (team_id, stat_team) in {
        "111": ("10", "club_a"),
        "222": ("20", "club_b"),
    }.items():
        history = fixture["histories"][player_id]
        matches = history["data"]["fantasyQueries"]["season"]["players"]["list"][0][
            "matches"
        ]
        second = copy.deepcopy(matches["matches"][0])
        second["match"] = {"id": "900002", "scheduledAt": "2025-07-25T17:30:00Z"}
        second["tour"] = {"id": "1773", "name": "2 тур", "status": "FINISHED"}
        second["playerMatchInfo"] = _game_stat(45, 5)
        second["statDetails"] = []
        matches["matches"].append(second)
        matches["pageInfo"]["totalCount"] = 2

    # The season aggregates must equal the sum of the two matches, otherwise the
    # quality gate refuses to publish the snapshot and nothing can be backtested.
    for player in fixture["players"]["data"]["fantasyQueries"]["players"]["list"]:
        aggregate = _game_stat(78 + 45, 12 + 5)
        for key in ("goals", "assists", "yellowCards", "ballRecovery"):
            aggregate[key] *= 2
        player["gameStat"] = aggregate

    # Club A won 2:0 and drew 1:1; Club B lost and drew.
    stat_season = fixture["team_stats"]["data"]["stat_season"][0]
    stat_season["team0"] = {
        "MatchesPlayed": 2,
        "MatchesWon": 1,
        "MatchesDrawn": 1,
        "MatchesLost": 0,
        "GoalsScored": 3,
        "GoalsConceded": 1,
        "YellowCards": 2,
        "RedCards": 0,
    }
    stat_season["team1"] = {
        "MatchesPlayed": 2,
        "MatchesWon": 0,
        "MatchesDrawn": 1,
        "MatchesLost": 1,
        "GoalsScored": 1,
        "GoalsConceded": 3,
        "YellowCards": 2,
        "RedCards": 0,
    }

    return fixture


@requires_database
class BacktestIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)
        report = run_ingestion(
            FakeClient(_two_tour_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        run_quality_checks(self.session_factory, run_id=report["run_id"])
        self.run_id = report["run_id"]

    def test_replays_every_played_tour_without_leakage(self) -> None:
        # The synthetic season has two players, far too few for a 15-man squad,
        # so only the forecast accuracy is measured here.
        report = run_backtest(self.session_factory, optimize=False)

        self.assertEqual(BACKTEST_VERSION, report["backtest_version"])
        self.assertEqual(self.run_id, report["run_id"])
        self.assertEqual(2, report["counts"]["tours_evaluated"])
        self.assertEqual(0, report["counts"]["tours_skipped"])
        self.assertTrue(report["cutoff_audit"]["passed"])
        self.assertEqual([], report["cutoff_audit"]["violations"])
        self.assertGreater(report["cutoff_audit"]["rows_checked"], 0)
        # The fixture dates tour 1's deadline 20 minutes after kickoff, which is
        # reported as a warning while the recomputed history stays clean.
        self.assertEqual(1, len(report["cutoff_audit"]["warnings"]))
        self.assertEqual("1 тур", report["cutoff_audit"]["warnings"][0]["name"])

        # Every requested model produced pooled metrics and a per-tour series.
        for model in (MODEL_EVENT, MODEL_MEAN, MODEL_RECENT):
            summary = report["models"][model]
            self.assertEqual(4, summary["metrics"]["n"])  # 2 players x 2 tours
            self.assertEqual(2, len(summary["by_tour"]))
            self.assertIsNotNone(summary["metrics"]["mae"])
        self.assertIn(
            report["verdict"]["decision"], (ACCEPT_MODEL, REVISE_MODEL, KEEP_BASELINE)
        )

    def test_second_tour_is_forecast_from_the_first_only(self) -> None:
        report = run_backtest(self.session_factory, optimize=False)

        first, second = report["tours"]
        self.assertEqual("1 тур", first["name"])
        self.assertEqual("2 тур", second["name"])
        # Tour 1 has no history before its cutoff, so the season-mean baseline
        # must predict nothing; tour 2 sees exactly the 12 points of tour 1.
        first_mean = first["models"][MODEL_MEAN]["metrics"]
        second_mean = second["models"][MODEL_MEAN]["metrics"]
        self.assertEqual(0.0, first_mean["mean_predicted"])
        self.assertAlmostEqual(12.0, second_mean["mean_predicted"], places=4)
        self.assertAlmostEqual(5.0, second_mean["mean_actual"], places=4)

    def test_single_tour_selection_and_reproducibility(self) -> None:
        first = run_backtest(self.session_factory, tour_refs=["1773"], optimize=False)
        second = run_backtest(self.session_factory, tour_refs=["1773"], optimize=False)

        self.assertEqual(["1773"], first["params"]["tours"])
        self.assertEqual(1, first["counts"]["tours_evaluated"])
        # Re-running on the same snapshot is deterministic apart from the clock.
        for report in (first, second):
            report.pop("generated_at")
        self.assertEqual(first, second)

    def test_unknown_tour_is_rejected(self) -> None:
        with self.assertRaises(BacktestError):
            run_backtest(self.session_factory, tour_refs=["9999"])

    def test_tour_without_actuals_is_skipped_with_a_reason(self) -> None:
        # A future tour has no player statistics, so it cannot be scored.
        with self.engine.begin() as connection:
            season_id = connection.execute(
                text("SELECT id FROM seasons LIMIT 1")
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO fantasy_tours
                        (season_id, fantasy_tour_id, name, status, starts_at,
                         transfers_deadline_at)
                    VALUES (:season, '1774', '3 тур', 'SCHEDULED',
                            '2025-08-01T16:00:00Z', '2025-08-01T15:00:00Z')
                    """
                ),
                {"season": season_id},
            )

        report = run_backtest(self.session_factory, optimize=False)

        self.assertEqual(2, report["counts"]["tours_evaluated"])
        self.assertEqual(1, report["counts"]["tours_skipped"])
        self.assertEqual("3 тур", report["skipped_tours"][0]["name"])
        self.assertIn("no actual player stats", report["skipped_tours"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
