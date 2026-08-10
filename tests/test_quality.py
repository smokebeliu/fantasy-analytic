"""Unit and integration tests for the data-quality gate (step 4).

The unit tests exercise the pure result/issue helpers. The integration tests
import a small, internally consistent synthetic season into a real PostgreSQL
database and then inject the three fixture scenarios required by the plan
(missing ingestion page, duplicated fixture id and a changed result) to prove
that each is detected, that blocking issues keep a snapshot inactive and that
the 72-hour adjustment window downgrades a stale mismatch to a warning.

A further set covers the discrepancies that only appear outside the RPL (step 22)
and must *not* withhold a snapshot: history missing for a single player because
Sports.ru returned none, a provider season wider than the fantasy calendar
(play-offs, Champions League qualifying) and a provider aggregate whose own
results do not add up to its own match count. They are skipped automatically when
no database is reachable.
"""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
)
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.quality import (
    BLOCKING,
    STAT_ADJUSTMENT_WINDOW,
    WARNING,
    CheckResult,
    QualityError,
    QualityIssue,
    run_quality_checks,
)

# Reuse the fetch fixture and fake client from the ingestion tests; the tests
# directory is on sys.path during unittest discovery.
from test_ingestion import FakeClient, _build_fixture, _game_stat

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get(
    "DATABASE_URL"
)

# The single history match for every player uses these stats, so a consistent
# season aggregate must equal them for reconciliation to pass.
_MATCH_MINUTES = 78
_MATCH_POINTS = 12

# Every non-nullable statistic on both player grains, and the two values a padded
# player is given; the rest stay zero.
_STAT_COLUMNS = (
    "points",
    "goals",
    "assists",
    "saves",
    "penalties_missed",
    "penalties_post",
    "penalties_target",
    "penalties_saved",
    "field_minutes",
    "yellow_cards",
    "red_cards",
    "goals_conceded",
    "penalty_goals_conceded",
    "penalties_faced",
    "penalty_conceded",
    "own_goals",
    "ball_recoveries",
)
_PAD_STATS = {"points": 3, "field_minutes": 45}


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


def _consistent_fixture() -> dict:
    """A fixture whose season aggregates equal the single-match totals."""
    fixture = _build_fixture()
    players = fixture["players"]["data"]["fantasyQueries"]["players"]["list"]
    for player in players:
        player["gameStat"] = _game_stat(_MATCH_MINUTES, _MATCH_POINTS)
    return fixture


class DataclassTest(unittest.TestCase):
    def test_issue_as_row_round_trips_fields(self) -> None:
        issue = QualityIssue(
            check_name="demo",
            severity=BLOCKING,
            message="boom",
            entity_type="match",
            entity_ref="900001",
            expected="1",
            actual="2",
            details={"k": "v"},
        )
        row = issue.as_row()
        self.assertEqual("demo", row["check_name"])
        self.assertEqual(BLOCKING, row["severity"])
        self.assertEqual("900001", row["entity_ref"])
        self.assertEqual({"k": "v"}, row["details"])

    def test_check_result_status_prefers_blocking(self) -> None:
        result = CheckResult(
            name="demo",
            expected=1,
            actual=2,
            issues=[
                QualityIssue("demo", WARNING, "w"),
                QualityIssue("demo", BLOCKING, "b"),
            ],
        )
        self.assertEqual(BLOCKING, result.status)
        self.assertEqual(1, result.as_dict()["blocking"])
        self.assertEqual(1, result.as_dict()["warnings"])

    def test_check_result_status_ok_when_empty(self) -> None:
        self.assertEqual("ok", CheckResult("demo", 1, 1, []).status)

    def test_adjustment_window_is_72_hours(self) -> None:
        self.assertEqual(timedelta(hours=72), STAT_ADJUSTMENT_WINDOW)


