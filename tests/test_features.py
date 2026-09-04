"""Unit and integration tests for the analytical feature dataset (step 6).

The unit tests exercise the pure, leakage-sensitive helpers (rolling windows,
per-90 rates and club strength) in isolation. The integration tests import a
small synthetic season into a real PostgreSQL database, publish it through the
quality gate, inject a second (future) tour and then build the feature dataset
to prove that features come only from matches before the cutoff, that a
target-tour match can never leak into the history, and that every row is stamped
with player, tour, cutoff and feature version. They are skipped automatically
when no database is reachable.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from sqlalchemy import text

from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.features import (
    FEATURE_VERSION,
    PRIOR_SEASON_HALF_LIFE,
    UNAVAILABLE_STATUSES,
    Appearance,
    ClubMatch,
    FeaturesError,
    _club_strengths,
    _venue_strength,
    blend,
    build_feature_dataset,
    decayed_share,
    per90,
    pending_red_card_suspension,
    played_before_cutoff,
    prior_season_weight,
    recent_before_cutoff,
    share_blend_weights,
)
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.quality import run_quality_checks

# Reuse the fake client and consistent fixture from the sibling test modules.
from test_ingestion import FakeClient
from test_quality import _consistent_fixture

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)

_CUTOFF = datetime(2025, 8, 1, tzinfo=timezone.utc)


def _appearance(
    day: int, points: int, minutes: int = 80, *, red_cards: int = 0
) -> Appearance:
    return Appearance(
        match_id=day,
        scheduled_at=datetime(2025, 7, day, 12, 0, tzinfo=timezone.utc),
        minutes=minutes,
        points=points,
        goals=0,
        assists=0,
        red_cards=red_cards,
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


class PureHelperTest(unittest.TestCase):
    def test_recent_before_cutoff_excludes_future_and_sorts_desc(self) -> None:
        appearances = [
            _appearance(10, 5),
            _appearance(20, 6),
            _appearance(31, 9),  # 2025-07-31 -> before the 2025-08-01 cutoff
        ]
        future = Appearance(
            match_id=99,
            scheduled_at=datetime(2025, 8, 5, 12, 0, tzinfo=timezone.utc),
            minutes=90,
            points=15,
            goals=1,
            assists=1,
        )
        recent = recent_before_cutoff([*appearances, future], _CUTOFF)

        self.assertEqual([31, 20, 10], [a.match_id for a in recent])
        self.assertNotIn(99, [a.match_id for a in recent])

    def test_recent_before_cutoff_excludes_target_tour_matches(self) -> None:
        # A match before the cutoff that belongs to the target tour must still
        # be dropped (second line of defence against a mis-dated deadline).
        appearances = [_appearance(10, 5), _appearance(20, 6)]
        recent = recent_before_cutoff(
            appearances, _CUTOFF, exclude_match_ids=frozenset({20})
        )
        self.assertEqual([10], [a.match_id for a in recent])

    def test_per90_zero_when_no_minutes(self) -> None:
        self.assertEqual(0.0, per90(5, 0))
        self.assertEqual(9.0, per90(9, 90))
        self.assertEqual(4.5, per90(9, 180))

    def test_club_strengths_and_venue_fallback(self) -> None:
        club_matches = {
            1: [
                (ClubMatch(1, datetime(2025, 7, 1, tzinfo=timezone.utc), True, 3, 0), 1.0),
                (ClubMatch(2, datetime(2025, 7, 8, tzinfo=timezone.utc), False, 1, 2), 1.0),
            ],
            2: [
                (ClubMatch(3, datetime(2025, 7, 1, tzinfo=timezone.utc), False, 0, 3), 1.0),
            ],
        }
        strengths, league = _club_strengths(club_matches, shrink_matches=0)

        self.assertEqual(3.0, strengths[1]["home_attack"])
        self.assertEqual(0.0, strengths[1]["home_defense"])
        self.assertEqual(1.0, strengths[1]["away_attack"])
        self.assertEqual(2.0, strengths[1]["away_defense"])

        # Club 2 has no home match: its home strength falls back to the league
        # mean of home attack (only club 1 played home, scoring 3).
        attack, defense = _venue_strength(2, True, strengths, league)
        self.assertEqual(league["home_attack"], attack)
        self.assertEqual(3.0, attack)

        # An unknown club falls back entirely to the league averages.
        attack, defense = _venue_strength(999, False, strengths, league)
        self.assertEqual(league["away_attack"], attack)
        self.assertEqual(league["away_defense"], defense)

    def test_a_weighted_match_counts_less_than_a_full_one(self) -> None:
        # Last season's matches arrive discounted, so a club that has scored
        # once this season is not suddenly a one-goal-a-game side.
        now = ClubMatch(1, datetime(2025, 8, 1, tzinfo=timezone.utc), True, 1, 0)
        then = ClubMatch(2, datetime(2025, 5, 1, tzinfo=timezone.utc), True, 3, 0)
        strengths, _ = _club_strengths(
            {1: [(now, 1.0), (then, 1.0)]}, shrink_matches=0
        )
        self.assertEqual(2.0, strengths[1]["home_attack"])
        discounted, _ = _club_strengths(
            {1: [(now, 1.0), (then, 0.25)]}, shrink_matches=0
        )
        self.assertEqual(1.4, discounted[1]["home_attack"])

    def test_a_venue_strength_is_shrunk_towards_the_league_average(self) -> None:
        # One 3-0 at home does not make a promoted club the league's best
        # attack: with pseudo-matches of the league average in the pool it sits
        # between its own result and the average, and a club with no home
        # match is exactly the average.
        when = datetime(2025, 8, 1, tzinfo=timezone.utc)
        club_matches = {
            1: [(ClubMatch(1, when, True, 3, 0), 1.0)],
            2: [(ClubMatch(2, when, True, 1, 1), 1.0)],
            3: [(ClubMatch(3, when, True, 1, 1), 1.0)],
        }
        strengths, league = _club_strengths(club_matches, shrink_matches=2)
        self.assertAlmostEqual(5.0 / 3.0, league["home_attack"], places=4)
        # (3 x 1 + 5/3 x 2) / 3
        self.assertAlmostEqual((3 + 10 / 3) / 3, strengths[1]["home_attack"], places=3)
        self.assertLess(strengths[1]["home_attack"], 3.0)
        self.assertGreater(strengths[1]["home_attack"], league["home_attack"])
        self.assertEqual(league["away_attack"], strengths[1]["away_attack"])
        # Without pseudo-matches the raw mean comes back.
        raw, _ = _club_strengths(club_matches, shrink_matches=0)
        self.assertEqual(3.0, raw[1]["home_attack"])

    def test_injury_is_an_unavailable_status(self) -> None:
        self.assertIn("INJURY", UNAVAILABLE_STATUSES)
        self.assertNotIn("FIERY", UNAVAILABLE_STATUSES)


class RedCardSuspensionTest(unittest.TestCase):
    def _club(self, day: int) -> ClubMatch:
        return ClubMatch(
            day,
            datetime(2025, 7, day, 12, 0, tzinfo=timezone.utc),
            True,
            1,
            0,
        )

    def test_no_red_card_is_not_a_suspension(self) -> None:
        self.assertFalse(
            pending_red_card_suspension([_appearance(10, 5)], [self._club(10)])
        )

    def test_a_red_card_in_the_last_match_suspends_the_next_tour(self) -> None:
        self.assertTrue(
            pending_red_card_suspension(
                [_appearance(10, 5, red_cards=1)], [self._club(10)]
            )
        )

    def test_a_later_club_match_serves_the_one_match_ban(self) -> None:
        # A double gameweek (or any later fixture) is the match the player
        # already missed, so he is available again for the target tour.
        self.assertFalse(
            pending_red_card_suspension(
                [_appearance(10, 5, red_cards=1)],
                [self._club(20), self._club(10)],
            )
        )

    def test_an_older_served_red_does_not_hide_a_fresh_one(self) -> None:
        self.assertTrue(
            pending_red_card_suspension(
                [
                    _appearance(5, 4, red_cards=1),
                    _appearance(20, 8, red_cards=1),
                ],
                [self._club(20), self._club(10), self._club(5)],
            )
        )


class HistoryBlendTest(unittest.TestCase):
    """Step 14 revisited: last season fades, it does not vanish."""

    def test_prior_weight_starts_whole_and_halves_every_half_life(self) -> None:
        self.assertEqual(1.0, prior_season_weight(0))
        self.assertAlmostEqual(0.5, prior_season_weight(5, half_life=5), places=6)
        self.assertAlmostEqual(0.25, prior_season_weight(10, half_life=5), places=6)
        # The default half-life is the module constant.
        self.assertAlmostEqual(
            0.5, prior_season_weight(int(PRIOR_SEASON_HALF_LIFE)), places=6
        )
        self.assertLess(prior_season_weight(30), 0.02)

    def test_prior_weight_is_strictly_decreasing(self) -> None:
        weights = [prior_season_weight(n) for n in range(0, 20)]
        self.assertEqual(weights, sorted(weights, reverse=True))
        self.assertEqual(len(set(weights)), len(weights))

    def test_the_current_season_outgrows_the_prior_one(self) -> None:
        # The point of the blend: whatever last season said, the new one wins
        # once it has said enough. Here last season claims a rate of 10 and the
        # new one a rate of 0.
        def blended(played: int) -> float:
            now_weight, prior_weight = share_blend_weights(
                played, prior_season_weight(played)
            )
            return blend(0.0, now_weight, 10.0, prior_weight)

        drifting = [blended(played) for played in (0, 1, 5, 10, 20)]
        self.assertEqual(10.0, drifting[0])
        self.assertEqual(drifting, sorted(drifting, reverse=True))
        self.assertLess(drifting[-1], 1.0)

    def test_share_weights_cap_both_seasons_at_the_same_size(self) -> None:
        # A finished 38-match season must not outvote the new one just by being
        # longer, so each side contributes at most SHARE_BLEND_WINDOW matches.
        current, prior = share_blend_weights(38, 1.0)
        self.assertEqual(current, prior)

    def test_decayed_share_weights_recent_matches_more(self) -> None:
        # Missing the two most recent matches hurts more than missing the two
        # oldest ones.
        recent_absence, total = decayed_share([False, False, True, True], 0.5)
        old_absence, _ = decayed_share([True, True, False, False], 0.5)
        self.assertLess(recent_absence / total, old_absence / total)
        # A decay of 1 makes every match count the same.
        flat, flat_total = decayed_share([True, False, True, False], 1.0)
        self.assertEqual(0.5, flat / flat_total)

    def test_an_unused_substitute_is_not_an_appearance(self) -> None:
        # Sports.ru returns a row for every named squad member, so a 0-minute
        # row means the player sat on the bench and never came on.
        history = [_appearance(10, 5, minutes=90), _appearance(20, 0, minutes=0)]
        self.assertEqual(2, len(recent_before_cutoff(history, _CUTOFF)))
        played = played_before_cutoff(history, _CUTOFF)
        self.assertEqual([10], [a.match_id for a in played])


@requires_database
class FeatureDatasetIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)
        self.run_id = self._import_and_publish()

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
        """Insert a second, not-yet-played tour with one club_a vs club_b match."""
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

    def _add_second_match_to_future_tour(self) -> None:
        """Move a third match into the future tour, doubling both clubs up."""
        season_id = self._scalar("SELECT id FROM seasons LIMIT 1")
        tour_id = self._scalar(
            "SELECT id FROM fantasy_tours WHERE fantasy_tour_id = '1773'"
        )
        club_a = self._scalar(
            "SELECT club_id FROM season_clubs WHERE fantasy_team_id = '10'"
        )
        club_b = self._scalar(
            "SELECT club_id FROM season_clubs WHERE fantasy_team_id = '20'"
        )
        self._exec(
            """
            INSERT INTO matches
                (season_id, tour_id, stat_match_id, scheduled_at,
                 home_club_id, away_club_id, home_score, away_score)
            VALUES (:season, :tour, '900003', '2025-07-28T16:00:00Z',
                    :home, :away, NULL, NULL)
            """,
            season=season_id,
            tour=tour_id,
            home=club_b,
            away=club_a,
        )

    def _rows_by_player(self, report: dict) -> dict[str, dict]:
        return {row["fantasy_player_id"]: row for row in report["rows"]}

    def test_a_club_doubled_up_by_a_postponement_gets_both_matches(self) -> None:
        # Fantasy tours are time windows that cannot overlap, so a postponed
        # match is re-attached to whichever tour it now falls in and a club can
        # end up playing twice inside one.
        self._add_future_tour()
        self._add_second_match_to_future_tour()

        report = build_feature_dataset(self.session_factory, tour_ref="1773")

        self.assertEqual(2, report["counts"]["double_fixture_clubs"])
        self.assertEqual(2, report["counts"]["double_fixture_rows"])
        home = self._rows_by_player(report)["111"]
        self.assertEqual(2, home["fixture_count"])
        # Reported in kickoff order, and the club hosts one and visits the other.
        self.assertEqual([True, False], [f["is_home"] for f in home["tour_fixtures"]])
        # The flat fields still describe the first match, for readers that never
        # had to think about a club playing twice.
        self.assertEqual(home["tour_fixtures"][0]["match_id"], home["match_id"])
        self.assertTrue(home["is_home"])

    def test_a_club_without_a_fixture_produces_no_row(self) -> None:
        # The mirror image: the tour a match was moved *out* of has a blank for
        # that club, and a player who cannot score has nothing to forecast.
        self._add_future_tour()
        club_c = self._exec(
            "INSERT INTO clubs (stat_team_id, canonical_name) "
            "VALUES ('club_c', 'Клуб C') RETURNING id"
        ).scalar_one()
        self._exec(
            "UPDATE matches SET away_club_id = :c WHERE stat_match_id = '900002'",
            c=club_c,
        )

        report = build_feature_dataset(self.session_factory, tour_ref="1773")

        self.assertEqual(1, report["counts"]["rows"])
        self.assertEqual(1, report["counts"]["players_without_fixture"])
        self.assertNotIn("222", self._rows_by_player(report))

    def test_the_cutoff_never_outlives_the_tours_first_kickoff(self) -> None:
        # A moved match takes the deadline with it, and the source does not
        # always move it back far enough. A deadline after a kickoff would let
        # the tour's own results into the club strengths that predict it.
        self._add_future_tour()
        self._exec(
            "UPDATE fantasy_tours SET transfers_deadline_at = "
            "'2025-07-25T18:00:00Z' WHERE fantasy_tour_id = '1773'"
        )

        report = build_feature_dataset(self.session_factory, tour_ref="1773")

        self.assertEqual("2025-07-25T16:00:00+00:00", report["cutoff"])

    def test_dataset_uses_only_pre_cutoff_history(self) -> None:
        self._add_future_tour()

        report = build_feature_dataset(self.session_factory, tour_ref="1773")

        self.assertEqual(FEATURE_VERSION, report["feature_version"])
        self.assertEqual("1773", report["tour"]["fantasy_tour_id"])
        self.assertEqual("2025-07-24T16:00:00+00:00", report["cutoff"])
        self.assertEqual(2, report["counts"]["rows"])

        rows = self._rows_by_player(report)
        home = rows["111"]
        away = rows["222"]

        # Row identity requirements: player, tour, cutoff and feature version.
        for row in (home, away):
            self.assertEqual(FEATURE_VERSION, row["feature_version"])
            self.assertEqual(report["cutoff"], row["tour_cutoff"])
            self.assertIn("player_season_id", row)

        # Only the tour-1 match (before the cutoff) is used as history.
        self.assertEqual(1, home["total_appearances"])
        self.assertEqual(1, home["appearances_3"])
        self.assertTrue(home["has_history"])
        self.assertEqual(12.0, home["points_avg_3"])
        self.assertEqual(78, home["total_minutes"])
        self.assertEqual(round(12 / 78 * 90, 4), home["points_per90"])

        # Fixture context: club_a hosts, club_b visits.
        self.assertTrue(home["is_home"])
        self.assertFalse(away["is_home"])
        self.assertEqual(home["club_id"], away["opponent_club_id"])
        # 2025-07-25 16:00 minus 2025-07-18 17:30 -> 6 whole days.
        self.assertEqual(6, home["rest_days"])

        # Availability and appearance modelling for a fit, ever-present player.
        self.assertTrue(home["is_available"])
        self.assertEqual(1.0, home["p_appearance"])
        self.assertEqual(78.0, home["expected_minutes"])
        self.assertEqual(1.0, home["appearance_share"])
        self.assertEqual(1.0, home["start_share"])
        self.assertFalse(home["red_card_suspension"])

    def test_a_red_card_makes_the_player_unavailable_next_tour(self) -> None:
        # Snapshot status stays FIT; the ban is derived from the previous
        # match's red card, which is what a historical (or just-played) tour
        # actually knows.
        self._add_future_tour()
        self._exec(
            "UPDATE player_match_stats SET red_cards = 1 "
            "WHERE player_season_id = ("
            "  SELECT id FROM player_seasons WHERE fantasy_player_id = '111')"
        )

        report = build_feature_dataset(self.session_factory, tour_ref="1773")
        rows = self._rows_by_player(report)
        banned = rows["111"]
        available = rows["222"]

        self.assertTrue(banned["red_card_suspension"])
        self.assertFalse(banned["is_available"])
        self.assertEqual(0.0, banned["p_appearance"])
        self.assertEqual(0.0, banned["expected_minutes"])
        self.assertFalse(available["red_card_suspension"])
        self.assertTrue(available["is_available"])
        self.assertGreater(available["p_appearance"], 0.0)

    def test_target_tour_match_never_leaks_into_history(self) -> None:
        # Tour 1's deadline (17:50) is after its own kickoff (17:30) in the
        # fixture, so only the explicit target-tour exclusion prevents leakage.
        report = build_feature_dataset(self.session_factory, tour_ref="1772")

        self.assertEqual("1772", report["tour"]["fantasy_tour_id"])
        for row in report["rows"]:
            self.assertEqual(0, row["total_appearances"])
            self.assertFalse(row["has_history"])
            self.assertEqual(0.0, row["points_avg_5"])

    def test_default_selects_active_snapshot(self) -> None:
        self._add_future_tour()

        report = build_feature_dataset(self.session_factory, tour_ref="1773")

        self.assertEqual(self.run_id, report["run_id"])

    def test_unknown_tour_raises(self) -> None:
        with self.assertRaises(FeaturesError):
            build_feature_dataset(self.session_factory, tour_ref="does-not-exist")

    def test_finished_season_without_tour_raises(self) -> None:
        # Every imported tour is FINISHED, so an implicit "next tour" fails.
        with self.assertRaises(FeaturesError):
            build_feature_dataset(self.session_factory)


if __name__ == "__main__":
    unittest.main()
