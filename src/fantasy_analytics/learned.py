"""A learned model on top of the feature dataset (step 23, plan item 2).

The event model is an explainable baseline: every point it forecasts is a
rule applied to a rate. It is also, by construction, blind to whatever the
rules and rates miss — how strongly a player's recent form carries over,
whether a home fixture is worth more to a forward than a defender, how much
price says on top of the rates. This module fits a *regularised linear
regression* (ridge) on the feature rows, with the event model's own forecast
and components among the inputs, so it can only add what the baseline lacks
and falls back to the baseline where there is nothing to add.

Design constraints, in order of importance:

* **Walk-forward, always.** A tour is only ever predicted from a model fitted
  on tours whose matches were all played before that tour's cutoff. The
  backtest feeds the pool tour by tour; the live forecast trains on the
  season's finished tours. No coefficient is ever fitted on the tour it
  predicts.
* **Explicit fallback.** With fewer than :data:`MIN_TRAINING_TOURS` tours in
  the pool the learned rows simply repeat the event forecast and say so in
  ``params.trained``, so the model is always defined for every tour.
* **Separate model name.** Rows are persisted under :data:`MODEL_LEARNED`,
  never in place of the event model; the optimizer keeps the event model as
  its default until the learned one wins on every backtest criterion in every
  league (see the step-23 card for the current standing).
* **No dependencies beyond numpy.** A closed-form ridge fit is a few lines and
  needs no tuning loop; the single regularisation strength is fixed and
  documented.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

MODEL_LEARNED = "ridge_stack"
LEARNED_VERSION = "1.0.0"

# The learned model needs this many *tours* of training rows before it
# produces its own numbers; below it the event forecast is repeated.
MIN_TRAINING_TOURS = 3

# Ridge regularisation on standardised features. Fixed rather than tuned per
# run so every tour of a backtest is predicted by the same procedure; a few
# thousand rows per tour make the fit stable well before this value matters.
RIDGE_ALPHA = 30.0

# How many finished tours the live forecast trains on at most (the most recent
# ones): enough for a stable fit, few enough to rebuild in seconds.
MAX_TRAINING_TOURS = 20

ROLES = ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")

# Feature row fields used directly (missing or null -> 0).
_ROW_FIELDS = (
    "p_appearance",
    "expected_minutes",
    "appearance_share",
    "start_share",
    "ninety_share",
    "points_per90",
    "goals_per90",
    "assists_per90",
    "saves_per90",
    "recoveries_per90",
    "points_avg_3",
    "points_avg_5",
    "points_avg_10",
    "minutes_avg_5",
    "club_attack",
    "club_defense",
    "opponent_attack",
    "opponent_defense",
    "fixture_count",
    "price",
)
# Event-forecast components used as inputs.
_COMPONENT_FIELDS = (
    "appearance",
    "goals",
    "assists",
    "clean_sheet",
    "conceded",
    "saves",
    "recoveries",
)

FEATURE_NAMES: tuple[str, ...] = (
    "event_expected_points",
    *(f"event_{name}" for name in _COMPONENT_FIELDS),
    *_ROW_FIELDS,
    "is_home",
    "is_newcomer",
    *(f"role_{role.lower()}" for role in ROLES),
    "home_x_goals",
)


def _num(value: Any) -> float:
    if value is None or isinstance(value, bool):
        return float(bool(value)) if isinstance(value, bool) else 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def feature_vector(row: dict[str, Any], event: dict[str, Any] | None) -> list[float]:
    """The numeric inputs of one player row, in :data:`FEATURE_NAMES` order."""
    components = (event or {}).get("components") or {}
    event_points = _num((event or {}).get("expected_points"))
    values = [event_points]
    values.extend(_num(components.get(name)) for name in _COMPONENT_FIELDS)
    values.extend(_num(row.get(name)) for name in _ROW_FIELDS)
    is_home = 1.0 if row.get("is_home") else 0.0
    values.append(is_home)
    values.append(1.0 if row.get("is_newcomer") else 0.0)
    values.extend(1.0 if row.get("role") == role else 0.0 for role in ROLES)
    values.append(is_home * _num(components.get("goals")))
    return values


@dataclass
class RidgeModel:
    """A fitted ridge regression on standardised inputs."""

    coefficients: np.ndarray
    intercept: float
    means: np.ndarray
    scales: np.ndarray
    alpha: float
    rows: int
    tours: int
    residual_std: float

    def predict(self, vectors: Sequence[Sequence[float]]) -> np.ndarray:
        matrix = np.asarray(vectors, dtype=float)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        standardised = (matrix - self.means) / self.scales
        return standardised @ self.coefficients + self.intercept

    def describe(self) -> dict[str, Any]:
        weights = {
            name: round(float(coef), 4)
            for name, coef in zip(FEATURE_NAMES, self.coefficients)
        }
        return {
            "alpha": self.alpha,
            "rows": self.rows,
            "tours": self.tours,
            "intercept": round(self.intercept, 4),
            "residual_std": round(self.residual_std, 4),
            "weights": weights,
        }


def fit_ridge(
    vectors: Sequence[Sequence[float]],
    targets: Sequence[float],
    *,
    alpha: float = RIDGE_ALPHA,
    tours: int = 0,
) -> RidgeModel:
    """Closed-form ridge on standardised features with an unpenalised intercept."""
    X = np.asarray(vectors, dtype=float)
    y = np.asarray(targets, dtype=float)
    if X.ndim != 2 or X.shape[0] == 0:
        raise ValueError("fit_ridge needs at least one training row")
    means = X.mean(axis=0)
    scales = X.std(axis=0)
    scales[scales <= 1e-12] = 1.0
    Z = (X - means) / scales
    y_mean = float(y.mean())
    penalty = alpha * np.eye(Z.shape[1])
    coefficients = np.linalg.solve(Z.T @ Z + penalty, Z.T @ (y - y_mean))
    residuals = y - (Z @ coefficients + y_mean)
    return RidgeModel(
        coefficients=coefficients,
        intercept=y_mean,
        means=means,
        scales=scales,
        alpha=alpha,
        rows=int(X.shape[0]),
        tours=tours,
        residual_std=float(np.sqrt(np.mean(residuals**2))),
    )


@dataclass
class TrainingPool:
    """Rows of already-played tours: ``(feature vector, actual points)``."""

    vectors: list[list[float]] = field(default_factory=list)
    targets: list[float] = field(default_factory=list)
    tours: int = 0

    def add_tour(
        self,
        feature_rows: Sequence[dict[str, Any]],
        event_by_player: dict[int, dict[str, Any]],
        actuals: dict[int, float],
    ) -> int:
        """Add one played tour. Rows of players who could not play are skipped.

        A player with ``p_appearance`` 0 is forecast at exactly zero by every
        model and would only teach the regression that zero predicts zero.
        """
        added = 0
        for row in feature_rows:
            if not row.get("is_available", True) or _num(row.get("p_appearance")) <= 0:
                continue
            psid = int(row["player_season_id"])
            self.vectors.append(feature_vector(row, event_by_player.get(psid)))
            self.targets.append(float(actuals.get(psid, 0.0)))
            added += 1
        self.tours += 1
        return added

    @property
    def ready(self) -> bool:
        return self.tours >= MIN_TRAINING_TOURS and len(self.vectors) >= 50

    def fit(self, *, alpha: float = RIDGE_ALPHA) -> RidgeModel:
        return fit_ridge(self.vectors, self.targets, alpha=alpha, tours=self.tours)


def learned_rows(
    feature_rows: Sequence[dict[str, Any]],
    event_rows: Sequence[dict[str, Any]],
    pool: TrainingPool | None,
    *,
    cutoff: str,
    feature_version: str,
) -> list[dict[str, Any]]:
    """Forecast rows for :data:`MODEL_LEARNED`, one per event-model row.

    ``event_rows`` are the event model's forecast rows for the same tour;
    identity fields are copied from them so the learned rows persist and
    optimise exactly like the others. Without a ready pool the expected
    points are the event model's, flagged ``trained: false``.
    """
    by_player = {int(row["player_season_id"]): row for row in event_rows}
    model = pool.fit() if pool is not None and pool.ready else None
    description = model.describe() if model is not None else None
    rows: list[dict[str, Any]] = []
    vectors: list[list[float]] = []
    order: list[int] = []
    for row in feature_rows:
        psid = int(row["player_season_id"])
        event = by_player.get(psid)
        if event is None:
            continue
        order.append(psid)
        vectors.append(feature_vector(row, event))
    predictions = model.predict(vectors) if model is not None and vectors else None
    for index, psid in enumerate(order):
        event = by_player[psid]
        available = bool(event.get("is_available", True)) and _num(event.get("p_appearance")) > 0
        if predictions is None or not available:
            expected = float(event["expected_points"]) if available or predictions is None else 0.0
        else:
            expected = float(predictions[index])
        rows.append(
            {
                **{
                    key: event.get(key)
                    for key in event
                    if key
                    not in (
                        "model_name",
                        "model_version",
                        "scoring_version",
                        "expected_points",
                        "uncertainty",
                        "components",
                        "params",
                    )
                },
                "feature_version": feature_version,
                "cutoff": cutoff,
                "model_name": MODEL_LEARNED,
                "model_version": LEARNED_VERSION,
                "scoring_version": event.get("scoring_version"),
                "expected_points": round(expected, 4),
                # The regression reports no per-player spread; the event
                # model's is the best available statement of it.
                "uncertainty": event.get("uncertainty"),
                "components": {
                    "event_expected_points": event["expected_points"],
                    "learned_adjustment": round(expected - float(event["expected_points"]), 4),
                },
                "params": {
                    "trained": model is not None,
                    "model": description,
                },
            }
        )
    return rows


__all__ = [
    "FEATURE_NAMES",
    "LEARNED_VERSION",
    "MAX_TRAINING_TOURS",
    "MIN_TRAINING_TOURS",
    "MODEL_LEARNED",
    "RIDGE_ALPHA",
    "RidgeModel",
    "TrainingPool",
    "feature_vector",
    "fit_ridge",
    "learned_rows",
]
