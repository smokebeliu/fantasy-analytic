"""Unit and integration tests for the squad optimizer (development-plan step 8).

The unit tests exercise the pure integer program and its independent validator
in isolation: they build a fresh squad for a normal tour, a limited-transfers
squad for a tour with a changed transfer limit, prove the objective equals the
starting-eleven points plus the captain, that recomputing is deterministic and
that an infeasible problem raises a clear error. A dedicated set covers the
fixture-aware objective (step 16) on a constructed pair of clubs that meet each
other in the target tour. The integration tests import a
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
    attach_future_points,
    expected_auto_sub_points,
    order_bench,
    DEFAULT_FIXTURE_CONFLICT_WEIGHT,
    Candidate,
    FixtureExposure,
    OptimizerError,
    ROLES,
    SquadRules,
    build_squad_optimization,
    cancellation,
    candidates_from_forecast,
    fixture_conflicts,
    parse_formation,
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

    def test_carries_every_fixture_of_a_doubled_tour(self) -> None:
        row = self._row(
            params={
                "fixture": {"goal_upside": 3.0, "shutout_stake": 0.0},
                "fixtures": [
                    {"match_id": 10, "goal_upside": 1.5, "shutout_stake": 0.0},
                    {"match_id": 11, "goal_upside": 1.5, "shutout_stake": 0.0},
                ],
            }
        )
        candidate = candidates_from_forecast([row], MODEL_EVENT)[0]
        self.assertEqual(2, len(candidate.tour_fixtures))
        self.assertEqual(
            [10, 11], [f["match_id"] for f in candidate.tour_fixtures]
        )
        # The scalar exposure stays the tour total, so a reader that only knows
        # about one match sees the whole of it rather than half.
        self.assertEqual(3.0, candidate.goal_upside)


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

    def test_no_transfer_is_suggested_when_it_gains_nothing(self) -> None:
        # Ties are everywhere: two bench players of the same role and price are
        # interchangeable, so an arbitrary tie-break would answer "make three
        # transfers for +0.0 points" and burn an allowance the user cannot get
        # back. Keeping players has to win every tie.
        solution = solve_squad(
            self.pool, self.rules, current_ids=self.optimal_ids, max_transfers=3
        )
        self.assertEqual(0, solution["transfers"]["made"])
        self.assertEqual([], solution["transfers"]["pairs"])

    def test_a_gaining_transfer_still_beats_keeping_the_player(self) -> None:
        # The preference for keeping players must not be strong enough to refuse a
        # transfer that actually scores more.
        worse = self._worse_squad_ids()
        solution = solve_squad(
            self.pool, self.rules, current_ids=worse, max_transfers=3
        )
        self.assertGreater(solution["transfers"]["made"], 0)
        free = solve_squad(self.pool, self.rules)
        self.assertLessEqual(
            solution["objective_expected_points"],
            free["objective_expected_points"],
        )
        self.assertGreater(
            solution["objective_expected_points"],
            solve_squad(
                self.pool, self.rules, current_ids=worse, max_transfers=0
            )["objective_expected_points"],
        )

    def test_a_marginal_transfer_is_not_proposed(self) -> None:
        # The pool's expected points climb in steps of 0.5 per player, so the
        # optimal squad's weakest forward can be swapped for the next one up
        # for exactly half a point. On live data such a swap once spent the
        # last transfer of the week for +0.05 points: below the minimum gain
        # it is noise, not advice, and the plan must leave it alone.
        by_id = {c.player_season_id: c for c in self.pool}
        forwards = sorted(
            (c for c in self.pool if c.role == "FORWARD"),
            key=lambda c: c.expected_points,
        )
        current = [pid for pid in self.optimal_ids if by_id[pid].role != "FORWARD"]
        kept_forwards = sorted(
            (by_id[pid] for pid in self.optimal_ids if by_id[pid].role == "FORWARD"),
            key=lambda c: c.expected_points,
        )
        # Replace the best forward by one worth 0.3 points less: a swap back
        # gains 0.3, which is real but below the threshold.
        downgraded = replace(
            forwards[0],
            player_season_id=99001,
            fantasy_player_id="99001",
            expected_points=round(kept_forwards[-1].expected_points - 0.3, 4),
            price=kept_forwards[-1].price,
            club_id=kept_forwards[-1].club_id,
        )
        pool = [*self.pool, downgraded]
        current = current + [c.player_season_id for c in kept_forwards[:-1]] + [99001]

        strict = solve_squad(pool, self.rules, current_ids=current, max_transfers=3)
        self.assertEqual(0, strict["transfers"]["made"])
        self.assertEqual(0.5, strict["transfers"]["min_gain"])

        greedy = solve_squad(
            pool, self.rules, current_ids=current, max_transfers=3, min_transfer_gain=0
        )
        self.assertEqual(1, greedy["transfers"]["made"])
        self.assertAlmostEqual(
            0.3, greedy["transfers"]["pairs"][0]["delta_expected_points"], places=3
        )
        self.assertEqual(0, greedy["transfers"]["min_gain"])

        # A swap that clears the bar is still made under the default.
        big_drop = replace(downgraded, expected_points=round(downgraded.expected_points - 2.0, 4))
        pool = [*self.pool, big_drop]
        cleared = solve_squad(pool, self.rules, current_ids=current, max_transfers=3)
        self.assertEqual(1, cleared["transfers"]["made"])
        self.assertEqual(validate_squad(cleared, self.rules), [])

    def test_negative_minimum_gain_is_rejected(self) -> None:
        with self.assertRaises(OptimizerError):
            solve_squad(
                self.pool, self.rules, current_ids=self.optimal_ids, min_transfer_gain=-1
            )

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

    # -- naming both sides of every swap --------------------------------------

    def test_outgoing_players_are_described_not_just_numbered(self) -> None:
        # A manager cannot act on "sell #4213": the report has to name the player
        # being sold as fully as the one being bought.
        worse = self._worse_squad_ids()
        solution = solve_squad(
            self.pool, self.rules, current_ids=worse, max_transfers=3
        )
        transfers = solution["transfers"]
        self.assertEqual(len(transfers["out"]), transfers["made"])
        for leaving in transfers["out"]:
            self.assertIsNotNone(leaving["player_name"])
            self.assertIn(leaving["role"], ROLES)
            self.assertIsNotNone(leaving["price"])
        self.assertNotIn(
            None, [entry["player_season_id"] for entry in transfers["out"]]
        )

    def test_every_transfer_is_paired_with_its_replacement(self) -> None:
        worse = self._worse_squad_ids()
        solution = solve_squad(
            self.pool, self.rules, current_ids=worse, max_transfers=3
        )
        transfers = solution["transfers"]
        pairs = transfers["pairs"]
        self.assertEqual(len(pairs), transfers["made"])
        # Every player who leaves and every player who arrives appears exactly
        # once, so no transfer is left unexplained or double counted.
        self.assertEqual(
            sorted(pair["out"]["player_season_id"] for pair in pairs),
            sorted(entry["player_season_id"] for entry in transfers["out"]),
        )
        self.assertEqual(
            sorted(pair["in"]["player_season_id"] for pair in pairs),
            sorted(entry["player_season_id"] for entry in transfers["in"]),
        )

    def test_pairs_swap_like_for_like_and_quantify_the_change(self) -> None:
        worse = self._worse_squad_ids()
        solution = solve_squad(
            self.pool, self.rules, current_ids=worse, max_transfers=3
        )
        for pair in solution["transfers"]["pairs"]:
            # The roster's role limits are exact, so a transfer can only trade a
            # player for another of the same position.
            self.assertEqual(pair["out"]["role"], pair["in"]["role"])
            self.assertAlmostEqual(
                pair["delta_expected_points"],
                pair["in"]["expected_points"] - pair["out"]["expected_points"],
                places=4,
            )
            self.assertAlmostEqual(
                pair["delta_price"],
                pair["in"]["price"] - pair["out"]["price"],
                places=2,
            )

    def test_forced_out_player_is_still_named_in_a_pair(self) -> None:
        # A player who vanished from the pool cannot be described, but he must
        # still show up as a transfer rather than disappearing from the plan.
        current = self.optimal_ids[:-1] + [99999]
        solution = solve_squad(
            self.pool, self.rules, current_ids=current, max_transfers=3
        )
        outgoing = {
            entry["player_season_id"]: entry
            for entry in solution["transfers"]["out"]
        }
        self.assertIn(99999, outgoing)
        self.assertTrue(outgoing[99999]["unavailable"])
        self.assertIn(
            99999,
            [pair["out"]["player_season_id"] for pair in solution["transfers"]["pairs"]],
        )


class SolveBudgetTest(unittest.TestCase):
    """The search is bounded, reproducible and honest about optimality."""

    def setUp(self) -> None:
        self.pool = _pool()
        self.rules = _rpl_rules()

    def test_an_easy_squad_is_proven_optimal(self) -> None:
        solution = solve_squad(self.pool, self.rules)
        self.assertTrue(solution["proven_optimal"])
        self.assertEqual(solution["status"], "OPTIMAL")

    def test_a_squad_found_without_a_proof_is_still_valid(self) -> None:
        # A budget too small to prove optimality still has to produce a squad
        # that obeys every roster rule: a bounded search degrades the answer's
        # quality, never its validity.
        solution = solve_squad(self.pool, self.rules, solve_limit=0.02)
        self.assertEqual(validate_squad(solution, self.rules), [])
        self.assertEqual(len(solution["squad"]), self.rules.total_players)

    def test_running_out_of_budget_is_not_reported_as_impossible(self) -> None:
        # Telling the user their constraints cannot be satisfied would send them
        # off changing pins and formations when the search merely gave up.
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(self.pool, self.rules, solve_limit=1e-9)
        self.assertIn("ran out of its budget", str(ctx.exception))

    def test_an_impossible_squad_is_reported_as_impossible(self) -> None:
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(self.pool, _rpl_rules(total_budget=1.0))
        self.assertIn("No valid squad satisfies", str(ctx.exception))

    def test_spending_less_only_breaks_a_genuine_tie(self) -> None:
        # The cheapest of several equally projected squads is chosen, but never at
        # the cost of a single point.
        free = solve_squad(self.pool, self.rules)
        richer = solve_squad(self.pool, _rpl_rules(total_budget=200.0))
        self.assertGreaterEqual(
            richer["objective_expected_points"],
            free["objective_expected_points"],
        )
        self.assertLessEqual(free["total_price"], self.rules.total_budget)

    def test_repeating_a_solve_returns_the_same_squad(self) -> None:
        # The pool is full of exact ties, so a search that raced its workers
        # would answer differently each run.
        first = solve_squad(self.pool, self.rules, formation="4-4-2")
        second = solve_squad(self.pool, self.rules, formation="4-4-2")
        self.assertEqual(first, second)


class ParseFormationTest(unittest.TestCase):
    def test_derives_goalkeepers_from_the_starting_size(self) -> None:
        counts = parse_formation("4-4-2", _rpl_rules())
        self.assertEqual(
            counts,
            {"DEFENDER": 4, "MIDFIELDER": 4, "FORWARD": 2, "GOALKEEPER": 1},
        )

    def test_rejects_malformed_input(self) -> None:
        for bad in ("442", "4-4", "4-4-2-1", "a-b-c", ""):
            with self.assertRaises(OptimizerError):
                parse_formation(bad, _rpl_rules())

    def test_rejects_formation_outside_starting_limits(self) -> None:
        # Only 1..3 forwards may start, so 3-2-5 is not playable.
        with self.assertRaises(OptimizerError) as ctx:
            parse_formation("3-2-5", _rpl_rules())
        self.assertIn("FWD", str(ctx.exception))

    def test_rejects_formation_leaving_no_goalkeeper(self) -> None:
        with self.assertRaises(OptimizerError) as ctx:
            parse_formation("4-4-3", _rpl_rules())
        self.assertIn("GK", str(ctx.exception))

    def test_rejects_more_outfielders_than_starters(self) -> None:
        with self.assertRaises(OptimizerError) as ctx:
            parse_formation("5-5-5", _rpl_rules())
        self.assertIn("11", str(ctx.exception))


class LockedPlayersTest(unittest.TestCase):
    """Step 15: user-pinned players and a user-chosen formation."""

    def setUp(self) -> None:
        self.pool = _pool()
        self.rules = _rpl_rules()
        self.free = solve_squad(self.pool, self.rules)
        self.free_ids = {p["player_season_id"] for p in self.free["squad"]}

    def _unwanted(self, count: int, role: str | None = None) -> list[int]:
        """Players the unconstrained optimum leaves out (so pinning bites)."""
        return [
            c.player_season_id
            for c in self.pool
            if c.player_season_id not in self.free_ids
            and (role is None or c.role == role)
        ][:count]

    def test_locked_players_are_always_selected(self) -> None:
        locked = self._unwanted(3)
        solution = solve_squad(self.pool, self.rules, locked_ids=locked)
        picked = {p["player_season_id"] for p in solution["squad"]}
        self.assertTrue(set(locked) <= picked)
        self.assertEqual(
            validate_squad(solution, self.rules, locked_ids=locked), []
        )
        self.assertEqual(solution["constraints"]["locked"], sorted(locked))

    def test_locked_players_are_flagged_in_the_report(self) -> None:
        locked = self._unwanted(2)
        solution = solve_squad(self.pool, self.rules, locked_ids=locked)
        flagged = {
            p["player_season_id"] for p in solution["squad"] if p["is_locked"]
        }
        self.assertEqual(flagged, set(locked))

    def test_remaining_slots_are_filled_optimally(self) -> None:
        # Fixing one player and re-optimizing must give exactly the best squad
        # that contains them, which is what an exhaustive search over the pool
        # with that player forced would return. Pinning a player the free
        # optimum already picked must therefore reproduce the free optimum.
        already_optimal = sorted(self.free_ids)[:2]
        solution = solve_squad(self.pool, self.rules, locked_ids=already_optimal)
        self.assertEqual(
            solution["objective_expected_points"],
            self.free["objective_expected_points"],
        )
        self.assertEqual(
            {p["player_season_id"] for p in solution["squad"]}, self.free_ids
        )

    def test_locking_never_beats_the_unconstrained_optimum(self) -> None:
        locked = self._unwanted(3)
        solution = solve_squad(self.pool, self.rules, locked_ids=locked)
        self.assertLessEqual(
            solution["objective_expected_points"],
            self.free["objective_expected_points"],
        )

    def test_locked_starter_is_in_the_starting_eleven(self) -> None:
        benched = self.free["bench"][0]["player_season_id"]
        solution = solve_squad(
            self.pool, self.rules, locked_starter_ids=[benched]
        )
        starters = {p["player_season_id"] for p in solution["starting"]}
        self.assertIn(benched, starters)
        self.assertEqual(
            validate_squad(
                solution, self.rules, locked_starter_ids=[benched]
            ),
            [],
        )

    def test_locked_starter_implies_locked_in_squad(self) -> None:
        target = self._unwanted(1)[0]
        solution = solve_squad(
            self.pool, self.rules, locked_starter_ids=[target]
        )
        self.assertIn(target, solution["constraints"]["locked"])

    def test_formation_shapes_the_starting_eleven(self) -> None:
        solution = solve_squad(self.pool, self.rules, formation="3-4-3")
        self.assertEqual(solution["formation"], "3-4-3")
        self.assertEqual(
            validate_squad(solution, self.rules, formation="3-4-3"), []
        )

    def test_formation_and_locks_combine(self) -> None:
        locked = self._unwanted(2, role="DEFENDER")
        solution = solve_squad(
            self.pool, self.rules, locked_ids=locked, formation="5-3-2"
        )
        picked = {p["player_season_id"] for p in solution["squad"]}
        self.assertTrue(set(locked) <= picked)
        self.assertEqual(solution["formation"], "5-3-2")
        self.assertEqual(
            validate_squad(
                solution, self.rules, locked_ids=locked, formation="5-3-2"
            ),
            [],
        )

    def test_locks_apply_in_transfers_mode(self) -> None:
        current = sorted(self.free_ids)
        locked = self._unwanted(1)
        solution = solve_squad(
            self.pool,
            self.rules,
            current_ids=current,
            max_transfers=3,
            locked_ids=locked,
        )
        picked = {p["player_season_id"] for p in solution["squad"]}
        self.assertTrue(set(locked) <= picked)
        self.assertLessEqual(solution["transfers"]["made"], 3)
        self.assertEqual(
            validate_squad(solution, self.rules, locked_ids=locked), []
        )

    def test_deterministic_with_locks(self) -> None:
        locked = self._unwanted(3)
        first = solve_squad(
            self.pool, self.rules, locked_ids=locked, formation="4-4-2"
        )
        second = solve_squad(
            self.pool, self.rules, locked_ids=locked, formation="4-4-2"
        )
        self.assertEqual(first, second)

    # -- incompatible pin sets ------------------------------------------------

    def test_unknown_locked_player_raises(self) -> None:
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(self.pool, self.rules, locked_ids=[999999])
        self.assertIn("999999", str(ctx.exception))

    def test_too_many_locked_players_raises(self) -> None:
        everyone = [c.player_season_id for c in self.pool]
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(self.pool, self.rules, locked_ids=everyone)
        self.assertIn("15", str(ctx.exception))

    def test_locked_role_over_limit_raises(self) -> None:
        keepers = [c.player_season_id for c in self.pool if c.role == "GOALKEEPER"]
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(self.pool, self.rules, locked_ids=keepers)
        self.assertIn("GK", str(ctx.exception))

    def test_locked_club_over_limit_raises(self) -> None:
        club = self.pool[0].club_id
        same_club = [c.player_season_id for c in self.pool if c.club_id == club][:4]
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(self.pool, self.rules, locked_ids=same_club)
        self.assertIn("club", str(ctx.exception))

    def test_locked_players_over_budget_raises(self) -> None:
        rules = _rpl_rules(total_budget=10.0)
        # One keeper, one defender and one midfielder: legal on every other
        # count, but together already more expensive than the whole budget.
        locked = [
            next(c.player_season_id for c in self.pool if c.role == role)
            for role in ("GOALKEEPER", "DEFENDER", "MIDFIELDER")
        ]
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(self.pool, rules, locked_ids=locked)
        self.assertIn("budget", str(ctx.exception))

    def test_locked_starters_conflicting_with_formation_raise(self) -> None:
        forwards = [c.player_season_id for c in self.pool if c.role == "FORWARD"][:3]
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(
                self.pool,
                self.rules,
                locked_starter_ids=forwards,
                formation="5-4-1",
            )
        self.assertIn("formation", str(ctx.exception))

    def test_infeasible_lock_combination_names_the_locks(self) -> None:
        # Each pin is legal on its own, but together they eat the whole budget
        # and leave nothing for the remaining, mandatory squad slots.
        expensive = _pool()
        expensive = [
            replace(c, price=30.0) if c.player_season_id <= 3 else c
            for c in expensive
        ]
        locked = [1, 2, 3]
        with self.assertRaises(OptimizerError) as ctx:
            solve_squad(expensive, self.rules, locked_ids=locked)
        self.assertIn("locked", str(ctx.exception))

    def test_validator_catches_a_dropped_lock(self) -> None:
        solution = solve_squad(self.pool, self.rules)
        violations = validate_squad(solution, self.rules, locked_ids=[999999])
        self.assertTrue(any("locked player 999999" in v for v in violations))

    def test_validator_catches_a_wrong_formation(self) -> None:
        solution = solve_squad(self.pool, self.rules, formation="4-4-2")
        violations = validate_squad(solution, self.rules, formation="3-4-3")
        self.assertTrue(any("formation 3-4-3" in v for v in violations))


class CancellationTest(unittest.TestCase):
    """Step 16: the pure measure of two players cancelling each other out."""

    def _exposure(self, **over) -> FixtureExposure:
        base = dict(match_id=1, club_id=1, goal_upside=2.0, shutout_stake=1.5)
        base.update(over)
        return FixtureExposure(**base)

    def test_opposing_pair_multiplies_upside_by_stake(self) -> None:
        home = self._exposure(club_id=1, goal_upside=1.0, shutout_stake=2.0)
        away = self._exposure(club_id=2, goal_upside=3.0, shutout_stake=0.5)
        # 1.0 * 0.5 (their keeper's stake) + 3.0 * 2.0 (our defence's stake).
        self.assertAlmostEqual(cancellation(home, away), 6.5, places=6)

    def test_symmetric(self) -> None:
        home = self._exposure(club_id=1)
        away = self._exposure(club_id=2, goal_upside=1.0, shutout_stake=3.0)
        self.assertEqual(cancellation(home, away), cancellation(away, home))

    def test_teammates_do_not_cancel(self) -> None:
        self.assertEqual(
            cancellation(self._exposure(club_id=7), self._exposure(club_id=7)), 0.0
        )

    def test_different_fixtures_do_not_cancel(self) -> None:
        self.assertEqual(
            cancellation(
                self._exposure(match_id=1, club_id=1),
                self._exposure(match_id=2, club_id=2),
            ),
            0.0,
        )

    def test_player_without_a_fixture_does_not_cancel(self) -> None:
        self.assertEqual(
            cancellation(
                self._exposure(match_id=None), self._exposure(match_id=None, club_id=2)
            ),
            0.0,
        )

    def test_pure_attackers_do_not_cancel(self) -> None:
        # Neither side has anything staked on a shutout, so nothing cancels.
        self.assertEqual(
            cancellation(
                self._exposure(club_id=1, shutout_stake=0.0),
                self._exposure(club_id=2, shutout_stake=0.0),
            ),
            0.0,
        )

    def test_conflicts_are_ordered_and_reported_once(self) -> None:
        exposures = [
            [FixtureExposure(match_id=1, club_id=1, goal_upside=0.0, shutout_stake=2.0)],
            [FixtureExposure(match_id=1, club_id=2, goal_upside=3.0, shutout_stake=0.0)],
            [FixtureExposure(match_id=1, club_id=2, goal_upside=1.0, shutout_stake=0.0)],
            [FixtureExposure(match_id=2, club_id=3, goal_upside=1.0, shutout_stake=1.0)],
        ]
        conflicts = fixture_conflicts(exposures)
        self.assertEqual([(left, right) for left, right, _ in conflicts], [(0, 1), (0, 2)])
        self.assertAlmostEqual(conflicts[0][2], 6.0, places=6)
        self.assertAlmostEqual(conflicts[1][2], 2.0, places=6)

    def test_two_players_meeting_twice_cancel_in_both_matches(self) -> None:
        # A postponement can double a club up, so the same pair of players can
        # face each other twice inside one tour. The pair is still reported once
        # and the cancellation is the sum over the matches they meet in.
        defender = [
            FixtureExposure(match_id=1, club_id=1, goal_upside=0.0, shutout_stake=2.0),
            FixtureExposure(match_id=2, club_id=1, goal_upside=0.0, shutout_stake=1.0),
        ]
        striker = [
            FixtureExposure(match_id=1, club_id=2, goal_upside=3.0, shutout_stake=0.0),
            FixtureExposure(match_id=2, club_id=2, goal_upside=3.0, shutout_stake=0.0),
        ]
        conflicts = fixture_conflicts([defender, striker])
        self.assertEqual(1, len(conflicts))
        self.assertEqual((0, 1), conflicts[0][:2])
        self.assertAlmostEqual(conflicts[0][2], 6.0 + 3.0, places=6)

    def test_a_player_never_clashes_with_himself(self) -> None:
        both_sides = [
            FixtureExposure(match_id=1, club_id=1, goal_upside=3.0, shutout_stake=2.0),
            FixtureExposure(match_id=1, club_id=1, goal_upside=3.0, shutout_stake=2.0),
        ]
        self.assertEqual([], fixture_conflicts([both_sides]))


# The two fixtures of a synthetic tour: club 1 hosts club 2, club 3 hosts club 4.
_DUEL_MATCH = 501
_OTHER_MATCH = 502


def _duel_rules(**overrides) -> SquadRules:
    """A two-man squad that must field exactly one defender and one forward."""
    defaults = dict(
        total_budget=100.0,
        total_players=2,
        starting_players=2,
        full_limits={"DEFENDER": (1, 1), "FORWARD": (1, 1)},
        starting_limits={"DEFENDER": (1, 1), "FORWARD": (1, 1)},
        max_same_team=2,
        total_transfers=1,
    )
    defaults.update(overrides)
    return SquadRules(**defaults)


def _duel_pool(rival_points: float) -> list[Candidate]:
    """A defender and the two forwards competing for the other starting slot.

    Forward ``2`` plays *against* the defender in ``_DUEL_MATCH``, so their
    forecasts cancel out; forward ``3`` has an unrelated fixture and is worth
    ``5.0``. ``rival_points`` decides whether the clashing forward is worth the
    cancellation.
    """
    return [
        Candidate(
            player_season_id=1,
            fantasy_player_id="1",
            player_name="Defender",
            role="DEFENDER",
            club_id=1,
            club_name="Club1",
            price=5.0,
            expected_points=5.0,
            match_id=_DUEL_MATCH,
            opponent_club_id=2,
            goal_upside=0.0,
            shutout_stake=1.5,
        ),
        Candidate(
            player_season_id=2,
            fantasy_player_id="2",
            player_name="Rival striker",
            role="FORWARD",
            club_id=2,
            club_name="Club2",
            price=5.0,
            expected_points=rival_points,
            match_id=_DUEL_MATCH,
            opponent_club_id=1,
            goal_upside=2.0,
            shutout_stake=0.0,
        ),
        Candidate(
            player_season_id=3,
            fantasy_player_id="3",
            player_name="Neutral striker",
            role="FORWARD",
            club_id=3,
            club_name="Club3",
            price=5.0,
            expected_points=5.0,
            match_id=_OTHER_MATCH,
            opponent_club_id=4,
            goal_upside=2.0,
            shutout_stake=0.0,
        ),
    ]


class FixtureAwareOptimizerTest(unittest.TestCase):
    """Step 16: the tour schedule is part of the objective."""

    def setUp(self) -> None:
        self.rules = _duel_rules()
        # goal_upside(2.0) * shutout_stake(1.5) of the opposing pair.
        self.expected_cancellation = 3.0
        self.expected_penalty = round(
            DEFAULT_FIXTURE_CONFLICT_WEIGHT * self.expected_cancellation, 4
        )

    def _ids(self, solution: dict) -> set[int]:
        return {player["player_season_id"] for player in solution["squad"]}

    def test_marginally_better_opponent_is_not_taken(self) -> None:
        # The clashing striker is worth 0.1 more, far less than the 0.75 the
        # cancellation costs, so the optimizer prefers the neutral fixture.
        solution = solve_squad(_duel_pool(5.1), self.rules)
        self.assertEqual(self._ids(solution), {1, 3})
        self.assertEqual(solution["fixtures"]["clashes"], [])
        self.assertEqual(solution["fixture_penalty"], 0.0)
        self.assertEqual(
            solution["objective_score"], solution["objective_expected_points"]
        )
        self.assertEqual(validate_squad(solution, self.rules), [])

    def test_clearly_better_opponent_is_still_taken(self) -> None:
        # Worth 2 points more than the alternative: the clash now pays for
        # itself, so the schedule must not veto it.
        solution = solve_squad(_duel_pool(7.0), self.rules)
        self.assertEqual(self._ids(solution), {1, 2})
        self.assertEqual(solution["fixture_penalty"], self.expected_penalty)
        self.assertEqual(
            solution["objective_score"],
            round(solution["objective_expected_points"] - self.expected_penalty, 4),
        )
        self.assertEqual(validate_squad(solution, self.rules), [])

    def test_clash_is_explained_in_the_report(self) -> None:
        solution = solve_squad(_duel_pool(7.0), self.rules)
        clash = solution["fixtures"]["clashes"][0]
        self.assertEqual(clash["match_id"], _DUEL_MATCH)
        self.assertEqual(
            {clash["player_season_id"], clash["opponent_player_season_id"]}, {1, 2}
        )
        self.assertEqual(clash["cancellation"], self.expected_cancellation)
        self.assertEqual(clash["penalty"], self.expected_penalty)
        head_to_head = solution["fixtures"]["head_to_head"]
        self.assertEqual(len(head_to_head), 1)
        self.assertEqual(head_to_head[0]["match_id"], _DUEL_MATCH)
        self.assertEqual(
            [club["starters"] for club in head_to_head[0]["clubs"]], [1, 1]
        )

    def test_zero_weight_ignores_the_schedule_but_still_reports_it(self) -> None:
        solution = solve_squad(
            _duel_pool(5.1), self.rules, fixture_conflict_weight=0.0
        )
        self.assertEqual(self._ids(solution), {1, 2})
        self.assertEqual(
            solution["fixtures"]["cancellation"], self.expected_cancellation
        )
        self.assertEqual(solution["fixtures"]["clashes"][0]["penalty"], 0.0)
        self.assertEqual(solution["fixture_penalty"], 0.0)
        self.assertEqual(validate_squad(solution, self.rules), [])

    def test_heavier_weight_forces_the_neutral_fixture(self) -> None:
        clashing = solve_squad(
            _duel_pool(7.0), self.rules, fixture_conflict_weight=0.0
        )
        # The clash is worth 4 points of objective (the striker also captains),
        # so it only loses once the cancellation is priced above 4/3 per point².
        avoided = solve_squad(
            _duel_pool(7.0), self.rules, fixture_conflict_weight=2.0
        )
        self.assertEqual(self._ids(clashing), {1, 2})
        self.assertEqual(self._ids(avoided), {1, 3})
        self.assertLess(
            avoided["objective_expected_points"],
            clashing["objective_expected_points"],
        )

    def test_negative_weight_raises(self) -> None:
        with self.assertRaises(OptimizerError):
            solve_squad(_duel_pool(7.0), self.rules, fixture_conflict_weight=-0.5)

    def test_deterministic_with_the_schedule_priced(self) -> None:
        pool = _duel_pool(7.0)
        self.assertEqual(solve_squad(pool, self.rules), solve_squad(pool, self.rules))

    def test_pool_without_fixtures_is_unaffected(self) -> None:
        solution = solve_squad(_pool(), _rpl_rules())
        self.assertEqual(solution["fixtures"]["clashes"], [])
        self.assertEqual(solution["fixtures"]["head_to_head"], [])
        self.assertEqual(solution["fixture_penalty"], 0.0)

    def test_locked_starters_may_force_a_priced_clash(self) -> None:
        # The user gets what they asked for; the cost is reported, not hidden.
        solution = solve_squad(
            _duel_pool(5.1), self.rules, locked_starter_ids=[1, 2]
        )
        self.assertEqual(self._ids(solution), {1, 2})
        self.assertEqual(solution["fixture_penalty"], self.expected_penalty)
        self.assertEqual(
            validate_squad(solution, self.rules, locked_starter_ids=[1, 2]), []
        )

    def test_validator_catches_an_unpriced_clash(self) -> None:
        solution = solve_squad(_duel_pool(7.0), self.rules)
        solution["fixture_penalty"] = 0.0
        violations = validate_squad(solution, self.rules)
        self.assertTrue(any("fixture penalty" in v for v in violations))

    def test_validator_catches_a_misreported_cancellation(self) -> None:
        solution = solve_squad(_duel_pool(7.0), self.rules)
        solution["fixtures"]["cancellation"] = 0.0
        violations = validate_squad(solution, self.rules)
        self.assertTrue(any("fixture cancellation" in v for v in violations))

    def test_validator_prices_the_cancellation_from_the_unrounded_sum(self) -> None:
        # round(w * round(x, 4), 4) can land a whole least-significant digit
        # away from round(w * x, 4). The validator used to do the former and the
        # report the latter, so a perfectly legal squad was rejected as invalid
        # and took a whole backtest run down with it.
        starters = [
            {
                "player_season_id": index,
                "role": role,
                "club_id": club,
                "price": 5.0,
                "expected_points": 4.0,
                "is_starter": True,
                "match_id": 77,
                "goal_upside": upside,
                "shutout_stake": stake,
            }
            for index, (role, club, upside, stake) in enumerate(
                (
                    ("FORWARD", 1, 2.385049, 0.0),
                    ("DEFENDER", 2, 0.0, 1.0),
                ),
                start=1,
            )
        ]
        weight = 0.25
        solution = {
            "squad": starters,
            "fixture_penalty": round(weight * 2.385049, 4),
            "objective_expected_points": 8.0,
            "objective_score": round(8.0 - round(weight * 2.385049, 4), 4),
            "fixtures": {"conflict_weight": weight, "cancellation": 2.385049},
        }
        violations = validate_squad(solution, self.rules)
        self.assertFalse([v for v in violations if "fixture" in v])

    def test_validator_catches_a_wrong_objective_score(self) -> None:
        solution = solve_squad(_duel_pool(7.0), self.rules)
        solution["objective_score"] = solution["objective_expected_points"]
        violations = validate_squad(solution, self.rules)
        self.assertTrue(any("objective score" in v for v in violations))

    def test_candidates_read_the_exposures_from_the_forecast(self) -> None:
        rows = [
            {
                "model_name": MODEL_EVENT,
                "player_season_id": 1,
                "role": "DEFENDER",
                "club_id": 1,
                "price": 5.0,
                "expected_points": 4.0,
                "match_id": _DUEL_MATCH,
                "opponent_club_id": 2,
                "params": {
                    "fixture": {"goal_upside": 0.4, "shutout_stake": 1.2},
                },
            },
            {
                "model_name": MODEL_EVENT,
                "player_season_id": 2,
                "role": "FORWARD",
                "club_id": 2,
                "price": 5.0,
                "expected_points": 4.0,
                "match_id": _DUEL_MATCH,
                "opponent_club_id": 1,
                "params": None,
            },
        ]
        candidates = candidates_from_forecast(rows, MODEL_EVENT)
        self.assertEqual(candidates[0].goal_upside, 0.4)
        self.assertEqual(candidates[0].shutout_stake, 1.2)
        self.assertEqual(candidates[0].opponent_club_id, 2)
        # A model without a fixture breakdown simply has no exposure.
        self.assertEqual(candidates[1].goal_upside, 0.0)
        self.assertEqual(candidates[1].shutout_stake, 0.0)


class DoubleGameweekSquadTest(unittest.TestCase):
    """A club doubled up by a postponement is worth owning twice over."""

    def _pool(self) -> list[Candidate]:
        # Two forwards of equal price. The neutral one plays once for 5 points;
        # the doubled one plays twice for 4 each, which is the better buy.
        return [
            Candidate(
                player_season_id=1,
                fantasy_player_id="1",
                player_name="Defender",
                role="DEFENDER",
                club_id=1,
                club_name="Club1",
                price=5.0,
                expected_points=4.0,
                match_id=_DUEL_MATCH,
                opponent_club_id=2,
                goal_upside=0.0,
                shutout_stake=1.5,
                tour_fixtures=(
                    {"match_id": _DUEL_MATCH, "goal_upside": 0.0, "shutout_stake": 0.75},
                    {"match_id": _OTHER_MATCH, "goal_upside": 0.0, "shutout_stake": 0.75},
                ),
            ),
            Candidate(
                player_season_id=2,
                fantasy_player_id="2",
                player_name="Doubled striker",
                role="FORWARD",
                club_id=2,
                club_name="Club2",
                price=5.0,
                expected_points=8.0,
                match_id=_DUEL_MATCH,
                opponent_club_id=1,
                goal_upside=4.0,
                shutout_stake=0.0,
                tour_fixtures=(
                    {"match_id": _DUEL_MATCH, "goal_upside": 2.0, "shutout_stake": 0.0},
                    {"match_id": _OTHER_MATCH, "goal_upside": 2.0, "shutout_stake": 0.0},
                ),
            ),
            Candidate(
                player_season_id=3,
                fantasy_player_id="3",
                player_name="Single striker",
                role="FORWARD",
                club_id=3,
                club_name="Club3",
                price=5.0,
                expected_points=5.0,
                match_id=503,
                opponent_club_id=4,
                goal_upside=2.0,
                shutout_stake=0.0,
                tour_fixtures=(
                    {"match_id": 503, "goal_upside": 2.0, "shutout_stake": 0.0},
                ),
            ),
        ]

    def test_the_doubled_player_wins_the_slot(self) -> None:
        solution = solve_squad(self._pool(), _duel_rules(), fixture_conflict_weight=0.0)
        self.assertEqual(
            {2}, {p["player_season_id"] for p in solution["squad"] if p["role"] == "FORWARD"}
        )

    def test_meeting_the_same_opponent_twice_is_priced_twice(self) -> None:
        # The defender and the striker face each other in both matches of the
        # tour, so the pair cancels out twice over.
        solution = solve_squad(self._pool(), _duel_rules())
        clashes = solution["fixtures"]["clashes"]
        self.assertEqual(1, len(clashes))
        self.assertAlmostEqual(2 * 2.0 * 0.75, clashes[0]["cancellation"], places=4)
        self.assertEqual([], validate_squad(solution, _duel_rules()))

    def test_the_squad_entry_carries_every_match(self) -> None:
        solution = solve_squad(self._pool(), _duel_rules())
        striker = next(p for p in solution["squad"] if p["player_season_id"] == 2)
        self.assertEqual(2, striker["fixture_count"])
        self.assertEqual(
            [_DUEL_MATCH, _OTHER_MATCH],
            [f["match_id"] for f in striker["tour_fixtures"]],
        )


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

    def test_a_red_card_zeroes_expected_points_for_the_next_tour(self) -> None:
        baseline = build_squad_optimization(self.session_factory, tour_ref="1773")
        forward = next(
            player
            for player in baseline["solution"]["squad"]
            if player["fantasy_player_id"] == "111"
        )
        self.assertGreater(forward["expected_points"], 0.0)

        self._exec(
            "UPDATE player_match_stats SET red_cards = 1 "
            "WHERE player_season_id = ("
            "  SELECT id FROM player_seasons WHERE fantasy_player_id = '111')"
        )
        report = build_squad_optimization(self.session_factory, tour_ref="1773")
        banned = next(
            player
            for player in report["solution"]["squad"]
            if player["fantasy_player_id"] == "111"
        )
        self.assertEqual(0.0, banned["expected_points"])
        self.assertEqual(0.0, banned["p_appearance"])

    def test_end_to_end_reports_the_head_to_head_fixture(self) -> None:
        # The fixture's only two players are a forward of club A and the
        # goalkeeper of club B, and club A hosts club B in the target tour, so
        # the forced two-man squad is itself a head-to-head pair.
        report = build_squad_optimization(self.session_factory, tour_ref="1773")

        fixtures = report["solution"]["fixtures"]
        self.assertEqual(len(fixtures["head_to_head"]), 1)
        self.assertEqual(
            [club["starters"] for club in fixtures["head_to_head"][0]["clubs"]], [1, 1]
        )
        self.assertEqual(report["counts"]["head_to_head_fixtures"], 1)
        self.assertEqual(
            fixtures["conflict_weight"], DEFAULT_FIXTURE_CONFLICT_WEIGHT
        )
        self.assertGreaterEqual(report["solution"]["fixture_penalty"], 0.0)
        self.assertEqual(
            report["solution"]["objective_score"],
            round(
                report["solution"]["objective_expected_points"]
                - report["solution"]["fixture_penalty"],
                4,
            ),
        )
        self.assertTrue(report["valid"])

    def test_end_to_end_zero_weight_keeps_the_expected_points(self) -> None:
        priced = build_squad_optimization(self.session_factory, tour_ref="1773")
        blind = build_squad_optimization(
            self.session_factory, tour_ref="1773", fixture_conflict_weight=0.0
        )
        self.assertEqual(blind["solution"]["fixture_penalty"], 0.0)
        self.assertGreaterEqual(
            blind["solution"]["objective_expected_points"],
            priced["solution"]["objective_expected_points"],
        )

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

    def test_end_to_end_locked_player(self) -> None:
        report = build_squad_optimization(
            self.session_factory, tour_ref="1773", locked=["111"]
        )
        squad = report["solution"]["squad"]
        locked_entry = next(p for p in squad if p["fantasy_player_id"] == "111")
        self.assertTrue(locked_entry["is_locked"])
        self.assertEqual(report["counts"]["locked"], 1)
        self.assertEqual(
            report["solution"]["constraints"]["locked"],
            [locked_entry["player_season_id"]],
        )
        self.assertTrue(report["valid"])

    def test_end_to_end_locked_formation(self) -> None:
        # Two starters: one goalkeeper and one forward, i.e. formation 0-0-1.
        report = build_squad_optimization(
            self.session_factory, tour_ref="1773", formation="0-0-1"
        )
        self.assertEqual(report["solution"]["formation"], "0-0-1")
        self.assertTrue(report["valid"])

    def test_end_to_end_unknown_lock_raises(self) -> None:
        with self.assertRaises(OptimizerError) as ctx:
            build_squad_optimization(
                self.session_factory, tour_ref="1773", locked=["does-not-exist"]
            )
        self.assertIn("does-not-exist", str(ctx.exception))

    def test_end_to_end_locked_blank_fixture_player(self) -> None:
        # Move the only tour fixture off club A so its forward has a price and a
        # club but no match — the same blank-week case that used to reject a pin
        # with "not selectable". Locking that forward must still keep him and
        # fill the remaining slot from clubs that do play.
        season_id = self._scalar("SELECT id FROM seasons LIMIT 1")
        club_b = self._scalar(
            "SELECT club_id FROM season_clubs WHERE fantasy_team_id = '20'"
        )
        club_c = self._exec(
            """
            INSERT INTO clubs (stat_team_id, canonical_name)
            VALUES ('club_c', 'Клуб C')
            RETURNING id
            """
        ).scalar_one()
        self._exec(
            """
            INSERT INTO season_clubs
                (season_id, club_id, fantasy_team_id, display_name)
            VALUES (:season, :club, '30', 'Клуб C')
            """,
            season=season_id,
            club=club_c,
        )
        tour_id = self._scalar(
            "SELECT id FROM fantasy_tours WHERE fantasy_tour_id = '1773'"
        )
        self._exec("DELETE FROM matches WHERE tour_id = :tour", tour=tour_id)
        self._exec(
            """
            INSERT INTO matches
                (season_id, tour_id, stat_match_id, scheduled_at,
                 home_club_id, away_club_id, home_score, away_score)
            VALUES (:season, :tour, '900003', '2025-07-25T16:00:00Z',
                    :home, :away, NULL, NULL)
            """,
            season=season_id,
            tour=tour_id,
            home=club_b,
            away=club_c,
        )

        report = build_squad_optimization(
            self.session_factory, tour_ref="1773", locked=["111"]
        )
        squad = report["solution"]["squad"]
        locked_entry = next(p for p in squad if p["fantasy_player_id"] == "111")
        self.assertTrue(locked_entry["is_locked"])
        self.assertEqual(locked_entry["expected_points"], 0.0)
        self.assertIsNone(locked_entry["match_id"])
        self.assertEqual(locked_entry["stat_source"], "blank_fixture")
        self.assertEqual(report["counts"]["locked"], 1)
        self.assertTrue(report["valid"])
        self.assertEqual(
            {p["fantasy_player_id"] for p in squad},
            {"111", "222"},
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


class StepTwentyThreeOptimizerTest(unittest.TestCase):
    """Captain on the upper tail, uncertainty-aware transfers, bench order, horizon."""

    @staticmethod
    def _candidate(pid: int, role: str, points: float, *, p: float = 1.0, unc: float = 0.0, future: float = 0.0, price: float = 5.0, club: int | None = None) -> Candidate:
        return Candidate(
            player_season_id=pid,
            fantasy_player_id=str(pid),
            player_name=f"P{pid}",
            role=role,
            club_id=club if club is not None else pid % 7 + 1,
            club_name="C",
            price=price,
            expected_points=points,
            p_appearance=p,
            uncertainty=unc,
            future_points=future,
        )

    def test_bench_order_maximises_the_expected_auto_sub_points(self) -> None:
        starters = [self._candidate(i, "MIDFIELDER", 3.0, p=0.7) for i in range(10)]
        # Same expected points. The gamble goes *first*: if he does not play
        # the reliable sub still takes the first vacancy, whereas the other
        # way round the gamble only ever sees a second vacancy.
        reliable = self._candidate(20, "DEFENDER", 1.5, p=1.0)
        gamble = self._candidate(21, "FORWARD", 1.5, p=0.3)
        keeper = self._candidate(22, "GOALKEEPER", 2.0, p=1.0)
        order = order_bench([reliable, keeper, gamble], starters)
        self.assertEqual([21, 20, 22], [c.player_season_id for c in order])
        self.assertGreater(
            expected_auto_sub_points([gamble, reliable], starters),
            expected_auto_sub_points([reliable, gamble], starters),
        )
        # What decides the order is the points a sub scores *when he plays*
        # (expected points over play probability): six a match beats five.
        strong = self._candidate(23, "DEFENDER", 3.0, p=0.5)
        order = order_bench([gamble, strong, keeper], starters)
        self.assertEqual(23, order[0].player_season_id)

    def test_no_absences_means_no_auto_sub_points(self) -> None:
        starters = [self._candidate(i, "MIDFIELDER", 3.0, p=1.0) for i in range(10)]
        bench = [self._candidate(20, "DEFENDER", 2.0, p=1.0)]
        self.assertAlmostEqual(0.0, expected_auto_sub_points(bench, starters))

    def test_attach_future_points_discounts_the_tours_ahead(self) -> None:
        rows = [
            {"model_name": "poisson_events", "player_season_id": 1, "expected_points": 3.0},
            {"model_name": "season_mean", "player_season_id": 1, "expected_points": 2.0},
        ]
        future = [
            [{"model_name": "poisson_events", "player_season_id": 1, "expected_points": 4.0}],
            [{"model_name": "poisson_events", "player_season_id": 1, "expected_points": 2.0}],
        ]
        stamped = attach_future_points(rows, future, model="poisson_events", decay=0.5)
        self.assertAlmostEqual(0.5 * 4.0 + 0.25 * 2.0, stamped[0]["future_points"])
        self.assertNotIn("future_points", stamped[1])
        self.assertEqual(3.0, stamped[0]["expected_points"])

    def _rules(self) -> SquadRules:
        return SquadRules(
            total_budget=100.0,
            total_players=15,
            starting_players=11,
            full_limits={"GOALKEEPER": (2, 2), "DEFENDER": (5, 5), "MIDFIELDER": (5, 5), "FORWARD": (3, 3)},
            starting_limits={"GOALKEEPER": (1, 1), "DEFENDER": (3, 5), "MIDFIELDER": (2, 5), "FORWARD": (1, 3)},
            max_same_team=15,
            total_transfers=2,
        )

    def _pool(self) -> list[Candidate]:
        pool = []
        pid = 1
        for role, count in (("GOALKEEPER", 3), ("DEFENDER", 7), ("MIDFIELDER", 7), ("FORWARD", 5)):
            for i in range(count):
                pool.append(self._candidate(pid, role, 2.0 + i * 0.5, price=4.0, club=pid))
                pid += 1
        return pool

    def test_captain_risk_weight_moves_the_armband_to_the_upper_tail(self) -> None:
        pool = self._pool()
        # Two forwards: one with the higher mean, one with a fat tail.
        pool = [c for c in pool if c.role != "FORWARD"]
        pool.append(self._candidate(101, "FORWARD", 6.0, unc=1.0, price=4.0, club=101))
        pool.append(self._candidate(102, "FORWARD", 5.5, unc=3.0, price=4.0, club=102))
        pool.append(self._candidate(103, "FORWARD", 1.0, price=4.0, club=103))
        mean = solve_squad(pool, self._rules(), captain_risk_weight=0.0)
        tail = solve_squad(pool, self._rules(), captain_risk_weight=0.5)
        self.assertEqual(101, mean["captain"]["player_season_id"])
        self.assertEqual(102, tail["captain"]["player_season_id"])
        self.assertAlmostEqual(5.5 + 1.5, tail["captain"]["captain_score"])
        # The reported objective stays in expected points.
        self.assertAlmostEqual(
            tail["starting_expected_points"] + 5.5, tail["objective_expected_points"]
        )

    def test_transfer_gain_sigma_blocks_an_uncertain_swap(self) -> None:
        pool = self._pool()
        rules = self._rules()
        base = solve_squad(pool, rules)
        current = [p["player_season_id"] for p in base["squad"]]
        # A newcomer worth 0.8 more than the best forward (so he would start),
        # but very uncertain.
        best_forward = max(
            (p for p in base["squad"] if p["role"] == "FORWARD"), key=lambda p: p["expected_points"]
        )
        pool.append(
            self._candidate(200, "FORWARD", best_forward["expected_points"] + 0.8, unc=4.0, price=4.0, club=200)
        )
        # The captain is chosen on the mean here so only the threshold speaks.
        flat = solve_squad(pool, rules, current_ids=current, max_transfers=1, min_transfer_gain=0.5, transfer_gain_sigma=0.0, captain_risk_weight=0.0)
        cautious = solve_squad(pool, rules, current_ids=current, max_transfers=1, min_transfer_gain=0.5, transfer_gain_sigma=0.25, captain_risk_weight=0.0)
        self.assertEqual(1, flat["transfers"]["made"])
        self.assertEqual(0, cautious["transfers"]["made"])
        self.assertEqual(0.25, cautious["transfers"]["gain_sigma"])

    def test_future_points_can_keep_a_player_in_the_roster(self) -> None:
        pool = [c for c in self._pool() if c.role != "FORWARD"]
        rules = self._rules()
        # Only one forward starts in this pool (5 defenders and 5 midfielders
        # are better), so the other two roster slots are decided by what the
        # tours ahead are worth. Four forwards for three slots: the starter,
        # then the two with the best runs ahead.
        pool.append(self._candidate(311, "FORWARD", 3.5, future=0.0, price=4.0, club=311))
        pool.append(self._candidate(312, "FORWARD", 3.0, future=1.0, price=4.0, club=312))
        pool.append(self._candidate(300, "FORWARD", 0.5, future=0.0, price=4.0, club=300))
        pool.append(self._candidate(301, "FORWARD", 0.5, future=5.0, price=4.0, club=301))
        solution = solve_squad(pool, rules)
        ids = {p["player_season_id"] for p in solution["squad"]}
        self.assertIn(301, ids)
        self.assertIn(312, ids)
        self.assertNotIn(300, ids)
        # Future points never enter the reported expected points of the eleven.
        starters = [p for p in solution["squad"] if p["is_starter"]]
        self.assertAlmostEqual(
            sum(p["expected_points"] for p in starters), solution["starting_expected_points"]
        )
