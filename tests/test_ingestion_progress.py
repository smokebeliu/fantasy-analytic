"""Unit tests for the ingestion progress vocabulary (step 17).

The admin refresh screen renders a stage, not a log line, so the mapping from the
pipeline's free-form progress messages to the stage vocabulary is the contract
between backend and frontend. These tests pin that mapping against the exact
messages :mod:`fantasy_analytics.ingestion` and the worker emit, and pin the
monotonicity guarantee that keeps a progress bar from going backwards.
"""

from __future__ import annotations

import unittest

from fantasy_analytics.ingestion_progress import (
    MAX_MESSAGE_LENGTH,
    STAGE_ORDER,
    STAGES,
    ProgressTracker,
    classify,
    percent_for,
    truncate,
)

# The messages the import pipeline actually emits, in the order it emits them.
PIPELINE_MESSAGES = (
    ("Job 7 started", "starting"),
    ("Created ingestion run 3", "starting"),
    ("Selected season 59", "fetch_season"),
    ("Fetched player page 1 (100 players)", "fetch_players"),
    ("Fetching match history for 423 players with 8 workers", "fetch_history"),
    ("Fetch stage finished in 31.4s", "persist"),
    ("Saved 432 raw responses", "persist"),
    ("Upserted 16 clubs", "persist"),
    ("Upserted 9578 player-match stats", "persist"),
    ("Ingestion run marked succeeded", "quality_gate"),
    ("Running quality checks for run 3", "quality_gate"),
    ("Job 7 succeeded (snapshot_active=True)", "finished"),
)


class StageVocabularyTest(unittest.TestCase):
    def test_stages_are_ordered_and_end_at_full_completion(self) -> None:
        percents = [percent for _, percent in STAGES]
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(0, percents[0])
        self.assertEqual(100, percents[-1])

    def test_percent_for_unknown_stage_is_zero(self) -> None:
        self.assertEqual(0, percent_for("not-a-stage"))
        self.assertEqual(100, percent_for("finished"))


class ClassifyTest(unittest.TestCase):
    def test_recognises_every_pipeline_message(self) -> None:
        for message, expected in PIPELINE_MESSAGES:
            with self.subTest(message=message):
                self.assertEqual(expected, classify(message))

    def test_unknown_message_is_not_classified(self) -> None:
        self.assertIsNone(classify("something entirely new"))
        self.assertIsNone(classify(""))


class ProgressTrackerTest(unittest.TestCase):
    def test_follows_the_pipeline_to_completion(self) -> None:
        tracker = ProgressTracker()
        self.assertEqual("queued", tracker.stage)

        seen: list[tuple[str, int]] = []
        for message, _ in PIPELINE_MESSAGES:
            stage, percent, note = tracker.observe(message)
            seen.append((stage, percent))
            self.assertEqual(message, note)

        self.assertEqual(("finished", 100), seen[-1])
        # The percentage never decreases along the run.
        percents = [percent for _, percent in seen]
        self.assertEqual(percents, sorted(percents))

    def test_late_message_from_an_earlier_stage_does_not_rewind(self) -> None:
        tracker = ProgressTracker()
        tracker.observe("Upserted 16 clubs")
        self.assertEqual("persist", tracker.stage)

        stage, percent, _ = tracker.observe("Fetched player page 9 (900 players)")
        self.assertEqual("persist", stage)
        self.assertEqual(percent_for("persist"), percent)

    def test_unknown_message_keeps_the_stage_but_updates_the_note(self) -> None:
        tracker = ProgressTracker()
        tracker.observe("Selected season 59")
        stage, _, note = tracker.observe("some new diagnostic")
        self.assertEqual("fetch_season", stage)
        self.assertEqual("some new diagnostic", note)

    def test_advance_to_only_moves_forward_and_rejects_unknown(self) -> None:
        tracker = ProgressTracker()
        tracker.advance_to("quality_gate")
        self.assertEqual(("quality_gate", percent_for("quality_gate")), tracker.advance_to("starting"))
        with self.assertRaises(ValueError):
            tracker.advance_to("nope")

    def test_every_stage_is_reachable_in_order(self) -> None:
        tracker = ProgressTracker()
        for stage in STAGE_ORDER:
            tracker.advance_to(stage)
            self.assertEqual(stage, tracker.stage)


class TruncateTest(unittest.TestCase):
    def test_bounds_long_messages(self) -> None:
        note = truncate("x" * 500)
        self.assertLessEqual(len(note), MAX_MESSAGE_LENGTH)
        self.assertTrue(note.endswith("…"))

    def test_keeps_short_messages_intact(self) -> None:
        self.assertEqual("Upserted 16 clubs", truncate("  Upserted 16 clubs  "))


if __name__ == "__main__":
    unittest.main()
