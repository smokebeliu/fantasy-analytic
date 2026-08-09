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

from sqlalchemy import text

from fantasy_analytics.db import (
    ForecastRepository,
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.features import (
    NEWCOMER_P_APPEARANCE,
    STAT_SOURCE_CURRENT,
    STAT_SOURCE_PRIOR,
    Appearance,
    PriorContext,
    PriorPlayer,
    RolePrior,
    _apply_newcomer_prior,
    _resolve_history_source,
    _role_priors,
    build_feature_dataset,
)
from fantasy_analytics.forecast import MODEL_EVENT, run_forecast
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.optimizer import build_squad_optimization, validate_squad
from fantasy_analytics.quality import run_quality_checks

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
    from datetime import datetime, timezone

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

    def test_current_season_when_not_cross_season(self) -> None:
        source = _resolve_history_source(
            self._player(),
            3,
            cross_season=False,
            appearances={10: [_appearance(1, minutes=90)]},
            current_club_matches={3: []},
            prior=None,
        )
        self.assertEqual(STAT_SOURCE_CURRENT, source["stat_source"])
        self.assertFalse(source["is_newcomer"])
        self.assertEqual(1, len(source["appearances"]))

    def test_prior_season_when_cross_and_history_exists(self) -> None:
        source = _resolve_history_source(
            self._player(),
            3,
            cross_season=True,
            appearances={},
            current_club_matches={},
            prior=self._prior(with_history=True),
        )
        self.assertEqual(STAT_SOURCE_PRIOR, source["stat_source"])
        self.assertFalse(source["is_newcomer"])
        self.assertEqual(1, len(source["appearances"]))
        self.assertIsNone(source["newcomer_prior"])

    def test_newcomer_when_cross_and_no_prior_history(self) -> None:
        # A player id the prior season never saw -> newcomer with role priors.
        source = _resolve_history_source(
            self._player(player_id=999),
            3,
            cross_season=True,
            appearances={},
            current_club_matches={},
            prior=self._prior(with_history=True),
        )
        self.assertEqual(STAT_SOURCE_PRIOR, source["stat_source"])
        self.assertTrue(source["is_newcomer"])
        self.assertEqual([], source["appearances"])
        self.assertIsNotNone(source["newcomer_prior"])


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
        self.assertEqual(NEWCOMER_P_APPEARANCE, newcomer["p_appearance"])

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


if __name__ == "__main__":
    unittest.main()
