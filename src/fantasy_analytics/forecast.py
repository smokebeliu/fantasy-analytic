"""Baseline fantasy-points forecast (development-plan step 7).

The forecast turns the leakage-free feature dataset (step 6) into an expected
number of fantasy points for every player whose club plays a target tour. It is
deliberately an *interpretable* baseline, not a black box:

* **Expected minutes first.** Appearance points build on the separate
  appearance-probability / expected-minutes estimates from the features.
* **Team goals via Poisson.** Each club's goals for/against are modelled as
  Poisson rates derived from venue attack/defence, and the clean-sheet
  probability is the Poisson probability that the opponent fails to score.
* **Event contributions.** Goals, assists, saves, ball recoveries and cards are
  projected from the player's per-90 rates and expected minutes.
* **Rules, not constants.** Each projected event is converted into points with a
  versioned scoring table (:data:`SCORING`), reconstructed from the season's
  own per-match points, so ``expected_points`` is the exact sum of its
  ``components``.
* **Fixture-linked exposures.** Alongside the components, each event forecast
  reports how much of it rides on the player's own club scoring
  (``goal_upside``) and how much is lost per goal their opponent scores
  (``shutout_stake``). The squad optimizer (step 16) uses the pair to price the
  anti-correlation between a defence and the attack it faces.

Two trivial baselines (season mean and recent form) are produced alongside the
event model so the main model can always be compared against them. Every
forecast is stamped with the model name/version, the feature version, the
scoring version and the ingestion run (data snapshot) it was built from, and the
computation is pure arithmetic, so recomputing on the same snapshot is
deterministic.

The module only reads the database (through the feature builder); persistence is
handled separately by :class:`fantasy_analytics.db.forecast_repository`.
Neural networks and automatic hyper-parameter tuning are out of scope.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import sessionmaker

from .db import session_scope
from .db.forecast_repository import ForecastRepository
from .features import FEATURE_VERSION, build_feature_dataset

# Bumped whenever the model or its parameters change so forecasts from different
# code revisions never get silently mixed.
MODEL_VERSION = "1.0.0"

# Model names persisted alongside every forecast row.
MODEL_EVENT = "poisson_events"
MODEL_MEAN = "season_mean"
MODEL_RECENT = "recent_form"

# Version of the scoring table below. It is empirically reconstructed from the
# 2025/2026 RPL season's per-match ``points`` (the authoritative fantasy score),
# because Sports.ru only publishes the scoring rules as an image and the
# structured per-event breakdown (``statDetails``) is empty. Reconstructing the
# 9578 imported player-match rows from these rules reproduces 83% exactly and
# 96% within +/-1 point; the residual is dominated by the indirect "fantasy
# assist" and late ball-recovery adjustments that are not present in the
# imported per-match columns (documented in docs/data-model.md).
SCORING_VERSION = "rpl-2025-2026.1"

# Minutes threshold for a "full" appearance (a start): clean sheets and the
# 2-point appearance bonus require it.
START_MINUTES = 60

ROLES = ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")


@dataclass(frozen=True)
class RoleScoring:
    """Points a single event is worth for one role."""

    appearance_sub: int  # played 1..59 minutes
    appearance_full: int  # played >= 60 minutes
    goal: int
    assist: int
    clean_sheet: int  # only when a full appearance and opponent fails to score
    conceded_per_two: int  # penalty for every 2 goals conceded (GK/DEF)
    save_per_three: int  # goalkeeper saves reward, per 3 saves
    recovery_per_three: int  # ball-recovery reward, per 3 recoveries
    yellow_card: int


# The versioned scoring table. Rewards/penalties that are role-independent
# (assist +3, recovery +1 per 3, yellow -1) are repeated per role for clarity.
SCORING: dict[str, RoleScoring] = {
    "GOALKEEPER": RoleScoring(
        appearance_sub=1,
        appearance_full=2,
        goal=6,
        assist=3,
        clean_sheet=4,
        conceded_per_two=-1,
        save_per_three=1,
        recovery_per_three=1,
        yellow_card=-1,
    ),
    "DEFENDER": RoleScoring(
        appearance_sub=1,
        appearance_full=2,
        goal=6,
        assist=3,
        clean_sheet=4,
        conceded_per_two=-1,
        save_per_three=0,
        recovery_per_three=1,
        yellow_card=-1,
    ),
    "MIDFIELDER": RoleScoring(
        appearance_sub=1,
        appearance_full=2,
        goal=5,
        assist=3,
        clean_sheet=1,
        conceded_per_two=0,
        save_per_three=0,
        recovery_per_three=1,
        yellow_card=-1,
    ),
    "FORWARD": RoleScoring(
        appearance_sub=1,
        appearance_full=2,
        goal=4,
        assist=3,
        clean_sheet=0,
        conceded_per_two=0,
        save_per_three=0,
        recovery_per_three=1,
        yellow_card=-1,
    ),
}


class ForecastError(RuntimeError):
    """Raised when a forecast cannot be built (propagates feature errors)."""


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in isolation).
# ---------------------------------------------------------------------------


def poisson_pmf(k: int, lam: float) -> float:
    """Probability of exactly ``k`` events for a Poisson mean ``lam``."""
    if lam < 0:
        raise ValueError("Poisson mean must be non-negative")
    if lam == 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def clean_sheet_probability(opponent_goals_mean: float) -> float:
    """Poisson probability that the opponent fails to score (a clean sheet)."""
    return poisson_pmf(0, max(0.0, opponent_goals_mean))


def team_goal_means(
    club_attack: float,
    club_defense: float,
    opponent_attack: float,
    opponent_defense: float,
) -> tuple[float, float]:
    """Return (expected goals for, expected goals against) for the club.

    Each side's scoring rate blends the club's own venue attack with how many
    goals the opponent typically concedes at that venue, and vice versa. It is a
    simple, symmetric estimator that needs no league-wide normalisation term.
    """
    goals_for = 0.5 * (max(0.0, club_attack) + max(0.0, opponent_defense))
    goals_against = 0.5 * (max(0.0, club_defense) + max(0.0, opponent_attack))
    return round(goals_for, 4), round(goals_against, 4)


def appearance_probabilities(
    p_appearance: float, start_share: float, appearance_share: float
) -> tuple[float, float]:
    """Split the play probability into (full-match, substitute) probabilities.

    ``start_share`` / ``appearance_share`` gives the historical share of a
    player's appearances that were full (>= 60'). Applying it to
    ``p_appearance`` splits the probability of playing into a full appearance
    and a shorter one, which the appearance and clean-sheet rewards need.
    """
    p_appearance = min(max(p_appearance, 0.0), 1.0)
    if appearance_share > 0:
        full_ratio = min(max(start_share / appearance_share, 0.0), 1.0)
    else:
        full_ratio = 0.0
    p_full = round(p_appearance * full_ratio, 6)
    p_sub = round(p_appearance - p_full, 6)
    return p_full, p_sub


def _expected_from_rate(per90: float, expected_minutes: float) -> float:
    """Expected count of an event from its per-90 rate and expected minutes."""
    return max(0.0, per90) * max(0.0, expected_minutes) / 90.0


# ---------------------------------------------------------------------------
# Event model.
# ---------------------------------------------------------------------------


def forecast_event_model(row: dict[str, Any]) -> dict[str, Any]:
    """Interpretable event-based forecast for one feature row.

    Returns a dict with ``expected_points`` (the exact sum of ``components``),
    an ``uncertainty`` standard deviation, the ``fixture`` exposures the
    optimizer prices and the intermediate expectations. An unavailable player
    (``is_available`` false or ``p_appearance`` 0) scores a flat zero so it never
    enters a squad.
    """
    role = row["role"]
    scoring = SCORING.get(role)
    if scoring is None:
        raise ForecastError(f"Unknown role {role!r} for scoring")

    p_appearance = float(row.get("p_appearance") or 0.0)
    expected_minutes = float(row.get("expected_minutes") or 0.0)
    is_available = bool(row.get("is_available", True))

    p_full, p_sub = appearance_probabilities(
        p_appearance,
        float(row.get("start_share") or 0.0),
        float(row.get("appearance_share") or 0.0),
    )

    goals_for, goals_against = team_goal_means(
        float(row.get("club_attack") or 0.0),
        float(row.get("club_defense") or 0.0),
        float(row.get("opponent_attack") or 0.0),
        float(row.get("opponent_defense") or 0.0),
    )
    p_clean_sheet = clean_sheet_probability(goals_against)

    exp_goals = _expected_from_rate(float(row.get("goals_per90") or 0.0), expected_minutes)
    exp_assists = _expected_from_rate(
        float(row.get("assists_per90") or 0.0), expected_minutes
    )
    exp_saves = _expected_from_rate(float(row.get("saves_per90") or 0.0), expected_minutes)
    exp_recoveries = _expected_from_rate(
        float(row.get("recoveries_per90") or 0.0), expected_minutes
    )
    exp_yellows = _expected_from_rate(
        float(row.get("yellows_per90") or 0.0), expected_minutes
    )

    # Points contributed by each component. The appearance bonus mixes full and
    # substitute probabilities; every other reward is a rate times its value.
    appearance_pts = scoring.appearance_full * p_full + scoring.appearance_sub * p_sub
    goal_pts = scoring.goal * exp_goals
    assist_pts = scoring.assist * exp_assists
    # A clean sheet only counts for a full appearance.
    clean_sheet_pts = scoring.clean_sheet * p_full * p_clean_sheet
    # -1 per 2 conceded ~= -0.5 per conceded goal, weighted by playing at all.
    conceded_pts = (scoring.conceded_per_two / 2.0) * goals_against * p_appearance
    save_pts = (scoring.save_per_three / 3.0) * exp_saves
    recovery_pts = (scoring.recovery_per_three / 3.0) * exp_recoveries
    yellow_pts = scoring.yellow_card * exp_yellows

    components = {
        "appearance": round(appearance_pts, 4),
        "goals": round(goal_pts, 4),
        "assists": round(assist_pts, 4),
        "clean_sheet": round(clean_sheet_pts, 4),
        "conceded": round(conceded_pts, 4),
        "saves": round(save_pts, 4),
        "recoveries": round(recovery_pts, 4),
        "yellow_cards": round(yellow_pts, 4),
    }

    playing = is_available and p_appearance > 0.0
    if not playing:
        components = {key: 0.0 for key in components}

    expected_points = round(sum(components.values()), 4)

    # Fixture-linked exposures (step 16). ``goal_upside`` is the part of the
    # forecast that only materialises when the player's own club scores, and
    # ``shutout_stake`` is what the player forfeits per goal their opponent
    # scores: the clean sheet they lose plus the concession penalty. For two
    # players on opposite sides of one fixture, ``goal_upside * shutout_stake``
    # (both ways round) is exactly the magnitude of the covariance of their two
    # forecasts under this Poisson goal model, because the Poisson mean cancels
    # out of ``Cov(1{G=0}, G) = -P(G=0) * lambda`` and ``Var(G) = lambda``.
    conceded_per_goal = (
        (-scoring.conceded_per_two / 2.0) * p_appearance if playing else 0.0
    )
    fixture = {
        "goal_upside": round(components["goals"] + components["assists"], 4),
        "shutout_stake": round(components["clean_sheet"] + conceded_per_goal, 4),
    }

    # Uncertainty: standard deviation from independent component variances.
    # Counts are treated as Poisson (Var = mean); the two Bernoulli terms use
    # p(1-p). This is a documented approximation, not a calibrated interval.
    if not playing:
        uncertainty = 0.0
    else:
        variance = (
            scoring.goal**2 * exp_goals
            + scoring.assist**2 * exp_assists
            + (scoring.save_per_three / 3.0) ** 2 * exp_saves
            + (scoring.recovery_per_three / 3.0) ** 2 * exp_recoveries
            + scoring.yellow_card**2 * exp_yellows
            + (scoring.clean_sheet * p_full) ** 2
            * p_clean_sheet
            * (1.0 - p_clean_sheet)
            + (scoring.appearance_full - scoring.appearance_sub) ** 2
            * p_full
            * (1.0 - p_full)
        )
        uncertainty = round(math.sqrt(max(0.0, variance)), 4)

    return {
        "expected_points": expected_points,
        "uncertainty": uncertainty,
        "components": components,
        "fixture": fixture,
        "expected": {
            "goals": round(exp_goals, 4),
            "assists": round(exp_assists, 4),
            "saves": round(exp_saves, 4),
            "recoveries": round(exp_recoveries, 4),
            "yellow_cards": round(exp_yellows, 4),
            "team_goals_for": goals_for,
            "team_goals_against": goals_against,
            "clean_sheet_probability": round(p_clean_sheet, 4),
            "p_full_appearance": p_full,
            "p_sub_appearance": p_sub,
        },
    }


def forecast_mean_baseline(row: dict[str, Any]) -> dict[str, Any]:
    """Baseline: season mean points per appearance scaled by play probability."""
    p_appearance = float(row.get("p_appearance") or 0.0)
    total_appearances = int(row.get("total_appearances") or 0)
    total_points = float(row.get("total_points") or 0.0)
    is_available = bool(row.get("is_available", True))

    mean_points = total_points / total_appearances if total_appearances else 0.0
    expected_points = mean_points * p_appearance if is_available else 0.0
    return {
        "expected_points": round(expected_points, 4),
        "uncertainty": None,
        "components": {
            "mean_points_per_appearance": round(mean_points, 4),
            "p_appearance": round(p_appearance, 4),
        },
    }


def forecast_recent_baseline(row: dict[str, Any]) -> dict[str, Any]:
    """Baseline: mean points over the last 5 appearances times play probability."""
    p_appearance = float(row.get("p_appearance") or 0.0)
    points_avg_5 = float(row.get("points_avg_5") or 0.0)
    is_available = bool(row.get("is_available", True))

    expected_points = points_avg_5 * p_appearance if is_available else 0.0
    return {
        "expected_points": round(expected_points, 4),
        "uncertainty": None,
        "components": {
            "points_avg_5": round(points_avg_5, 4),
            "p_appearance": round(p_appearance, 4),
        },
    }


# ---------------------------------------------------------------------------
# Dataset builder.
# ---------------------------------------------------------------------------

_IDENTITY_FIELDS = (
    "player_season_id",
    "fantasy_player_id",
    "player_name",
    "role",
    "club_id",
    "club_name",
    "opponent_club_id",
    "opponent_name",
    "match_id",
    "is_home",
    "is_available",
    "availability_status",
    "price",
    # Cross-season provenance (step 14): where the history came from, whether the
    # player has any history at all and whether they are a prior-less newcomer.
    "stat_source",
    "has_history",
    "is_newcomer",
)


def _forecast_rows_for_player(
    feature_row: dict[str, Any], cutoff: str
) -> list[dict[str, Any]]:
    """Produce one forecast row per model for a single feature row."""
    identity = {field: feature_row.get(field) for field in _IDENTITY_FIELDS}
    common = {
        **identity,
        "feature_version": feature_row.get("feature_version", FEATURE_VERSION),
        "cutoff": cutoff,
        "p_appearance": feature_row.get("p_appearance"),
        "expected_minutes": feature_row.get("expected_minutes"),
    }

    event = forecast_event_model(feature_row)
    mean = forecast_mean_baseline(feature_row)
    recent = forecast_recent_baseline(feature_row)

    rows: list[dict[str, Any]] = [
        {
            **common,
            "model_name": MODEL_EVENT,
            "model_version": MODEL_VERSION,
            "scoring_version": SCORING_VERSION,
            "expected_points": event["expected_points"],
            "uncertainty": event["uncertainty"],
            "components": event["components"],
            "params": {"expected": event["expected"], "fixture": event["fixture"]},
        },
        {
            **common,
            "model_name": MODEL_MEAN,
            "model_version": MODEL_VERSION,
            "scoring_version": None,
            "expected_points": mean["expected_points"],
            "uncertainty": mean["uncertainty"],
            "components": mean["components"],
            "params": None,
        },
        {
            **common,
            "model_name": MODEL_RECENT,
            "model_version": MODEL_VERSION,
            "scoring_version": None,
            "expected_points": recent["expected_points"],
            "uncertainty": recent["uncertainty"],
            "components": recent["components"],
            "params": None,
        },
    ]
    return rows


def forecast_from_features(
    features: dict[str, Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Turn an already-built feature dataset into the forecast report.

    Separated from :func:`build_forecast_dataset` so a caller that already has
    the features (backtesting, step 19, which also audits them) does not have to
    rebuild them from the database a second time.
    """
    generated_at = now or datetime.now(UTC)
    cutoff = features["cutoff"]
    rows: list[dict[str, Any]] = []
    for feature_row in features["rows"]:
        rows.extend(_forecast_rows_for_player(feature_row, cutoff))

    # Deterministic ordering: model, then descending expected points, then name.
    rows.sort(
        key=lambda r: (
            r["model_name"],
            -float(r["expected_points"]),
            r["player_name"] or "",
            r["player_season_id"],
        )
    )

    available_players = sum(
        1 for r in features["rows"] if r.get("is_available", True)
    )

    return {
        "model_version": MODEL_VERSION,
        "scoring_version": SCORING_VERSION,
        "feature_version": features["feature_version"],
        "generated_at": generated_at.isoformat(),
        "run_id": features["run_id"],
        "season_id": features["season_id"],
        "season": features["season"],
        "tour": features["tour"],
        "cutoff": cutoff,
        "cross_season": features.get("cross_season", False),
        "prior_run_id": features.get("prior_run_id"),
        "models": [
            {"name": MODEL_EVENT, "version": MODEL_VERSION, "kind": "event"},
            {"name": MODEL_MEAN, "version": MODEL_VERSION, "kind": "baseline"},
            {"name": MODEL_RECENT, "version": MODEL_VERSION, "kind": "baseline"},
        ],
        "scoring": {
            "version": SCORING_VERSION,
            "roles": {role: vars(SCORING[role]) for role in ROLES},
        },
        "counts": {
            "players": len(features["rows"]),
            "available_players": available_players,
            "rows": len(rows),
            "fixtures": features["counts"]["fixtures"],
            "prior_sourced": features["counts"].get("prior_sourced", 0),
            "newcomers": features["counts"].get("newcomers", 0),
        },
        "rows": rows,
    }


