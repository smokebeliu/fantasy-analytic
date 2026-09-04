"""Unit and integration tests for the cross-season forecast (development-plan step 14).

The unit tests exercise the pure cross-season helpers in isolation: aggregating
the prior season into role priors, choosing where a player's history comes from
(current season, prior season by the shared identity, or a newcomer prior) and
applying a newcomer prior in place.

The integration test builds a realistic two-season scenario in a real
PostgreSQL database: a finished prior season imported and published through the
quality gate, plus an *active* season that has not started yet (no played
matches) sharing clubs and players by cross-season identity. It then proves the
plan's acceptance criteria for step 14:

* ``fantasy-forecast`` builds a tour-1 forecast for the active season from the
  prior season's data;
* departed players (registered last season, absent this season) are excluded and
  newcomers (no prior history) are present with priors and a no-history flag;
* every forecast row carries the stat source (prior vs current season); and
* the optimizer builds a valid squad for the first tour of the new tournament.

They are skipped automatically when no database is reachable.
"""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import text

from fantasy_analytics.api import create_app
from fantasy_analytics.db import (
    ForecastRepository,
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.features import (
    CURRENT_RATE_DECAY,
    NEWCOMER_P_APPEARANCE,
    NEWCOMER_PRICE_SLOPE,
    NEWCOMER_PRIOR_MATCHES,
    RATE_PRIOR_WINDOW,
    RATE_SHRINK_MATCHES,
    STAT_SOURCE_CURRENT,
    STAT_SOURCE_PRIOR,
    Appearance,
    ClubMatch,
    Fixture,
    PriorContext,
    PriorPlayer,
    RolePrior,
    _apply_newcomer_prior,
    _build_row,
    _league_role_priors,
    _resolve_history_source,
    _role_priors,
    build_feature_dataset,
    newcomer_appearance_prior,
    rate_prior_scale,
    shrunk_per90,
)
from fantasy_analytics.forecast import (
    MODEL_EVENT,
    forecast_event_model,
    run_forecast,
)
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.optimizer import build_squad_optimization, validate_squad
from fantasy_analytics.quality import run_quality_checks
from fantasy_analytics.read_repository import ReadRepository

from test_ingestion import FakeClient
from test_optimizer import _rules_from_report
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


def _appearance(match_id: int, *, minutes: int, goals: int = 0, assists: int = 0,
                saves: int = 0, recoveries: int = 0, yellows: int = 0) -> Appearance:
    return Appearance(
        match_id=match_id,
        scheduled_at=datetime(2025, 7, match_id, tzinfo=timezone.utc),
        minutes=minutes,
        points=0,
        goals=goals,
        assists=assists,
        saves=saves,
        ball_recoveries=recoveries,
        yellow_cards=yellows,
    )


class RolePriorsTest(unittest.TestCase):
    def test_pools_rates_per_role_and_minutes(self) -> None:
        appearances = {
            1: [
                _appearance(1, minutes=90, goals=1, assists=0),
                _appearance(2, minutes=90, goals=2, assists=1),
            ],
            2: [_appearance(3, minutes=45, saves=3)],
        }
        role_by_ps = {1: "FORWARD", 2: "GOALKEEPER"}
        priors = _role_priors(appearances, role_by_ps)

        forward = priors["FORWARD"]
        # 3 goals over 180 minutes -> 1.5 per 90; mean minutes 90.
        self.assertEqual(1.5, forward.goals_per90)
        self.assertEqual(0.5, forward.assists_per90)
        self.assertEqual(90.0, forward.mean_minutes)

        keeper = priors["GOALKEEPER"]
        # 3 saves over 45 minutes -> 6 per 90; mean minutes 45.
        self.assertEqual(6.0, keeper.saves_per90)
        self.assertEqual(45.0, keeper.mean_minutes)

    def test_league_priors_pool_both_seasons_before_the_cutoff(self) -> None:
        cutoff = datetime(2025, 7, 3, tzinfo=timezone.utc)
        # Match 1 and 2 are before the cutoff; match 3 is not, and match 2 is
        # the tour being predicted, so only match 1 may count.
        current = {
            1: [
                _appearance(1, minutes=90, goals=1),
                _appearance(2, minutes=90, goals=5),
                _appearance(3, minutes=90, goals=5),
            ]
        }
        prior = PriorContext(
            run_id=1,
            season_id=1,
            appearances={7: [_appearance(1, minutes=90, goals=1)]},
            club_matches={},
            by_player_id={7: PriorPlayer(player_season_id=7, club_id=1, role="FORWARD")},
            role_priors={},
        )
        priors = _league_role_priors(
            current, {1: "FORWARD"}, prior, cutoff=cutoff, exclude_match_ids=frozenset({2})
        )
        # 2 goals over 180 minutes, one from each season.
        self.assertEqual(1.0, priors["FORWARD"].goals_per90)
        self.assertEqual(90.0, priors["FORWARD"].mean_minutes)

    def test_rate_prior_scale_caps_a_long_season(self) -> None:
        # A short prior season is admitted whole (at its weight); a long one is
        # cut down to the window first.
        self.assertEqual(1.0, rate_prior_scale(5, 1.0, window=8))
        self.assertEqual(0.25, rate_prior_scale(32, 1.0, window=8))
        self.assertEqual(0.125, rate_prior_scale(32, 0.5, window=8))
        self.assertEqual(0.0, rate_prior_scale(0, 1.0))
        self.assertEqual(0.0, rate_prior_scale(10, 0.0))

    def test_shrunk_per90_moves_from_the_prior_to_the_sample(self) -> None:
        # No minutes: the prior decides. Many minutes: the sample decides.
        self.assertEqual(0.5, shrunk_per90(0, 0, 0.5, pseudo_matches=3))
        self.assertAlmostEqual(
            1.0, shrunk_per90(1000, 90_000, 0.5, pseudo_matches=3), places=2
        )
        # One goal in one match against a 0.5 prior with three pseudo-matches:
        # (1 + 1.5) / 4 matches.
        self.assertAlmostEqual(0.625, shrunk_per90(1, 90, 0.5, pseudo_matches=3))
        # No pseudo-count reproduces the plain per-90 rate.
        self.assertEqual(1.0, shrunk_per90(1, 90, 0.5, pseudo_matches=0))

    def test_role_without_appearances_is_absent(self) -> None:
        priors = _role_priors({1: [_appearance(1, minutes=90)]}, {1: "DEFENDER"})
        self.assertIn("DEFENDER", priors)
        self.assertNotIn("FORWARD", priors)


class ResolveHistorySourceTest(unittest.TestCase):
    def _player(self, **over) -> dict:
        player = {"player_season_id": 10, "player_id": 100, "role": "FORWARD"}
        player.update(over)
        return player

    def _prior(self, *, with_history: bool) -> PriorContext:
        prior_ps = 500
        return PriorContext(
            run_id=1,
            season_id=1,
            appearances={prior_ps: [_appearance(1, minutes=80, goals=1)]}
            if with_history
            else {},
            club_matches={},
            by_player_id={100: PriorPlayer(prior_ps, 7, "FORWARD")},
            role_priors={"FORWARD": RolePrior(1.0, 0.5, 0.0, 2.0, 0.3, 70.0)},
        )

    def test_current_season_history_without_a_prior_run(self) -> None:
        source = _resolve_history_source(
            self._player(),
            3,
            appearances={10: [_appearance(1, minutes=90)]},
            current_club_matches={3: []},
            prior=None,
        )
        self.assertFalse(source["is_newcomer"])
        self.assertEqual(1, len(source["appearances"]))
        self.assertEqual([], source["prior_appearances"])

    def test_both_seasons_are_returned_when_both_exist(self) -> None:
        # The two halves come back side by side; which one dominates is decided
        # later by the prior weight, not here.
        source = _resolve_history_source(
            self._player(),
            3,
            appearances={10: [_appearance(9, minutes=70)]},
            current_club_matches={3: []},
            prior=self._prior(with_history=True),
        )
        self.assertFalse(source["is_newcomer"])
        self.assertEqual(1, len(source["appearances"]))
        self.assertEqual(1, len(source["prior_appearances"]))

    def test_prior_season_history_is_found_by_the_shared_identity(self) -> None:
        source = _resolve_history_source(
            self._player(),
            3,
            appearances={},
            current_club_matches={},
            prior=self._prior(with_history=True),
        )
        self.assertFalse(source["is_newcomer"])
        self.assertEqual(1, len(source["prior_appearances"]))
        self.assertIsNone(source["newcomer_prior"])

    def test_newcomer_when_neither_season_knows_the_player(self) -> None:
        # A player id the prior season never saw -> newcomer with role priors.
        source = _resolve_history_source(
            self._player(player_id=999),
            3,
            appearances={},
            current_club_matches={},
            prior=self._prior(with_history=True),
        )
        self.assertTrue(source["is_newcomer"])
        self.assertEqual([], source["appearances"])
        self.assertEqual([], source["prior_appearances"])
        self.assertIsNotNone(source["newcomer_prior"])


class BlendedHistoryRowTest(unittest.TestCase):
    """The two seasons meet here: what each one is worth, and when."""

    CUTOFF = datetime(2027, 8, 1, tzinfo=timezone.utc)
    PRIOR_END = datetime(2026, 5, 1, tzinfo=timezone.utc)
    CURRENT_END = datetime(2027, 7, 25, tzinfo=timezone.utc)
    FIXTURE = Fixture(
        match_id=9000,
        scheduled_at=datetime(2027, 8, 15, tzinfo=timezone.utc),
        club_id=1,
        opponent_club_id=2,
        is_home=True,
    )
    LEAGUE = {
        "home_attack": 1.4,
        "home_defense": 1.1,
        "away_attack": 1.1,
        "away_defense": 1.4,
    }

    def _season(
        self,
        count: int,
        *,
        ends: datetime,
        played: int | None = None,
        goals: int = 0,
        points: int = 5,
    ) -> tuple[list[Appearance], list[ClubMatch]]:
        """A club's matches plus the player's appearances in the oldest ``played``."""
        played = count if played is None else played
        matches: list[ClubMatch] = []
        appearances: list[Appearance] = []
        for index in range(count):
            # Index 0 is the most recent match, so the ones the player misses
            # are the freshest — a run-in spent injured or rested.
            when = ends - timedelta(days=7 * index)
            match_id = ends.year * 1000 + index
            matches.append(ClubMatch(match_id, when, index % 2 == 0, 1, 1))
            if index >= count - played:
                appearances.append(
                    Appearance(
                        match_id=match_id,
                        scheduled_at=when,
                        minutes=90,
                        points=points,
                        goals=goals,
                        assists=0,
                    )
                )
        return appearances, matches

    def _row(
        self, *, current: tuple, prior: tuple, role_prior: RolePrior | None = None
    ) -> dict:
        current_appearances, current_matches = current
        prior_appearances, prior_matches = prior
        return _build_row(
            role_prior=role_prior,
            player={
                "player_season_id": 1,
                "player_id": 1,
                "fantasy_player_id": "1",
                "role": "FORWARD",
                "player_name": "Звезда",
            },
            fixtures=[self.FIXTURE],
            club_name="Club A",
            opponent_names={2: "Club B"},
            cutoff=self.CUTOFF,
            target_match_ids=frozenset(),
            appearances=current_appearances,
            club_matches=current_matches,
            prior_appearances=prior_appearances,
            prior_club_matches=prior_matches,
            snapshot={
                "availability_status": "UNKNOWN",
                "status_description": None,
                "price": 11.5,
                "selected_by": None,
                "form": None,
            },
            strengths={},
            league=self.LEAGUE,
        )

    def test_a_star_who_missed_the_run_in_is_not_written_off(self) -> None:
        # The league's best players routinely miss the last matches of a season
        # once the table is settled. Judging the new season on that window alone
        # forecast them at exactly zero points — for every tour, all season.
        prior = self._season(20, ends=self.PRIOR_END, played=16, goals=1, points=9)
        row = self._row(current=([], []), prior=prior)

        self.assertEqual(0.8, row["p_appearance"])
        self.assertEqual(STAT_SOURCE_PRIOR, row["stat_source"])
        self.assertGreater(row["expected_minutes"], 60)
        # A goal a game last season is still read as a striker, less the
        # shrinkage every rate gets towards the role average (zero here, since
        # no role prior is given): eight window matches against eight
        # pseudo-matches of nothing leave exactly half the rate.
        self.assertGreater(row["goals_per90"], 0.45)
        self.assertGreater(forecast_event_model(row)["expected_points"], 3.0)

    def test_last_season_still_counts_once_the_new_one_starts(self) -> None:
        # The plan's requirement: last season keeps informing later tours, at a
        # weight that falls with every match of the new one.
        prior = self._season(20, ends=self.PRIOR_END, goals=1)
        weights = []
        for played in (1, 3, 6, 12, 24):
            current = self._season(played, ends=self.CURRENT_END, goals=0)
            row = self._row(current=current, prior=prior)
            weights.append(row["goals_per90"])
            self.assertGreater(row["goals_per90"], 0.0)

        self.assertEqual(weights, sorted(weights, reverse=True))
        # A single match of the new season leaves last season most of the
        # story (the shrinkage pseudo-matches take the rest); two dozen
        # decide it.
        self.assertGreater(weights[0], 0.4)
        self.assertLess(weights[-1], 0.1)

    def test_last_season_cannot_outvote_this_one_by_being_longer(self) -> None:
        # The 1.6.0 fix: a 38-match season used to bring 38 matches of evidence
        # to the pool, so eight blank matches of the new season were outvoted
        # three to one by last year's goals. Capped at RATE_PRIOR_WINDOW
        # matches, a long prior season and a short one say the same thing.
        current = self._season(8, ends=self.CURRENT_END, goals=0)
        long_prior = self._season(38, ends=self.PRIOR_END, goals=1)
        short_prior = self._season(
            int(RATE_PRIOR_WINDOW), ends=self.PRIOR_END, goals=1
        )
        long_rate = self._row(current=current, prior=long_prior)["goals_per90"]
        short_rate = self._row(current=current, prior=short_prior)["goals_per90"]
        self.assertAlmostEqual(long_rate, short_rate, places=3)
        # Eight blank matches now outweigh last season: well under half the
        # goal-a-game rate last season claimed.
        self.assertLess(long_rate, 0.4)
        # ... and the reported scale says how much of last season was used.
        row = self._row(current=current, prior=long_prior)
        self.assertLess(row["prior_season_scale"], row["prior_season_weight"])
        self.assertAlmostEqual(
            row["prior_season_scale"],
            row["prior_season_weight"] * RATE_PRIOR_WINDOW / 38,
            places=5,
        )

    def test_rates_are_shrunk_towards_the_role_average(self) -> None:
        # Two goals in two matches is not a goal-a-game striker: with the role
        # average in the pool as pseudo-matches, the rate lands between the
        # sample and the average, and moves towards the sample as it grows.
        anchor = RolePrior(0.3, 0.2, 0.0, 3.0, 0.2, 80.0)
        two = self._season(2, ends=self.CURRENT_END, goals=1)
        twenty = self._season(20, ends=self.CURRENT_END, goals=1)
        rate_two = self._row(current=two, prior=([], []), role_prior=anchor)["goals_per90"]
        rate_twenty = self._row(current=twenty, prior=([], []), role_prior=anchor)["goals_per90"]
        self.assertGreater(rate_two, 0.3)
        self.assertLess(rate_two, 0.7)
        self.assertGreater(rate_twenty, rate_two)
        # The current season is recency-weighted: the older of the two
        # matches enters at CURRENT_RATE_DECAY.
        decayed = 1.0 + CURRENT_RATE_DECAY
        self.assertAlmostEqual(
            rate_two,
            shrunk_per90(decayed, 90 * decayed, 0.3, pseudo_matches=RATE_SHRINK_MATCHES),
            places=4,
        )

    def test_the_current_season_is_never_discounted(self) -> None:
        # "Current results always take priority": with the same number of
        # matches on both sides the new season already outweighs the old one.
        prior = self._season(10, ends=self.PRIOR_END, goals=2)
        current = self._season(10, ends=self.CURRENT_END, goals=0)
        row = self._row(current=current, prior=prior)
        self.assertLess(row["goals_per90"], 2.0 * 0.25)
        self.assertEqual(STAT_SOURCE_CURRENT, row["stat_source"])

    def test_a_benched_player_is_not_ever_present(self) -> None:
        # Sports.ru lists every named substitute, so counting matchday rows made
        # a permanent reserve look like a starter on half-length shifts.
        matches = [
            ClubMatch(i, self.CUTOFF - timedelta(days=7 * i), True, 1, 1)
            for i in range(10)
        ]
        bench = [
            Appearance(
                match_id=i,
                scheduled_at=self.CUTOFF - timedelta(days=7 * i),
                minutes=0,
                points=0,
                goals=0,
                assists=0,
            )
            for i in range(10)
        ]
        row = self._row(current=(bench, matches), prior=([], []))
        self.assertEqual(0.0, row["p_appearance"])
        self.assertEqual(0.0, row["expected_minutes"])
        self.assertEqual(0, row["current_appearances"])
        self.assertFalse(row["has_history"])

    def test_recent_absences_weigh_more_than_old_ones(self) -> None:
        # Within the season being played, when a player stopped featuring is the
        # whole question, so the club's latest matches dominate.
        recent_absence = self._row(
            current=self._season(10, ends=self.CURRENT_END, played=6), prior=([], [])
        )
        old_absence_appearances, matches = self._season(10, ends=self.CURRENT_END)
        # Drop the four *oldest* appearances instead of the four newest.
        old_absence = self._row(
            current=(old_absence_appearances[:6], matches), prior=([], [])
        )
        self.assertLess(recent_absence["p_appearance"], old_absence["p_appearance"])

    def test_leakage_totals_stay_unweighted_and_current(self) -> None:
        # The backtest's audit recomputes these, so they must be this season's
        # raw numbers however much of last season is blended in.
        prior = self._season(20, ends=self.PRIOR_END, points=9)
        current = self._season(2, ends=self.CURRENT_END, points=4)
        row = self._row(current=current, prior=prior)
        self.assertEqual(2, row["current_appearances"])
        self.assertEqual(8, row["current_points"])
        self.assertEqual(180, row["current_minutes"])
        self.assertGreater(row["total_points"], row["current_points"])


class ApplyNewcomerPriorTest(unittest.TestCase):
    def test_overrides_rates_and_probabilities(self) -> None:
        row = {
            "is_available": True,
            "p_appearance": 0.0,
            "expected_minutes": 0.0,
            "appearance_share": 0.0,
            "start_share": 0.0,
            "goals_per90": 0.0,
            "assists_per90": 0.0,
            "saves_per90": 0.0,
            "recoveries_per90": 0.0,
            "yellows_per90": 0.0,
        }
        prior = RolePrior(
            goals_per90=1.0,
            assists_per90=0.5,
            saves_per90=0.0,
            recoveries_per90=2.0,
            yellows_per90=0.3,
            mean_minutes=90.0,
        )
        _apply_newcomer_prior(row, prior, is_available=True)

        self.assertEqual(NEWCOMER_P_APPEARANCE, row["p_appearance"])
        self.assertEqual(round(NEWCOMER_P_APPEARANCE * 90.0, 2), row["expected_minutes"])
        # Offensive rates are discounted; the yellow-card penalty is not.
        self.assertEqual(0.7, row["goals_per90"])
        self.assertEqual(0.3, row["yellows_per90"])

    def test_priced_appearance_prior(self) -> None:
        # At the median price the role base applies; a unit above or below
        # moves it by the slope; keepers start much lower; the range is clamped.
        self.assertEqual(NEWCOMER_P_APPEARANCE, newcomer_appearance_prior("FORWARD", 6.0, 6.0))
        self.assertAlmostEqual(
            NEWCOMER_P_APPEARANCE + NEWCOMER_PRICE_SLOPE,
            newcomer_appearance_prior("MIDFIELDER", 7.0, 6.0),
            places=4,
        )
        self.assertLess(
            newcomer_appearance_prior("DEFENDER", 4.5, 6.0), NEWCOMER_P_APPEARANCE
        )
        self.assertLess(
            newcomer_appearance_prior("GOALKEEPER", 5.0, 5.0), NEWCOMER_P_APPEARANCE
        )
        self.assertEqual(0.65, newcomer_appearance_prior("FORWARD", 20.0, 6.0))
        self.assertEqual(0.03, newcomer_appearance_prior("FORWARD", 1.0, 6.0))
        # No price, or no median to compare with: the base.
        self.assertEqual(NEWCOMER_P_APPEARANCE, newcomer_appearance_prior("FORWARD", None, 6.0))

    def test_a_missed_club_match_cuts_the_assumption_fast(self) -> None:
        # The assumption is worth NEWCOMER_PRIOR_MATCHES matches of evidence,
        # so a club match the newcomer sat out halves it and two quarter it.
        def p_after(missed: int) -> float:
            row = {
                "is_available": True, "p_appearance": 0.0, "expected_minutes": 0.0,
                "appearance_share": 0.0, "start_share": 0.0, "goals_per90": 0.0,
                "assists_per90": 0.0, "saves_per90": 0.0, "recoveries_per90": 0.0,
                "yellows_per90": 0.0,
            }
            _apply_newcomer_prior(
                row, RolePrior(1.0, 0.5, 0.0, 2.0, 0.3, 90.0), is_available=True,
                prior_share=NEWCOMER_PRIOR_MATCHES, current_share=float(missed),
                p_base=0.4,
            )
            return row["p_appearance"]

        self.assertEqual(0.4, p_after(0))
        self.assertAlmostEqual(0.2, p_after(1), places=4)
        self.assertLess(p_after(2), 0.15)

    def test_unavailable_newcomer_has_zero_appearance(self) -> None:
        row = {
            "is_available": False,
            "p_appearance": 0.0,
            "expected_minutes": 0.0,
            "appearance_share": 0.0,
            "start_share": 0.0,
            "goals_per90": 0.0,
            "assists_per90": 0.0,
            "saves_per90": 0.0,
            "recoveries_per90": 0.0,
            "yellows_per90": 0.0,
        }
        prior = RolePrior(1.0, 0.5, 0.0, 2.0, 0.3, 90.0)
        _apply_newcomer_prior(row, prior, is_available=False)
        self.assertEqual(0.0, row["p_appearance"])
        self.assertEqual(0.0, row["expected_minutes"])


@requires_database
class CrossSeasonIntegrationTest(unittest.TestCase):
    # Active-season fantasy ids (distinct from the prior-season ones).
    ACTIVE_FORWARD = "311"
    ACTIVE_KEEPER = "322"
    ACTIVE_NEWCOMER = "399"
    ACTIVE_TOUR = "1801"

    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)
        self.prior_run_id = self._import_and_publish_prior_season()
        self._add_departed_prior_player()
        self._build_active_season()

    # -- helpers -------------------------------------------------------------
    def _exec(self, sql: str, **params):
        with self.engine.begin() as connection:
            return connection.execute(text(sql), params)

    def _scalar(self, sql: str, **params):
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar_one()

    def _import_and_publish_prior_season(self) -> int:
        report = run_ingestion(
            FakeClient(_consistent_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        run_quality_checks(self.session_factory, run_id=report["run_id"])
        return report["run_id"]

    def _add_departed_prior_player(self) -> None:
        """A player registered only in the prior season (leaves before the new one)."""
        prior_season = self._scalar("SELECT id FROM seasons WHERE fantasy_season_id = '59'")
        club_a = self._scalar(
            "SELECT id FROM season_clubs WHERE season_id = :s AND fantasy_team_id = '10'",
            s=prior_season,
        )
        player = self._exec(
            "INSERT INTO players (stat_player_id, canonical_name) "
            "VALUES ('p_departed', 'Ушедший') RETURNING id"
        ).scalar_one()
        self._exec(
            "INSERT INTO player_seasons "
            "(season_id, player_id, fantasy_player_id, role, current_season_club_id) "
            "VALUES (:s, :p, '590', 'FORWARD', :sc)",
            s=prior_season,
            p=player,
            sc=club_a,
        )

    def _build_active_season(self) -> None:
        comp_id = self._scalar(
            "SELECT competition_id FROM seasons WHERE fantasy_season_id = '59'"
        )
        prior_season = self._scalar(
            "SELECT id FROM seasons WHERE fantasy_season_id = '59'"
        )
        club_a = self._scalar(
            "SELECT club_id FROM season_clubs WHERE season_id = :s AND fantasy_team_id = '10'",
            s=prior_season,
        )
        club_b = self._scalar(
            "SELECT club_id FROM season_clubs WHERE season_id = :s AND fantasy_team_id = '20'",
            s=prior_season,
        )
        pid_one = self._scalar("SELECT id FROM players WHERE stat_player_id = 'p_one'")
        pid_two = self._scalar("SELECT id FROM players WHERE stat_player_id = 'p_two'")

        season = self._exec(
            """
            INSERT INTO seasons
                (competition_id, fantasy_season_id, stat_season_id, name,
                 is_active, starts_at, ends_at)
            VALUES (:c, '75', 'rfpl_26-27', '2026/2027', true,
                    '2026-07-01T00:00:00Z', '2027-05-30T00:00:00Z')
            RETURNING id
            """,
            c=comp_id,
        ).scalar_one()
        self.active_season = season

        two_player_roster = json.dumps(
            [
                {"role": "GOALKEEPER", "minCount": 1, "maxCount": 1},
                {"role": "FORWARD", "minCount": 1, "maxCount": 1},
            ]
        )
        self._exec(
            """
            INSERT INTO season_rules
                (season_id, rules_html, total_budget, total_players,
                 starting_players, full_roster_constraints,
                 starting_roster_constraints)
            VALUES (:s, '<p/>', 100, 2, 2, :roster, :roster)
            """,
            s=season,
            roster=two_player_roster,
        )

        sc_a = self._exec(
            "INSERT INTO season_clubs (season_id, club_id, fantasy_team_id, display_name) "
            "VALUES (:s, :c, '10', 'Клуб A') RETURNING id",
            s=season,
            c=club_a,
        ).scalar_one()
        sc_b = self._exec(
            "INSERT INTO season_clubs (season_id, club_id, fantasy_team_id, display_name) "
            "VALUES (:s, :c, '20', 'Клуб B') RETURNING id",
            s=season,
            c=club_b,
        ).scalar_one()

        # A returning forward and keeper (shared player_id), plus a brand-new
        # forward with no prior history at all.
        ps_forward = self._exec(
            "INSERT INTO player_seasons "
            "(season_id, player_id, fantasy_player_id, role, current_season_club_id) "
            "VALUES (:s, :p, :fid, 'FORWARD', :sc) RETURNING id",
            s=season,
            p=pid_one,
            fid=self.ACTIVE_FORWARD,
            sc=sc_a,
        ).scalar_one()
        ps_keeper = self._exec(
            "INSERT INTO player_seasons "
            "(season_id, player_id, fantasy_player_id, role, current_season_club_id) "
            "VALUES (:s, :p, :fid, 'GOALKEEPER', :sc) RETURNING id",
            s=season,
            p=pid_two,
            fid=self.ACTIVE_KEEPER,
            sc=sc_b,
        ).scalar_one()
        new_player = self._exec(
            "INSERT INTO players (stat_player_id, canonical_name) "
            "VALUES ('p_new', 'Новичок') RETURNING id"
        ).scalar_one()
        ps_new = self._exec(
            "INSERT INTO player_seasons "
            "(season_id, player_id, fantasy_player_id, role, current_season_club_id) "
            "VALUES (:s, :p, :fid, 'FORWARD', :sc) RETURNING id",
            s=season,
            p=new_player,
            fid=self.ACTIVE_NEWCOMER,
            sc=sc_a,
        ).scalar_one()

        tour = self._exec(
            """
            INSERT INTO fantasy_tours
                (season_id, fantasy_tour_id, name, status, starts_at,
                 transfers_deadline_at, total_transfers, max_same_team_players)
            VALUES (:s, :tour, '1 тур', 'OPENED', '2026-08-10T16:00:00Z',
                    '2026-08-09T10:00:00Z', 3, 3)
            RETURNING id
            """,
            s=season,
            tour=self.ACTIVE_TOUR,
        ).scalar_one()
        self._exec(
            """
            INSERT INTO matches
                (season_id, tour_id, stat_match_id, scheduled_at,
                 home_club_id, away_club_id, home_score, away_score)
            VALUES (:s, :t, '950001', '2026-08-10T16:00:00Z', :ca, :cb, NULL, NULL)
            """,
            s=season,
            t=tour,
            ca=club_a,
            cb=club_b,
        )

        # An active, published run for the new season with no played matches yet.
        run = self._exec(
            """
            INSERT INTO ingestion_runs
                (trigger_type, tournament_slug, status, season_id, is_active,
                 quality_checked_at, finished_at)
            VALUES ('manual', 'russia', 'succeeded', :s, true, now(), now())
            RETURNING id
            """,
            s=season,
        ).scalar_one()
        self.active_run = run

        for player_season, season_club, price in (
            (ps_forward, sc_a, 9),
            (ps_keeper, sc_b, 6),
            (ps_new, sc_a, 5),
        ):
            self._exec(
                """
                INSERT INTO fantasy_player_snapshots
                    (player_season_id, ingestion_run_id, season_club_id, price,
                     availability_status)
                VALUES (:ps, :run, :sc, :price, 'FIT')
                """,
                ps=player_season,
                run=run,
                sc=season_club,
                price=price,
            )

    # -- tests ---------------------------------------------------------------
    def test_a_later_season_is_never_the_prior_of_an_earlier_one(self) -> None:
        # Backtesting 2025/26 with 2026/27 imported used to pick 2026/27 as the
        # "prior" season for the opening tour, because nothing started earlier
        # and the fallback took any other season. That is the future.
        report = build_feature_dataset(
            self.session_factory, season_ref="59", tour_ref="1772"
        )
        self.assertIsNone(report["prior_run_id"])
        self.assertFalse(report["cross_season"])
        self.assertEqual(0.0, report["prior_season_weight"])

    def test_features_source_from_prior_season(self) -> None:
        report = build_feature_dataset(
            self.session_factory, season_ref="75", tour_ref=self.ACTIVE_TOUR
        )
        self.assertTrue(report["cross_season"])
        self.assertEqual(self.prior_run_id, report["prior_run_id"])
        self.assertEqual(3, report["counts"]["rows"])  # departed excluded
        self.assertEqual(1, report["counts"]["newcomers"])

        by_fid = {row["fantasy_player_id"]: row for row in report["rows"]}
        self.assertNotIn("590", by_fid)  # departed player is gone

        forward = by_fid[self.ACTIVE_FORWARD]
        self.assertEqual(STAT_SOURCE_PRIOR, forward["stat_source"])
        self.assertTrue(forward["has_history"])
        self.assertFalse(forward["is_newcomer"])
        # Prior-season history flows through: one appearance, real per-90 rates.
        self.assertEqual(1, forward["total_appearances"])
        self.assertGreater(forward["goals_per90"], 0.0)
        self.assertIsNone(forward["rest_days"])  # nulled in cross-season mode

        newcomer = by_fid[self.ACTIVE_NEWCOMER]
        self.assertEqual(STAT_SOURCE_PRIOR, newcomer["stat_source"])
        self.assertTrue(newcomer["is_newcomer"])
        self.assertFalse(newcomer["has_history"])
        # Priced at 5 against a forward median well above it, the newcomer is
        # assumed to play far less often than the flat base.
        forward_prices = sorted(
            row["price"] for row in report["rows"] if row["role"] == "FORWARD"
        )
        median = forward_prices[len(forward_prices) // 2]
        self.assertEqual(
            newcomer_appearance_prior("FORWARD", 5.0, median),
            newcomer["p_appearance"],
        )
        self.assertLess(newcomer["p_appearance"], NEWCOMER_P_APPEARANCE)

    def test_forecast_labels_source_and_persists_it(self) -> None:
        report = run_forecast(
            self.session_factory,
            season_ref="75",
            tour_ref=self.ACTIVE_TOUR,
            persist=True,
        )
        self.assertTrue(report["cross_season"])
        self.assertEqual(self.active_run, report["run_id"])
        self.assertGreater(report["persisted"], 0)

        event_rows = {
            row["fantasy_player_id"]: row
            for row in report["rows"]
            if row["model_name"] == MODEL_EVENT
        }
        forward = event_rows[self.ACTIVE_FORWARD]
        self.assertEqual(STAT_SOURCE_PRIOR, forward["stat_source"])
        self.assertGreater(forward["expected_points"], 0.0)

        newcomer = event_rows[self.ACTIVE_NEWCOMER]
        self.assertTrue(newcomer["is_newcomer"])
        # A newcomer is present and scored, but the source is prior-season priors.
        self.assertEqual(STAT_SOURCE_PRIOR, newcomer["stat_source"])

        # The source is persisted so the read API and frontend can separate it.
        with self.session_factory() as session:
            repo = ForecastRepository(session)
            stored = repo.list_forecasts(
                run_id=self.active_run,
                tour_id=report["tour"]["tour_id"],
                model_name=MODEL_EVENT,
            )
        self.assertTrue(stored)
        self.assertTrue(all(row.stat_source == STAT_SOURCE_PRIOR for row in stored))
        self.assertTrue(any(row.has_history for row in stored))
        self.assertTrue(any(row.has_history is False for row in stored))

    def test_optimizer_builds_valid_first_tour_squad(self) -> None:
        report = build_squad_optimization(
            self.session_factory, season_ref="75", tour_ref=self.ACTIVE_TOUR
        )
        self.assertTrue(report["valid"])
        self.assertEqual("2026/2027", report["season"]["name"])
        solution = report["solution"]
        self.assertEqual(2, len(solution["squad"]))
        self.assertEqual({"GOALKEEPER", "FORWARD"}, {p["role"] for p in solution["squad"]})
        self.assertEqual([], validate_squad(solution, _rules_from_report(report)))
        # Every picked player is transparently sourced from the prior season.
        self.assertTrue(
            all(p["stat_source"] == STAT_SOURCE_PRIOR for p in solution["squad"])
        )

    # -- last season's numbers on this season's players -----------------------
    def test_returning_player_carries_his_previous_season(self) -> None:
        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
                ensure_forecasts=lambda *a, **kw: 0,
            )
        )
        items = client.get(f"/players?season_id={self.active_season}").json()["items"]
        by_fid = {item["fantasy_player_id"]: item for item in items}

        forward = by_fid[self.ACTIVE_FORWARD]["prior_season"]
        self.assertEqual("2025/2026", forward["season_name"])
        # The prior season had one played match with the player on the pitch.
        self.assertEqual(1, forward["matches"])
        self.assertGreater(forward["minutes"], 0)
        self.assertIsNotNone(forward["points"])
        self.assertIsNotNone(forward["club_name"])

    def test_newcomer_has_no_previous_season(self) -> None:
        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
                ensure_forecasts=lambda *a, **kw: 0,
            )
        )
        items = client.get(f"/players?season_id={self.active_season}").json()["items"]
        by_fid = {item["fantasy_player_id"]: item for item in items}
        self.assertIsNone(by_fid[self.ACTIVE_NEWCOMER]["prior_season"])

    def test_player_card_repeats_the_previous_season(self) -> None:
        # The card is what a manager opens to judge a signing, so it must carry
        # the same block as the list rather than only this season's empty columns.
        with self.session_factory() as session:
            player_season_id = session.execute(
                text(
                    "SELECT id FROM player_seasons "
                    "WHERE season_id = :s AND fantasy_player_id = :fid"
                ),
                {"s": self.active_season, "fid": self.ACTIVE_FORWARD},
            ).scalar_one()

        client = TestClient(
            create_app(
                session_factory=self.session_factory,
                spawn_worker=lambda job_id: None,
                ensure_forecasts=lambda *a, **kw: 0,
            )
        )
        card = client.get(f"/players/{player_season_id}").json()
        self.assertIsNotNone(card["prior_season"])
        self.assertEqual("2025/2026", card["prior_season"]["season_name"])

    def test_prior_season_players_have_no_earlier_season(self) -> None:
        # The oldest imported season has nothing behind it; asking must yield an
        # empty mapping rather than falling back to a later season.
        prior_season_id = self._scalar(
            "SELECT id FROM seasons WHERE fantasy_season_id = '59'"
        )
        with self.session_factory() as session:
            repo = ReadRepository(session)
            player_ids = list(
                session.execute(
                    text("SELECT id FROM player_seasons WHERE season_id = :s"),
                    {"s": prior_season_id},
                )
                .scalars()
                .all()
            )
            self.assertEqual({}, repo.prior_season_stats(prior_season_id, player_ids))


if __name__ == "__main__":
    unittest.main()
