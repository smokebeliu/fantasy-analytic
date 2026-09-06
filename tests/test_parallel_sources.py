"""Unit tests for parallel sourcing (development-plan step 24).

A European cup is forecast from the national leagues its clubs play in at the
same time. These tests exercise the pure pieces in isolation: which league
seasons count as parallel, how a league result is translated into the cup's
goals, how a league snapshot lends an injury to the cup row, and how the
league layers enter a player's row (rates, appearance share, provenance label)
and a club's strength — always cut at the target tour's deadline.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from fantasy_analytics.features import (
    DEFAULT_LEAGUE_STRENGTH,
    LEAGUE_STRENGTH,
    PARALLEL_WEIGHT,
    STAT_SOURCE_CURRENT,
    STAT_SOURCE_PARALLEL,
    STAT_SOURCE_PRIOR,
    Appearance,
    ClubMatch,
    Fixture,
    HistoryLayer,
    ParallelContext,
    PriorContext,
    PriorPlayer,
    RolePrior,
    _blended_club_matches,
    _build_row,
    _club_strengths,
    league_strength,
    merge_availability,
    parallel_club_layers,
    parallel_layers_for_player,
    parallel_season_overlaps,
    scale_club_match,
)

UTC = timezone.utc
CUTOFF = datetime(2026, 9, 8, 16, 45, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 8, 19, 0, tzinfo=UTC)


def _appearance(match_id: int, days_before: int, **kwargs) -> Appearance:
    base = {
        "minutes": 90,
        "points": 6,
        "goals": 1,
        "assists": 0,
    }
    base.update(kwargs)
    return Appearance(
        match_id=match_id,
        scheduled_at=CUTOFF - timedelta(days=days_before),
        **base,
    )


def _club_match(match_id: int, days_before: int, scored: int = 2, conceded: int = 1) -> ClubMatch:
    return ClubMatch(
        match_id=match_id,
        scheduled_at=CUTOFF - timedelta(days=days_before),
        is_home=match_id % 2 == 0,
        goals_scored=scored,
        goals_conceded=conceded,
    )


ROLE_PRIOR = RolePrior(
    goals_per90=0.2,
    assists_per90=0.15,
    saves_per90=0.0,
    recoveries_per90=5.0,
    yellows_per90=0.15,
    mean_minutes=70.0,
    points_per90=4.0,
)


def _row(**overrides):
    """A cup row for a forward with no cup history of his own."""
    fixture = Fixture(
        match_id=900,
        scheduled_at=KICKOFF,
        club_id=1,
        opponent_club_id=2,
        is_home=True,
    )
    params = dict(
        player={
            "player_season_id": 10,
            "player_id": 100,
            "fantasy_player_id": "f10",
            "role": "FORWARD",
            "player_name": "Striker",
        },
        fixtures=[fixture],
        club_name="Club",
        opponent_names={2: "Opponent"},
        cutoff=CUTOFF,
        target_match_ids=frozenset({900}),
        appearances=[],
        club_matches=[],
        snapshot={
            "availability_status": "UNKNOWN",
            "status_description": "",
            "price": 8.0,
            "selected_by": 20.0,
            "form": None,
        },
        strengths={},
        league={
            "home_attack": 1.5,
            "home_defense": 1.2,
            "away_attack": 1.2,
            "away_defense": 1.5,
        },
        role_prior=ROLE_PRIOR,
    )
    params.update(overrides)
    return _build_row(**params)


class SeasonOverlapTest(unittest.TestCase):
    def test_a_league_running_at_the_cutoff_is_parallel(self) -> None:
        self.assertTrue(
            parallel_season_overlaps(
                datetime(2026, 8, 15, tzinfo=UTC),
                datetime(2027, 5, 30, tzinfo=UTC),
                target_starts_at=datetime(2026, 7, 7, tzinfo=UTC),
                cutoff=CUTOFF,
            )
        )

    def test_a_league_season_that_starts_after_the_cutoff_is_the_future(self) -> None:
        self.assertFalse(
            parallel_season_overlaps(
                datetime(2026, 9, 20, tzinfo=UTC),
                None,
                target_starts_at=datetime(2026, 7, 7, tzinfo=UTC),
                cutoff=CUTOFF,
            )
        )

    def test_last_years_league_season_is_not_parallel(self) -> None:
        self.assertFalse(
            parallel_season_overlaps(
                datetime(2025, 8, 15, tzinfo=UTC),
                datetime(2026, 5, 24, tzinfo=UTC),
                target_starts_at=datetime(2026, 7, 7, tzinfo=UTC),
                cutoff=CUTOFF,
            )
        )

    def test_unknown_dates_are_taken_as_overlapping(self) -> None:
        self.assertTrue(
            parallel_season_overlaps(None, None, target_starts_at=None, cutoff=CUTOFF)
        )


class LeagueFactorTest(unittest.TestCase):
    def test_known_and_unknown_leagues(self) -> None:
        self.assertEqual(league_strength("spain"), LEAGUE_STRENGTH["spain"])
        self.assertEqual(league_strength("moon"), DEFAULT_LEAGUE_STRENGTH)
        self.assertEqual(league_strength(None), DEFAULT_LEAGUE_STRENGTH)

    def test_a_league_result_is_translated_into_the_cups_goals(self) -> None:
        match = _club_match(1, 3, scored=2, conceded=1)
        scaled = scale_club_match(match, 0.5)
        self.assertEqual(scaled.goals_scored, 1.0)
        self.assertEqual(scaled.goals_conceded, 2.0)
        self.assertIs(scale_club_match(match, 1.0), match)


class AvailabilityMergeTest(unittest.TestCase):
    def _own(self, status: str = "UNKNOWN") -> dict:
        return {
            "availability_status": status,
            "status_description": "",
            "price": 5.0,
            "selected_by": 1.0,
            "form": None,
        }

    def test_the_league_snapshot_lends_an_injury(self) -> None:
        merged = merge_availability(
            self._own(),
            [
                ("england", self._own("UNKNOWN")),
                ("spain", {"availability_status": "INJURY", "status_description": "2026-10-01"}),
            ],
        )
        self.assertEqual(merged["availability_status"], "INJURY")
        self.assertEqual(merged["status_description"], "2026-10-01")
        self.assertEqual(merged["availability_source"], "spain")
        # Price and ownership stay the cup's own.
        self.assertEqual(merged["price"], 5.0)

    def test_the_cups_own_word_wins(self) -> None:
        own = self._own("DISQUALIFICATION")
        merged = merge_availability(own, [("spain", self._own("INJURY"))])
        self.assertIs(merged, own)

    def test_nothing_to_lend_leaves_the_snapshot_alone(self) -> None:
        own = self._own()
        self.assertIs(merge_availability(own, [("spain", self._own())]), own)
        self.assertIsNone(merge_availability(None, [("spain", None)]))

    def test_a_missing_cup_snapshot_still_takes_the_injury(self) -> None:
        merged = merge_availability(None, [("spain", self._own("INJURY"))])
        self.assertEqual(merged["availability_status"], "INJURY")
        self.assertIsNone(merged["price"])


def _context(**overrides) -> ParallelContext:
    params = dict(
        run_id=50,
        season_id=5,
        competition_slug="spain",
        competition_name="Испания",
        season_name="2026/2027",
        factor=0.9,
        appearances={},
        club_matches={},
        by_player_id={},
        snapshots={},
        prior=None,
    )
    params.update(overrides)
    return ParallelContext(**params)


class LayerResolutionTest(unittest.TestCase):
    def test_a_league_player_gets_his_appearances_and_his_clubs_matches(self) -> None:
        apps = [_appearance(1, 5), _appearance(2, 12)]
        clubs = [_club_match(1, 5), _club_match(2, 12), _club_match(3, 19)]
        context = _context(
            appearances={77: apps},
            club_matches={1: clubs},
            by_player_id={100: PriorPlayer(player_season_id=77, club_id=1, role="FORWARD")},
        )
        layers = parallel_layers_for_player(100, [context], club_id=1)
        self.assertEqual(len(layers), 1)
        layer = layers[0]
        self.assertEqual(layer.source, "spain")
        self.assertFalse(layer.is_prior)
        self.assertEqual(layer.weight, PARALLEL_WEIGHT)
        self.assertEqual(layer.factor, 0.9)
        self.assertEqual(layer.appearances, apps)
        self.assertEqual(layer.club_matches, clubs)

    def test_a_player_registered_elsewhere_in_the_league_keeps_only_his_own_play(self) -> None:
        apps = [_appearance(1, 5)]
        context = _context(
            appearances={77: apps},
            club_matches={9: [_club_match(1, 5)]},
            by_player_id={100: PriorPlayer(player_season_id=77, club_id=9, role="FORWARD")},
        )
        layers = parallel_layers_for_player(100, [context], club_id=1)
        self.assertEqual(len(layers), 1)
        self.assertEqual(layers[0].appearances, apps)
        self.assertEqual(layers[0].club_matches, [])

    def test_the_leagues_last_season_needs_appearances(self) -> None:
        prior = PriorContext(
            run_id=40,
            season_id=4,
            appearances={66: [_appearance(1, 400)]},
            club_matches={1: [_club_match(1, 400)]},
            by_player_id={
                100: PriorPlayer(player_season_id=66, club_id=1, role="FORWARD"),
                101: PriorPlayer(player_season_id=67, club_id=1, role="FORWARD"),
            },
            role_priors={},
            season_name="2025/2026",
        )
        context = _context(prior=prior)
        layers = parallel_layers_for_player(100, [context], club_id=1)
        self.assertEqual([layer.is_prior for layer in layers], [True])
        self.assertEqual(layers[0].season_name, "2025/2026")
        self.assertEqual(parallel_layers_for_player(101, [context], club_id=1), [])
        self.assertEqual(parallel_layers_for_player(999, [context], club_id=1), [])

    def test_club_layers_are_restricted_to_the_cups_clubs(self) -> None:
        context = _context(
            club_matches={1: [_club_match(1, 5)], 8: [_club_match(2, 5)]},
            prior=PriorContext(
                run_id=40,
                season_id=4,
                appearances={},
                club_matches={1: [_club_match(3, 400)]},
                by_player_id={},
                role_priors={},
            ),
        )
        layers = parallel_club_layers([context], [1, 2])
        self.assertEqual(set(layers), {1})
        self.assertEqual([layer.is_prior for layer in layers[1]], [False, True])


class RowWithParallelLayersTest(unittest.TestCase):
    def _league_layer(self, *, days: tuple[int, ...] = (3, 10, 17), goals: int = 1) -> HistoryLayer:
        apps = [
            _appearance(match_id, day, goals=goals) for match_id, day in enumerate(days, start=1)
        ]
        clubs = [_club_match(match_id, day) for match_id, day in enumerate(days, start=1)]
        return HistoryLayer(
            source="spain",
            season_name="2026/2027",
            appearances=apps,
            club_matches=clubs,
            weight=PARALLEL_WEIGHT,
            factor=0.9,
            is_prior=False,
        )

    def test_without_layers_the_row_is_a_newcomer(self) -> None:
        row = _row()
        self.assertTrue(row["is_newcomer"])
        self.assertEqual(row["stat_source"], STAT_SOURCE_CURRENT)
        self.assertEqual(row["parallel_appearances"], 0)
        self.assertEqual(row["league_factor"], 1.0)
        self.assertEqual([s["kind"] for s in row["sources"]], ["own", "own"])

    def test_league_play_makes_the_player_a_known_quantity(self) -> None:
        row = _row(parallel_layers=[self._league_layer()])
        self.assertFalse(row["is_newcomer"])
        self.assertTrue(row["has_history"])
        self.assertEqual(row["stat_source"], STAT_SOURCE_PARALLEL)
        self.assertEqual(row["parallel_appearances"], 3)
        self.assertEqual(row["parallel_club_matches"], 3)
        self.assertEqual(row["parallel_weight"], PARALLEL_WEIGHT)
        self.assertEqual(row["league_factor"], 0.9)
        # He played every one of his club's league matches: the share is 1.
        self.assertEqual(row["appearance_share"], 1.0)
        self.assertEqual(row["p_appearance"], 1.0)
        self.assertEqual(row["expected_minutes"], 90.0)
        # Three goals in three matches, at 0.7 x 0.9, shrunk towards the role
        # average: well above the prior, well below a goal a game.
        self.assertGreater(row["goals_per90"], ROLE_PRIOR.goals_per90)
        self.assertLess(row["goals_per90"], 1.0)
        # The cup's own totals stay empty: the leakage audit recomputes them.
        self.assertEqual(row["current_appearances"], 0)
        self.assertGreater(row["total_appearances"], 0)
        parallel = [s for s in row["sources"] if s["kind"] == "parallel"]
        self.assertEqual(len(parallel), 1)
        self.assertEqual(parallel[0]["competition"], "spain")
        self.assertEqual(parallel[0]["appearances"], 3)

    def test_the_cups_own_play_outranks_the_league_in_the_label(self) -> None:
        own = [_appearance(50, 1, goals=0)]
        row = _row(
            appearances=own,
            club_matches=[_club_match(50, 1)],
            parallel_layers=[self._league_layer()],
        )
        self.assertEqual(row["stat_source"], STAT_SOURCE_CURRENT)
        self.assertEqual(row["current_appearances"], 1)
        self.assertEqual(row["parallel_appearances"], 3)
        # The rolling window mixes both by date: the cup match is the latest.
        self.assertEqual(row["appearances_5"], 4)

    def test_a_league_match_after_the_cutoff_never_enters(self) -> None:
        layer = HistoryLayer(
            source="spain",
            season_name="2026/2027",
            appearances=[_appearance(1, 3), _appearance(2, -1)],
            club_matches=[_club_match(1, 3), _club_match(2, -1)],
            weight=PARALLEL_WEIGHT,
            factor=0.9,
            is_prior=False,
        )
        row = _row(parallel_layers=[layer])
        self.assertEqual(row["parallel_appearances"], 1)
        self.assertEqual(row["parallel_club_matches"], 1)

    def test_a_league_bench_warmer_is_not_expected_to_play(self) -> None:
        layer = HistoryLayer(
            source="spain",
            season_name="2026/2027",
            appearances=[],
            club_matches=[_club_match(1, 3), _club_match(2, 10), _club_match(3, 17)],
            weight=PARALLEL_WEIGHT,
            factor=0.9,
            is_prior=False,
        )
        row = _row(parallel_layers=[layer])
        # No play anywhere: a newcomer, but one whose club has played three
        # matches without him, so the assumed probability has faded.
        self.assertTrue(row["is_newcomer"])
        self.assertLess(row["p_appearance"], 0.2)

    def test_the_league_factor_scales_goals_but_not_minutes(self) -> None:
        strong = _row(parallel_layers=[self._league_layer()])
        weak_layer = HistoryLayer(
            source="minor",
            season_name="2026",
            appearances=self._league_layer().appearances,
            club_matches=self._league_layer().club_matches,
            weight=PARALLEL_WEIGHT,
            factor=0.5,
            is_prior=False,
        )
        weak = _row(parallel_layers=[weak_layer])
        self.assertLess(weak["goals_per90"], strong["goals_per90"])
        self.assertEqual(weak["expected_minutes"], strong["expected_minutes"])
        self.assertEqual(weak["total_minutes"], strong["total_minutes"])

    def test_last_seasons_league_is_a_prior_and_is_labelled_so(self) -> None:
        prior_layer = HistoryLayer(
            source="spain",
            season_name="2025/2026",
            appearances=[_appearance(i, 300 + i * 7) for i in range(1, 11)],
            club_matches=[_club_match(i, 300 + i * 7) for i in range(1, 11)],
            weight=PARALLEL_WEIGHT,
            factor=0.9,
            is_prior=True,
        )
        row = _row(parallel_layers=[prior_layer])
        self.assertFalse(row["is_newcomer"])
        self.assertEqual(row["stat_source"], STAT_SOURCE_PRIOR)
        self.assertEqual(row["parallel_appearances"], 0)
        self.assertGreater(row["prior_season_weight"], 0.0)
        self.assertGreater(row["p_appearance"], 0.5)

    def test_a_red_card_in_the_league_does_not_ban_in_the_cup(self) -> None:
        layer = HistoryLayer(
            source="spain",
            season_name="2026/2027",
            appearances=[_appearance(1, 3, red_cards=1)],
            club_matches=[_club_match(1, 3)],
            weight=PARALLEL_WEIGHT,
            factor=0.9,
            is_prior=False,
        )
        row = _row(parallel_layers=[layer])
        self.assertFalse(row["red_card_suspension"])
        self.assertTrue(row["is_available"])

    def test_a_lent_injury_marks_the_row_out(self) -> None:
        row = _row(
            snapshot={
                "availability_status": "INJURY",
                "status_description": "",
                "price": 8.0,
                "selected_by": 20.0,
                "form": None,
                "availability_source": "spain",
            },
            parallel_layers=[self._league_layer()],
        )
        self.assertFalse(row["is_available"])
        self.assertEqual(row["p_appearance"], 0.0)
        self.assertEqual(row["availability_source"], "spain")


class ClubStrengthWithLeagueLayersTest(unittest.TestCase):
    def test_league_results_describe_a_club_with_no_cup_history(self) -> None:
        layers = {
            1: [
                HistoryLayer(
                    source="spain",
                    season_name="2026/2027",
                    appearances=[],
                    club_matches=[_club_match(i, 7 * i, scored=3, conceded=0) for i in range(1, 4)],
                    weight=PARALLEL_WEIGHT,
                    factor=0.5,
                    is_prior=False,
                )
            ]
        }
        pooled = _blended_club_matches({}, {}, layers)
        self.assertEqual(set(pooled), {1})
        matches, weights = zip(*pooled[1])
        # Translated into the cup's goals and admitted at the layer's weight.
        self.assertEqual({m.goals_scored for m in matches}, {1.5})
        self.assertEqual({m.goals_conceded for m in matches}, {0.0})
        self.assertAlmostEqual(max(weights), PARALLEL_WEIGHT)
        strengths, _ = _club_strengths(pooled, shrink_matches=0, venue_mode="split")
        self.assertGreater(strengths[1]["home_attack"], 1.0)

    def test_league_matches_push_the_cups_own_prior_down(self) -> None:
        prior = {1: [_club_match(i, 300 + 7 * i) for i in range(1, 9)]}
        alone = _blended_club_matches({}, prior)
        with_league = _blended_club_matches(
            {},
            prior,
            {
                1: [
                    HistoryLayer(
                        source="spain",
                        season_name="2026/2027",
                        appearances=[],
                        club_matches=[_club_match(i, 7 * i) for i in range(1, 7)],
                        weight=PARALLEL_WEIGHT,
                        factor=1.0,
                        is_prior=False,
                    )
                ]
            },
        )
        prior_weight_alone = max(w for m, w in alone[1] if m.scheduled_at < CUTOFF - timedelta(days=200))
        prior_weight_with = max(
            w for m, w in with_league[1] if m.scheduled_at < CUTOFF - timedelta(days=200)
        )
        self.assertEqual(prior_weight_alone, 1.0)
        self.assertLess(prior_weight_with, prior_weight_alone)

    def test_without_layers_nothing_changes(self) -> None:
        current = {1: [_club_match(1, 3), _club_match(2, 10)]}
        prior = {1: [_club_match(3, 300)], 2: [_club_match(4, 300)]}
        self.assertEqual(
            _blended_club_matches(current, prior),
            _blended_club_matches(current, prior, {}),
        )


if __name__ == "__main__":
    unittest.main()