def build_forecast_dataset(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    season_ref: str | None = None,
    tour_ref: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build forecasts for every player whose club plays the target tour.

    Returns a JSON-serialisable report with metadata, the model list and one
    ``rows`` entry per (player, model). The dataset is reproducible from
    ``(run_id, tour, model_version, feature_version)``.
    """
    generated_at = now or datetime.now(UTC)
    try:
        features = build_feature_dataset(
            session_factory,
            run_id=run_id,
            season_ref=season_ref,
            tour_ref=tour_ref,
            now=generated_at,
        )
    except Exception as error:  # noqa: BLE001 - normalise to a forecast error
        raise ForecastError(str(error)) from error
    return forecast_from_features(features, now=generated_at)


def run_forecast(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    season_ref: str | None = None,
    tour_ref: str | None = None,
    now: datetime | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Build forecasts and (optionally) persist them to ``player_forecasts``.

    Persistence is idempotent per run/tour/model, so re-running on the same
    snapshot replaces rather than accumulates rows. The returned report gains a
    ``persisted`` count when ``persist`` is true.
    """
    report = build_forecast_dataset(
        session_factory,
        run_id=run_id,
        season_ref=season_ref,
        tour_ref=tour_ref,
        now=now,
    )
    if persist and report["rows"]:
        cutoff = datetime.fromisoformat(report["cutoff"])
        with session_scope(session_factory) as session:
            repo = ForecastRepository(session)
            persisted = repo.replace_forecasts(
                run_id=report["run_id"],
                season_id=report["season_id"],
                tour_id=report["tour"]["tour_id"],
                cutoff=cutoff,
                rows=report["rows"],
            )
        report["persisted"] = persisted
    else:
        report["persisted"] = 0
    return report


__all__ = [
    "MODEL_VERSION",
    "MODEL_EVENT",
    "MODEL_MEAN",
    "MODEL_RECENT",
    "SCORING_VERSION",
    "SCORING",
    "RoleScoring",
    "ForecastError",
    "poisson_pmf",
    "clean_sheet_probability",
    "team_goal_means",
    "appearance_probabilities",
    "forecast_event_model",
    "forecast_mean_baseline",
    "forecast_recent_baseline",
    "forecast_from_features",
    "build_forecast_dataset",
    "run_forecast",
]
