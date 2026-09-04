"""Tests for the scoring reconstruction and its cross-league audit (step 22).

The unit tests pin the scoring table to hand-computed matches, one per rule, so a
change to a point value cannot pass unnoticed. The integration test runs the audit
over a real imported snapshot and checks the shape of its report.

The audit exists because the scoring table was reconstructed from a single RPL
season: it is how a league whose point values differ would be caught, rather than
silently producing wrong projections and wrong squads.

They are skipped automatically when no database is reachable.
"""

from __future__ import annotations

import os
import unittest

from sqlalchemy import text

from fantasy_analytics.db import (
    create_db_engine,
    create_session_factory,
    migration,
    session_scope,
)
from fantasy_analytics.forecast import SCORING_VERSION, reconstruct_points
from fantasy_analytics.ingestion import IngestionOptions, run_ingestion
from fantasy_analytics.scoring_audit import audit_active_snapshots, audit_run
from fantasy_analytics.quality import run_quality_checks

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


def _score(role: str, **events: int) -> int:
    return reconstruct_points(
        role=role,
        minutes=events.pop("minutes", 90),
        goals=events.pop("goals", 0),
        assists=events.pop("assists", 0),
        saves=events.pop("saves", 0),
        ball_recoveries=events.pop("ball_recoveries", 0),
        yellow_cards=events.pop("yellow_cards", 0),
        goals_conceded=events.pop("goals_conceded", 0),
        **events,
    )


class ReconstructPointsTest(unittest.TestCase):
    def test_unused_substitute_scores_nothing(self) -> None:
        self.assertEqual(0, _score("MIDFIELDER", minutes=0, goals=2))

    def test_appearance_reward_depends_on_the_hour_mark(self) -> None:
        # An hour on the pitch turns one appearance point into two and is also
        # what makes the midfielder's clean sheet count, so the step is +2.
        self.assertEqual(1, _score("MIDFIELDER", minutes=59))
        self.assertEqual(2 + 1, _score("MIDFIELDER", minutes=60))

    def test_goals_are_worth_more_from_the_back(self) -> None:
        by_role = {
            role: _score(role, goals=1, goals_conceded=1, minutes=89)
            for role in ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")
        }
        # Appearance 2, one goal, and no conceded penalty until the second goal.
        self.assertEqual(8, by_role["GOALKEEPER"])
        self.assertEqual(8, by_role["DEFENDER"])
        self.assertEqual(7, by_role["MIDFIELDER"])
        self.assertEqual(6, by_role["FORWARD"])

    def test_the_full_match_pays_attackers_one_more(self) -> None:
        # Scoring .3: a midfielder or forward who sees the final whistle earns a
        # third appearance point; a defender or keeper does not.
        self.assertEqual(3, _score("MIDFIELDER", minutes=90, goals_conceded=1))
        self.assertEqual(2, _score("MIDFIELDER", minutes=89, goals_conceded=1))
        self.assertEqual(3, _score("FORWARD", minutes=90))
        self.assertEqual(2, _score("DEFENDER", minutes=90, goals_conceded=1))
        self.assertEqual(2, _score("GOALKEEPER", minutes=90, goals_conceded=1))

    def test_rare_events_are_charged_at_their_table_values(self) -> None:
        base = _score("DEFENDER", goals_conceded=1)
        self.assertEqual(base - 3, _score("DEFENDER", goals_conceded=1, red_cards=1))
        # A second yellow: the yellow's -1 plus -2 for the red make the same -3.
        self.assertEqual(
            base - 3,
            _score("DEFENDER", goals_conceded=1, yellow_cards=1, red_cards=1),
        )
        self.assertEqual(base - 2, _score("DEFENDER", goals_conceded=1, own_goals=1))
        self.assertEqual(
            base - 2, _score("DEFENDER", goals_conceded=1, penalty_conceded=1)
        )
        forward = _score("FORWARD", minutes=89)
        self.assertEqual(forward - 2, _score("FORWARD", minutes=89, penalties_missed=1))
        keeper = _score("GOALKEEPER", goals_conceded=1)
        self.assertEqual(keeper + 5, _score("GOALKEEPER", goals_conceded=1, penalties_saved=1))
        # Only a keeper is paid for a penalty save.
        self.assertEqual(base, _score("DEFENDER", goals_conceded=1, penalties_saved=1))

    def test_clean_sheet_needs_a_full_appearance(self) -> None:
        self.assertEqual(2 + 4, _score("DEFENDER", minutes=90))
        self.assertEqual(1, _score("DEFENDER", minutes=30))

    def test_conceded_goals_cost_a_point_per_pair(self) -> None:
        self.assertEqual(2 - 1, _score("DEFENDER", goals_conceded=2))
        self.assertEqual(2 - 1, _score("DEFENDER", goals_conceded=3))
        self.assertEqual(2 - 2, _score("DEFENDER", goals_conceded=4))
        # Outfield attackers are not charged for conceding.
        self.assertEqual(2, _score("FORWARD", goals_conceded=4, minutes=89))

    def test_saves_only_reward_the_goalkeeper(self) -> None:
        self.assertEqual(2 + 2, _score("GOALKEEPER", saves=6, goals_conceded=1))
        self.assertEqual(2, _score("DEFENDER", saves=6, goals_conceded=1))

    def test_recoveries_and_cards_apply_to_every_role(self) -> None:
        self.assertEqual(
            2 + 4 + 2 - 1,
            _score("DEFENDER", ball_recoveries=7, yellow_cards=1),
        )
        self.assertEqual(
            2 + 2 - 1,
            _score("FORWARD", ball_recoveries=6, yellow_cards=1, minutes=89),
        )


@requires_database
class ScoringAuditIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_db_engine(TEST_DATABASE_URL)
        self.addCleanup(self.engine.dispose)
        migration.downgrade(TEST_DATABASE_URL)
        migration.upgrade(TEST_DATABASE_URL)
        self.session_factory = create_session_factory(self.engine)
        report = run_ingestion(
            FakeClient(_consistent_fixture()),
            self.session_factory,
            IngestionOptions(history_workers=1),
        )
        self.run_id = report["run_id"]
        run_quality_checks(self.session_factory, run_id=self.run_id)

    def test_audit_scores_every_played_match(self) -> None:
        with session_scope(self.session_factory) as session:
            report = audit_run(session, self.run_id)

        self.assertEqual(SCORING_VERSION, report["scoring_version"])
        self.assertGreater(report["appearances"], 0)
        # The shares are cumulative bands, so a wider tolerance can never fit less.
        self.assertLessEqual(report["within_0"], report["within_1"])
        self.assertLessEqual(report["within_1"], report["within_2"])
        self.assertTrue(report["by_role"])
        self.assertEqual(
            report["appearances"],
            sum(role["appearances"] for role in report["by_role"].values()),
        )

    def test_audit_covers_every_published_league(self) -> None:
        reports = audit_active_snapshots(self.session_factory)

        self.assertEqual(1, len(reports))
        self.assertEqual("russia", reports[0]["slug"])
        self.assertEqual("2025/2026", reports[0]["season"])
        self.assertEqual(self.run_id, reports[0]["run_id"])

    def test_audit_of_an_unknown_run_is_empty_rather_than_an_error(self) -> None:
        with session_scope(self.session_factory) as session:
            report = audit_run(session, 999999)

        self.assertEqual(0, report["appearances"])
        self.assertEqual(0.0, report["within_0"])


if __name__ == "__main__":
    unittest.main()
