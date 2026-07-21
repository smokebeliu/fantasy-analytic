"""Unit and integration tests for the squad optimizer (development-plan step 8).

The unit tests exercise the pure integer program and its independent validator
in isolation: they build a fresh squad for a normal tour, a limited-transfers
squad for a tour with a changed transfer limit, prove the objective equals the
starting-eleven points plus the captain, that recomputing is deterministic and
that an infeasible problem raises a clear error. The integration tests import a
small synthetic season into a real PostgreSQL database, publish it through the
quality gate, add a future tour and then run the whole database-backed pipeline
(forecast -> rules -> solve -> validate). They are skipped automatically when no
database is reachable.
"""

from __future__ import annotations

import os
import unittest
from dataclasses import replace

from sqlalchemy import text

from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.forecast import MODEL_EVENT
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.optimizer import (
    Candidate,
    OptimizerError,
    SquadRules,
    build_squad_optimization,
    candidates_from_forecast,
    parse_role_limits,
    solve_squad,
    validate_squad,
)
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


# ---------------------------------------------------------------------------
# Shared synthetic pool for the pure unit tests.
# ---------------------------------------------------------------------------

_ROLE_COUNTS = {"GOALKEEPER": 4, "DEFENDER": 10, "MIDFIELDER": 10, "FORWARD": 6}
_ROLE_PRICE = {"GOALKEEPER": 5.0, "DEFENDER": 5.5, "MIDFIELDER": 6.0, "FORWARD": 6.5}


def _rpl_rules(**overrides) -> SquadRules:
    """The standard 2025/2026 RPL configuration, with optional overrides."""
    defaults = dict(
        total_budget=100.0,
        total_players=15,
        starting_players=11,
        full_limits={
            "GOALKEEPER": (2, 2),
            "DEFENDER": (5, 5),
            "MIDFIELDER": (5, 5),
            "FORWARD": (3, 3),
        },
        starting_limits={
            "GOALKEEPER": (1, 1),
            "DEFENDER": (3, 5),
            "MIDFIELDER": (2, 5),
            "FORWARD": (1, 3),
        },
        max_same_team=3,
        total_transfers=3,
    )
    defaults.update(overrides)
    return SquadRules(**defaults)


def _pool() -> list[Candidate]:
    """A deterministic, feasible candidate pool spread over eight clubs.

    Expected points increase with the index so the optimum is predictable, while
    prices stay low enough for a full squad to fit inside the budget.
    """
    candidates: list[Candidate] = []
    psid = 1
    for role, count in _ROLE_COUNTS.items():
        for k in range(count):
            candidates.append(
                Candidate(
                    player_season_id=psid,
                    fantasy_player_id=str(psid),
                    player_name=f"{role[:3]}-{k}",
                    role=role,
                    club_id=psid % 8,
                    club_name=f"Club{psid % 8}",
                    price=round(_ROLE_PRICE[role] + (k % 3), 2),
                    expected_points=round(1.0 + 0.5 * k, 4),
                )
            )
            psid += 1
    return candidates


def _starting_points(solution: dict) -> float:
    return round(sum(p["expected_points"] for p in solution["starting"]), 4)


class ParseRoleLimitsTest(unittest.TestCase):
    def test_parses_min_and_max(self) -> None:
        limits = parse_role_limits(
            [
                {"role": "GOALKEEPER", "minCount": 2, "maxCount": 2},
                {"role": "DEFENDER", "minCount": 3, "maxCount": 5},
            ]
        )
        self.assertEqual(limits["GOALKEEPER"], (2, 2))
        self.assertEqual(limits["DEFENDER"], (3, 5))

    def test_missing_bounds_are_permissive(self) -> None:
        limits = parse_role_limits([{"role": "FORWARD"}])
        self.assertEqual(limits["FORWARD"][0], 0)
        self.assertGreater(limits["FORWARD"][1], 100)

    def test_none_yields_empty(self) -> None:
        self.assertEqual(parse_role_limits(None), {})