@requires_database
class QualityGateIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)

    def _import(self) -> int:
        report = run_ingestion(
            FakeClient(_consistent_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        return report["run_id"]

    def _pad_players_with_history(self, run_id: int, *, count: int) -> None:
        """Register ``count`` extra players who each played one match.

        The missing-history check weighs the gap against how many players played,
        and the synthetic season has only a handful. Padding it to a realistic
        size is what makes "one player of several hundred" expressible at all.
        """
        # A run only points at its season once the gate has published it, so the
        # season is resolved through the snapshot the import wrote.
        season_id = self._scalar(
            """
            SELECT sc.season_id
            FROM season_clubs sc
            JOIN club_season_stats css ON css.season_club_id = sc.id
            WHERE css.ingestion_run_id = :run
            LIMIT 1
            """,
            run=run_id,
        )
        season_club_id = self._scalar(
            "SELECT id FROM season_clubs WHERE season_id = :s ORDER BY id LIMIT 1",
            s=season_id,
        )
        match_id = self._scalar(
            "SELECT id FROM matches WHERE season_id = :s ORDER BY id LIMIT 1",
            s=season_id,
        )
        tour_id = self._scalar("SELECT tour_id FROM matches WHERE id = :m", m=match_id)

        columns = ", ".join(_STAT_COLUMNS)
        values = ", ".join(str(_PAD_STATS.get(column, 0)) for column in _STAT_COLUMNS)
        self._exec(
            f"""
            WITH new_players AS (
                INSERT INTO players (stat_player_id, canonical_name)
                SELECT 'pad_' || g, 'Запасной ' || g
                FROM generate_series(1, :count) AS g
                RETURNING id
            ), new_seasons AS (
                INSERT INTO player_seasons
                    (season_id, player_id, fantasy_player_id, role,
                     current_season_club_id)
                SELECT :season, id, 'pad' || id, 'MIDFIELDER', :season_club
                FROM new_players
                RETURNING id
            ), totals AS (
                INSERT INTO player_season_stats
                    (player_season_id, ingestion_run_id, {columns})
                SELECT id, :run, {values} FROM new_seasons
                RETURNING player_season_id
            )
            INSERT INTO player_match_stats
                (player_season_id, match_id, tour_id, season_club_id,
                 ingestion_run_id, {columns})
            SELECT player_season_id, :match, :tour, :season_club, :run, {values}
            FROM totals
            """,
            count=count,
            season=season_id,
            season_club=season_club_id,
            match=match_id,
            tour=tour_id,
            run=run_id,
        )

    def _exec(self, sql: str, **params):
        with self.engine.begin() as connection:
            return connection.execute(text(sql), params)

    def _scalar(self, sql: str, **params):
        with self.engine.connect() as connection:
            return connection.execute(text(sql), params).scalar_one()

    def _run_is_active(self, run_id: int) -> bool:
        return bool(
            self._scalar(
                "SELECT is_active FROM ingestion_runs WHERE id = :id", id=run_id
            )
        )

    def test_clean_snapshot_passes_and_becomes_active(self) -> None:
        run_id = self._import()

        report = run_quality_checks(self.session_factory, run_id=run_id)

        self.assertTrue(report["passed"])
        self.assertTrue(report["is_active"])
        self.assertEqual(0, report["counts"]["blocking"])
        self.assertEqual(0, report["counts"]["warnings"])
        self.assertTrue(self._run_is_active(run_id))
        self.assertEqual(
            0,
            self._scalar("SELECT count(*) FROM data_quality_issues"),
        )
        self.assertIsNotNone(
            self._scalar(
                "SELECT season_id FROM ingestion_runs WHERE id = :id", id=run_id
            )
        )
        self.assertEqual(
            report["season_id"],
            self._scalar(
                "SELECT season_id FROM ingestion_runs WHERE id = :id", id=run_id
            ),
        )

    def test_default_evaluates_latest_successful_run(self) -> None:
        run_id = self._import()

        report = run_quality_checks(self.session_factory)

        self.assertEqual(run_id, report["run_id"])

    def test_missing_history_page_blocks_activation(self) -> None:
        good_run = self._import()
        run_quality_checks(self.session_factory, run_id=good_run)
        self.assertTrue(self._run_is_active(good_run))

        # A second import reproduces the snapshot; then a player page is dropped.
        bad_run = self._import()
        deleted = self._exec(
            """
            DELETE FROM player_match_stats
            WHERE player_season_id = (
                SELECT ps.id FROM player_seasons ps
                WHERE ps.fantasy_player_id = '111'
            )
            """
        ).rowcount
        self.assertGreater(deleted, 0)

        report = run_quality_checks(self.session_factory, run_id=bad_run)

        self.assertFalse(report["passed"])
        self.assertFalse(report["is_active"])
        self.assertGreaterEqual(report["counts"]["blocking"], 1)
        # The previously valid snapshot is not superseded by the invalid one.
        self.assertTrue(self._run_is_active(good_run))
        self.assertFalse(self._run_is_active(bad_run))
        checks = {c["name"]: c for c in report["checks"]}
        self.assertEqual(BLOCKING, checks["player_points_reconciliation"]["status"])

    def test_duplicate_fixture_is_blocking(self) -> None:
        run_id = self._import()
        self._exec(
            """
            INSERT INTO matches
                (season_id, tour_id, stat_match_id, scheduled_at,
                 home_club_id, away_club_id, home_score, away_score)
            SELECT season_id, tour_id, '900001-dup', scheduled_at,
                   home_club_id, away_club_id, home_score, away_score
            FROM matches WHERE stat_match_id = '900001'
            """
        )

        report = run_quality_checks(self.session_factory, run_id=run_id)

        checks = {c["name"]: c for c in report["checks"]}
        self.assertEqual(BLOCKING, checks["duplicate_fixtures"]["status"])
        self.assertFalse(report["passed"])
        self.assertFalse(self._run_is_active(run_id))
        self.assertGreaterEqual(
            self._scalar(
                "SELECT count(*) FROM data_quality_issues "
                "WHERE check_name = 'duplicate_fixtures' AND severity = 'blocking'"
            ),
            1,
        )

    def test_stale_result_change_blocks_but_recent_is_warning(self) -> None:
        run_id = self._import()
        # Force a mismatch between the stored aggregate and the match results.
        self._exec(
            """
            UPDATE club_season_stats
            SET goals_scored = goals_scored + 5
            WHERE ingestion_run_id = :run
            """,
            run=run_id,
        )

        # Match is dated 2025-07-18; a far-future "now" puts it well outside the
        # 72h adjustment window, so the mismatch is blocking.
        stale_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        stale = run_quality_checks(
            self.session_factory, run_id=run_id, now=stale_now
        )
        stale_checks = {c["name"]: c for c in stale["checks"]}
        self.assertEqual(
            BLOCKING, stale_checks["club_result_reconciliation"]["status"]
        )
        self.assertFalse(stale["passed"])
        self.assertFalse(self._run_is_active(run_id))

        # Evaluated shortly after kickoff, the same mismatch falls inside the
        # adjustment window and is only a warning, so the snapshot still passes.
        recent_now = datetime(2025, 7, 18, 18, 30, tzinfo=timezone.utc)
        recent = run_quality_checks(
            self.session_factory, run_id=run_id, now=recent_now
        )
        recent_checks = {c["name"]: c for c in recent["checks"]}
        self.assertEqual(
            WARNING, recent_checks["club_result_reconciliation"]["status"]
        )
        self.assertTrue(recent["passed"])
        self.assertTrue(self._run_is_active(run_id))

    def test_isolated_missing_history_warns_and_still_publishes(self) -> None:
        """One player without history is a provider gap, not a dropped page.

        Sports.ru answers some players' history with an empty list while still
        reporting a non-zero total (one Serie A player in 2025/2026). Refusing a
        729-player season over that would be wrong, so the severity follows the
        share of players affected — and here it is far below the threshold.
        """
        run_id = self._import()
        # Enough players with minutes that a single gap stays under 1%.
        self._pad_players_with_history(run_id, count=200)
        deleted = self._exec(
            """
            DELETE FROM player_match_stats
            WHERE player_season_id = (
                SELECT ps.id FROM player_seasons ps
                WHERE ps.fantasy_player_id = '111'
            )
            """
        ).rowcount
        self.assertGreater(deleted, 0)

        report = run_quality_checks(self.session_factory, run_id=run_id)

        checks = {c["name"]: c for c in report["checks"]}
        reconciliation = checks["player_points_reconciliation"]
        self.assertEqual(WARNING, reconciliation["status"])
        self.assertEqual(1, reconciliation["actual"]["missing_history"])
        self.assertFalse(reconciliation["actual"]["missing_history_is_systemic"])
        self.assertTrue(report["passed"])
        self.assertTrue(self._run_is_active(run_id))

    def test_wider_provider_season_warns_and_still_publishes(self) -> None:
        """Play-offs and qualifying are scored outside the fantasy calendar.

        Four Eredivisie clubs report more matches than the 34 fantasy rounds, and
        Champions League clubs report their whole European campaign against 8
        league-phase rounds. The extra matches bring extra goals and results with
        them, so the aggregate stays self-consistent — it just measures more.
        """
        run_id = self._import()
        self._exec(
            """
            UPDATE club_season_stats
            SET matches_played = matches_played + 2,
                matches_won = matches_won + 2,
                goals_scored = goals_scored + 5,
                goals_conceded = goals_conceded + 1
            WHERE ingestion_run_id = :run
            """,
            run=run_id,
        )

        stale_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        report = run_quality_checks(
            self.session_factory, run_id=run_id, now=stale_now
        )

        checks = {c["name"]: c for c in report["checks"]}
        club_check = checks["club_result_reconciliation"]
        self.assertEqual(WARNING, club_check["status"])
        self.assertTrue(
            all(
                issue["details"]["provider_season_is_wider"]
                for issue in club_check["issues"]
            )
        )
        self.assertTrue(report["passed"])
        self.assertTrue(self._run_is_active(run_id))

    def test_self_contradicting_aggregate_warns_and_still_publishes(self) -> None:
        """A provider aggregate that disagrees with itself proves nothing.

        Kairat's 2025/2026 Champions League row counts 8 matches but 16 results,
        because the match count covers the league phase while the results include
        qualifying. Such a row cannot be reconciled against any calendar.
        """
        run_id = self._import()
        self._exec(
            """
            UPDATE club_season_stats
            SET matches_won = matches_won + 3
            WHERE ingestion_run_id = :run
            """,
            run=run_id,
        )

        stale_now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        report = run_quality_checks(
            self.session_factory, run_id=run_id, now=stale_now
        )

        checks = {c["name"]: c for c in report["checks"]}
        club_check = checks["club_result_reconciliation"]
        self.assertEqual(WARNING, club_check["status"])
        self.assertTrue(
            all(
                issue["details"]["provider_aggregate_contradicts_itself"]
                for issue in club_check["issues"]
            )
        )
        self.assertTrue(report["passed"])
        self.assertTrue(self._run_is_active(run_id))

    def test_missing_matches_report_expected_and_actual(self) -> None:
        run_id = self._import()
        self._exec("DELETE FROM club_match_stats")
        self._exec("DELETE FROM player_match_stats")
        self._exec("DELETE FROM matches")

        report = run_quality_checks(self.session_factory, run_id=run_id)

        checks = {c["name"]: c for c in report["checks"]}
        catalog = checks["catalog_completeness"]
        self.assertEqual(BLOCKING, catalog["status"])
        self.assertEqual(0, catalog["actual"]["matches"])
        self.assertEqual("> 0", catalog["expected"]["matches"])
        self.assertFalse(report["passed"])

    def test_reissue_is_idempotent(self) -> None:
        run_id = self._import()
        self._exec(
            """
            INSERT INTO matches
                (season_id, tour_id, stat_match_id, scheduled_at,
                 home_club_id, away_club_id, home_score, away_score)
            SELECT season_id, tour_id, '900001-dup', scheduled_at,
                   home_club_id, away_club_id, home_score, away_score
            FROM matches WHERE stat_match_id = '900001'
            """
        )
        run_quality_checks(self.session_factory, run_id=run_id)
        first = self._scalar("SELECT count(*) FROM data_quality_issues")
        run_quality_checks(self.session_factory, run_id=run_id)
        second = self._scalar("SELECT count(*) FROM data_quality_issues")
        self.assertEqual(first, second)

    def test_empty_database_raises(self) -> None:
        with self.assertRaises(QualityError):
            run_quality_checks(self.session_factory)


if __name__ == "__main__":
    unittest.main()
