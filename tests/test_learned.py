"""Unit tests for the learned (ridge) model on top of the feature dataset."""

from __future__ import annotations

import unittest

from fantasy_analytics.learned import (
    FEATURE_NAMES,
    MIN_TRAINING_TOURS,
    MODEL_LEARNED,
    TrainingPool,
    feature_vector,
    fit_ridge,
    learned_rows,
)


def _row(psid: int, points_per90: float, p_appearance: float = 1.0) -> dict:
    return {
        "player_season_id": psid,
        "role": "MIDFIELDER",
        "p_appearance": p_appearance,
        "expected_minutes": 90.0 * p_appearance,
        "appearance_share": p_appearance,
        "start_share": p_appearance,
        "points_per90": points_per90,
        "is_available": p_appearance > 0,
        "is_home": psid % 2 == 0,
    }


def _event(psid: int, expected: float, p_appearance: float = 1.0) -> dict:
    return {
        "player_season_id": psid,
        "model_name": "poisson_events",
        "model_version": "x",
        "scoring_version": "y",
        "expected_points": expected,
        "uncertainty": 1.5,
        "components": {"appearance": 2.0, "goals": expected - 2.0},
        "params": {},
        "p_appearance": p_appearance,
        "is_available": p_appearance > 0,
        "role": "MIDFIELDER",
        "player_name": str(psid),
    }


class FeatureVectorTest(unittest.TestCase):
    def test_vector_matches_the_documented_names(self) -> None:
        vector = feature_vector(_row(1, 3.0), _event(1, 4.0))
        self.assertEqual(len(FEATURE_NAMES), len(vector))
        self.assertEqual(4.0, vector[FEATURE_NAMES.index("event_expected_points")])
        self.assertEqual(1.0, vector[FEATURE_NAMES.index("role_midfielder")])
        self.assertEqual(0.0, vector[FEATURE_NAMES.index("role_forward")])

    def test_missing_values_become_zero(self) -> None:
        vector = feature_vector({"player_season_id": 1, "role": "FORWARD"}, None)
        self.assertEqual(0.0, sum(abs(v) for v in vector[:-5]))


class RidgeTest(unittest.TestCase):
    def test_recovers_a_linear_relation(self) -> None:
        vectors = [[float(i), float(i % 3)] for i in range(40)]
        targets = [2.0 * v[0] - 1.0 * v[1] + 0.5 for v in vectors]
        model = fit_ridge(vectors, targets, alpha=0.001)
        predictions = model.predict([[10.0, 1.0], [20.0, 2.0]])
        self.assertAlmostEqual(19.5, float(predictions[0]), places=2)
        self.assertAlmostEqual(38.5, float(predictions[1]), places=2)
        self.assertLess(model.residual_std, 0.01)

    def test_regularisation_shrinks_the_weights(self) -> None:
        vectors = [[float(i)] for i in range(20)]
        targets = [3.0 * v[0] for v in vectors]
        loose = fit_ridge(vectors, targets, alpha=0.001)
        tight = fit_ridge(vectors, targets, alpha=1000.0)
        self.assertLess(abs(tight.coefficients[0]), abs(loose.coefficients[0]))

    def test_constant_column_does_not_break_the_fit(self) -> None:
        vectors = [[1.0, float(i)] for i in range(10)]
        model = fit_ridge(vectors, [float(i) for i in range(10)])
        self.assertEqual(2, len(model.coefficients))


class LearnedRowsTest(unittest.TestCase):
    def _pool(self, tours: int) -> TrainingPool:
        pool = TrainingPool()
        for tour in range(tours):
            rows = [_row(i, 1.0 + i * 0.1) for i in range(60)]
            events = {i: _event(i, 2.0 + i * 0.1) for i in range(60)}
            actuals = {i: 3.0 + i * 0.1 for i in range(60)}
            pool.add_tour(rows, events, actuals)
        return pool

    def test_falls_back_to_the_event_forecast_before_enough_tours(self) -> None:
        pool = self._pool(MIN_TRAINING_TOURS - 1)
        self.assertFalse(pool.ready)
        rows = learned_rows([_row(1, 1.0)], [_event(1, 4.2)], pool, cutoff="c", feature_version="f")
        self.assertEqual(1, len(rows))
        self.assertEqual(MODEL_LEARNED, rows[0]["model_name"])
        self.assertEqual(4.2, rows[0]["expected_points"])
        self.assertFalse(rows[0]["params"]["trained"])

    def test_learns_the_offset_the_event_model_misses(self) -> None:
        pool = self._pool(MIN_TRAINING_TOURS)
        self.assertTrue(pool.ready)
        rows = learned_rows([_row(7, 1.7)], [_event(7, 2.7)], pool, cutoff="c", feature_version="f")
        # The pool says actual = event + 1 for every row; the ridge penalty
        # keeps the fit a little short of the exact offset.
        self.assertAlmostEqual(3.7, rows[0]["expected_points"], delta=0.3)
        self.assertTrue(rows[0]["params"]["trained"])
        self.assertGreater(rows[0]["components"]["learned_adjustment"], 0.7)
        self.assertEqual("event_expected_points", list(rows[0]["components"])[0])

    def test_unavailable_players_stay_at_zero(self) -> None:
        pool = self._pool(MIN_TRAINING_TOURS)
        rows = learned_rows(
            [_row(9, 1.0, p_appearance=0.0)],
            [_event(9, 0.0, p_appearance=0.0)],
            pool,
            cutoff="c",
            feature_version="f",
        )
        self.assertEqual(0.0, rows[0]["expected_points"])

    def test_pool_skips_players_who_could_not_play(self) -> None:
        pool = TrainingPool()
        added = pool.add_tour(
            [_row(1, 1.0), _row(2, 1.0, p_appearance=0.0)],
            {1: _event(1, 2.0), 2: _event(2, 0.0, 0.0)},
            {1: 3.0},
        )
        self.assertEqual(1, added)
        self.assertEqual(1, pool.tours)


if __name__ == "__main__":
    unittest.main()