class CandidatesFromForecastTest(unittest.TestCase):
    def _row(self, **over) -> dict:
        row = {
            "model_name": MODEL_EVENT,
            "player_season_id": 1,
            "fantasy_player_id": "1",
            "player_name": "A",
            "role": "MIDFIELDER",
            "club_id": 3,
            "club_name": "C",
            "price": 7.0,
            "expected_points": 4.0,
        }
        row.update(over)
        return row

    def test_filters_by_model(self) -> None:
        rows = [self._row(), self._row(model_name="season_mean", player_season_id=2)]
        cands = candidates_from_forecast(rows, MODEL_EVENT)
        self.assertEqual([c.player_season_id for c in cands], [1])

    def test_drops_rows_without_price_or_club(self) -> None:
        rows = [
            self._row(player_season_id=1),
            self._row(player_season_id=2, price=None),
            self._row(player_season_id=3, club_id=None),
        ]
        cands = candidates_from_forecast(rows, MODEL_EVENT)
        self.assertEqual([c.player_season_id for c in cands], [1])

    def test_dedupes_and_orders(self) -> None:
        rows = [
            self._row(player_season_id=5),
            self._row(player_season_id=2),
            self._row(player_season_id=5),
        ]
        cands = candidates_from_forecast(rows, MODEL_EVENT)
        self.assertEqual([c.player_season_id for c in cands], [2, 5])


class SolveSquadTest(unittest.TestCase):
    def test_normal_tour_builds_a_valid_squad(self) -> None:
        rules = _rpl_rules()
        solution = solve_squad(_pool(), rules)

        self.assertIn(solution["status"], ("OPTIMAL", "FEASIBLE"))
        self.assertEqual(len(solution["squad"]), 15)
        self.assertEqual(len(solution["starting"]), 11)
        self.assertEqual(len(solution["bench"]), 4)
        self.assertEqual(validate_squad(solution, rules), [])
        self.assertLessEqual(solution["total_price"], rules.total_budget)

    def test_full_and_starting_role_counts_respected(self) -> None:
        rules = _rpl_rules()
        solution = solve_squad(_pool(), rules)
        full = {r: 0 for r in ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")}
        start = {r: 0 for r in full}
        for player in solution["squad"]:
            full[player["role"]] += 1
            if player["is_starter"]:
                start[player["role"]] += 1
        self.assertEqual(full, {"GOALKEEPER": 2, "DEFENDER": 5, "MIDFIELDER": 5, "FORWARD": 3})
        self.assertEqual(start["GOALKEEPER"], 1)
        self.assertTrue(3 <= start["DEFENDER"] <= 5)
        self.assertTrue(2 <= start["MIDFIELDER"] <= 5)
        self.assertTrue(1 <= start["FORWARD"] <= 3)

    def test_objective_equals_starting_plus_captain(self) -> None:
        rules = _rpl_rules()
        solution = solve_squad(_pool(), rules)
        captain_points = solution["captain"]["expected_points"]
        expected = round(_starting_points(solution) + captain_points, 4)
        self.assertAlmostEqual(
            solution["objective_expected_points"], expected, places=4
        )

    def test_captain_is_highest_scoring_starter(self) -> None:
        solution = solve_squad(_pool(), _rpl_rules())
        best = max(p["expected_points"] for p in solution["starting"])
        self.assertEqual(solution["captain"]["expected_points"], best)
        self.assertNotEqual(
            solution["captain"]["player_season_id"],
            solution["vice_captain"]["player_season_id"],
        )

    def test_club_limit_respected(self) -> None:
        rules = _rpl_rules(max_same_team=2)
        solution = solve_squad(_pool(), rules)
        counts: dict[int, int] = {}
        for player in solution["squad"]:
            counts[player["club_id"]] = counts.get(player["club_id"], 0) + 1
        self.assertTrue(all(c <= 2 for c in counts.values()))
        self.assertEqual(validate_squad(solution, rules), [])

    def test_deterministic(self) -> None:
        rules = _rpl_rules()
        pool = _pool()
        self.assertEqual(solve_squad(pool, rules), solve_squad(pool, rules))

    def test_infeasible_budget_raises(self) -> None:
        rules = _rpl_rules(total_budget=10.0)
        with self.assertRaises(OptimizerError):
            solve_squad(_pool(), rules)

    def test_too_few_candidates_raises(self) -> None:
        rules = _rpl_rules()
        with self.assertRaises(OptimizerError):
            solve_squad(_pool()[:5], rules)

    def test_empty_pool_raises(self) -> None:
        with self.assertRaises(OptimizerError):
            solve_squad([], _rpl_rules())


class TransfersModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = _pool()
        self.rules = _rpl_rules()
        # Start from a valid but sub-optimal squad: the cheapest legal 15 by id.
        base = solve_squad(self.pool, _rpl_rules(total_budget=100.0))
        self.optimal_ids = [p["player_season_id"] for p in base["squad"]]

    def _worse_squad_ids(self) -> list[int]:
        # The worst *valid* squad: solve the same program with negated points so
        # every rule (roster, budget, club limit) still holds, but the lineup is
        # deliberately weak and therefore has room to improve via transfers.
        negated = [replace(c, expected_points=-c.expected_points) for c in self.pool]
        worst = solve_squad(negated, self.rules)
        return [p["player_season_id"] for p in worst["squad"]]

    def test_keeping_optimal_squad_needs_no_transfers(self) -> None:
        solution = solve_squad(
            self.pool, self.rules, current_ids=self.optimal_ids, max_transfers=3
        )
        self.assertEqual(solution["transfers"]["made"], 0)
        self.assertEqual(solution["transfers"]["kept"], 15)
        self.assertEqual(validate_squad(solution, self.rules), [])

    def test_transfer_limit_caps_changes(self) -> None:
        worse = self._worse_squad_ids()
        limited = solve_squad(
            self.pool, self.rules, current_ids=worse, max_transfers=1
        )
        self.assertLessEqual(limited["transfers"]["made"], 1)
        self.assertEqual(validate_squad(limited, self.rules), [])

    def test_more_transfers_never_worse(self) -> None:
        worse = self._worse_squad_ids()
        one = solve_squad(self.pool, self.rules, current_ids=worse, max_transfers=1)
        three = solve_squad(
            self.pool, self.rules, current_ids=worse, max_transfers=3
        )
        self.assertGreaterEqual(
            three["objective_expected_points"], one["objective_expected_points"]
        )
        self.assertLessEqual(one["transfers"]["made"], 1)
        self.assertLessEqual(three["transfers"]["made"], 3)

    def test_missing_current_player_is_reported(self) -> None:
        current = self.optimal_ids[:-1] + [99999]
        solution = solve_squad(
            self.pool, self.rules, current_ids=current, max_transfers=3
        )
        self.assertIn(99999, solution["transfers"]["missing_from_pool"])
        self.assertEqual(validate_squad(solution, self.rules), [])

    def test_no_transfer_limit_without_override_raises(self) -> None:
        rules = _rpl_rules(total_transfers=None)
        with self.assertRaises(OptimizerError):
            solve_squad(self.pool, rules, current_ids=self.optimal_ids)


class ValidatorTest(unittest.TestCase):
    def _valid_solution(self) -> dict:
        return solve_squad(_pool(), _rpl_rules())

    def test_detects_budget_violation(self) -> None:
        solution = self._valid_solution()
        rules = _rpl_rules(total_budget=1.0)
        violations = validate_squad(solution, rules)
        self.assertTrue(any("budget" in v for v in violations))

    def test_detects_wrong_squad_size(self) -> None:
        solution = self._valid_solution()
        solution["squad"] = solution["squad"][:-1]
        violations = validate_squad(solution, _rpl_rules())
        self.assertTrue(any("squad size" in v for v in violations))

    def test_detects_two_captains(self) -> None:
        solution = self._valid_solution()
        for player in solution["squad"]:
            if player["is_starter"]:
                player["is_captain"] = True
        violations = validate_squad(solution, _rpl_rules())
        self.assertTrue(any("captain" in v for v in violations))

    def test_detects_club_limit_violation(self) -> None:
        solution = self._valid_solution()
        for player in solution["squad"]:
            player["club_id"] = 1
        violations = validate_squad(solution, _rpl_rules())
        self.assertTrue(any("club" in v for v in violations))


@requires_database
class OptimizerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)
        self.run_id = self._import_and_publish()
        self._add_future_tour()
        self._shrink_rules_to_two_players()

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
                 transfers_deadline_at, total_transfers, max_same_team_players)
            VALUES (:season, '1773', '2 тур', 'SCHEDULED',
                    '2025-07-25T16:00:00Z', '2025-07-24T16:00:00Z', 1, 3)
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

    def _shrink_rules_to_two_players(self) -> None:
        # The fixture only carries two players (a forward and a goalkeeper), so
        # rewrite the roster rules to a valid two-player configuration that the
        # optimizer can satisfy end-to-end.
        self._exec(
            """
            UPDATE season_rules SET
                total_budget = 100,
                total_players = 2,
                starting_players = 2,
                full_roster_constraints = :full,
                starting_roster_constraints = :starting
            """,
            full=(
                '[{"role": "GOALKEEPER", "minCount": 1, "maxCount": 1},'
                ' {"role": "FORWARD", "minCount": 1, "maxCount": 1}]'
            ),
            starting=(
                '[{"role": "GOALKEEPER", "minCount": 1, "maxCount": 1},'
                ' {"role": "FORWARD", "minCount": 1, "maxCount": 1}]'
            ),
        )

    def test_end_to_end_squad(self) -> None:
        report = build_squad_optimization(self.session_factory, tour_ref="1773")

        self.assertEqual(report["mode"], "squad")
        self.assertTrue(report["valid"])
        self.assertEqual(report["tour"]["fantasy_tour_id"], "1773")
        solution = report["solution"]
        self.assertEqual(len(solution["squad"]), 2)
        self.assertEqual(len(solution["starting"]), 2)
        # Rules were loaded from the database, not hard-coded.
        self.assertEqual(report["rules"]["max_same_team"], 3)
        self.assertEqual(report["rules"]["total_transfers"], 1)
        roles = {p["role"] for p in solution["squad"]}
        self.assertEqual(roles, {"GOALKEEPER", "FORWARD"})
        self.assertEqual(validate_squad(solution, _rules_from_report(report)), [])

    def test_end_to_end_deterministic(self) -> None:
        a = build_squad_optimization(self.session_factory, tour_ref="1773")
        b = build_squad_optimization(self.session_factory, tour_ref="1773")
        a.pop("generated_at")
        b.pop("generated_at")
        self.assertEqual(a["solution"], b["solution"])

    def test_end_to_end_transfers_mode(self) -> None:
        report = build_squad_optimization(
            self.session_factory,
            tour_ref="1773",
            current_squad=["111", "222"],
        )
        self.assertEqual(report["mode"], "transfers")
        self.assertLessEqual(
            report["solution"]["transfers"]["made"],
            report["rules"]["total_transfers"],
        )

    def test_finished_season_without_tour_raises(self) -> None:
        self._exec("UPDATE fantasy_tours SET status = 'FINISHED'")
        with self.assertRaises(OptimizerError):
            build_squad_optimization(self.session_factory)


def _rules_from_report(report: dict) -> SquadRules:
    rules = report["rules"]
    return SquadRules(
        total_budget=rules["total_budget"],
        total_players=rules["total_players"],
        starting_players=rules["starting_players"],
        full_limits={r: tuple(v) for r, v in rules["full_limits"].items()},
        starting_limits={r: tuple(v) for r, v in rules["starting_limits"].items()},
        max_same_team=rules["max_same_team"],
        total_transfers=rules["total_transfers"],
    )


if __name__ == "__main__":
    unittest.main()
