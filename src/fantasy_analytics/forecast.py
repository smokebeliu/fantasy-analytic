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
  projected from the player's per-90 rates and expected minutes. Rewards that
  the rules pay per completed block (per three saves or recoveries, per two
  goals conceded) are scored as the expected number of whole blocks, not as the
  count divided by the block size.
* **Rules, not constants.** Each projected event is converted into points with a
  versioned scoring table (:data:`SCORING`), reconstructed from the season's
  own per-match points, so ``expected_points`` is the exact sum of its
  ``components``.
* **Every match of the tour.** A fantasy tour is a slice of the calendar, not a
  round, so a postponed match is re-attached to whichever tour it now falls in.
  A club can therefore play twice in one tour and not at all in another, and the
  forecast is the sum over the matches it actually plays.
* **Fixture-linked exposures.** Alongside the components, each event forecast
  reports per match how much of it rides on the player's own club scoring
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
from .odds import DEFAULT_ODDS_WEIGHT, attach_odds_to_features, blend_goal_means

# Bumped whenever the model or its parameters change so forecasts from different
# code revisions never get silently mixed.
# 1.1.0 corrected the goalkeeper scoring row and replaced the linear
#   approximation of the per-N thresholds with their exact Poisson expectation.
# 1.2.0 scores every match a club plays in the target tour rather than only the
#   first, so a club doubled up by a postponement is no longer forecast at half.
# 1.3.0 blends the 1x2 bookmaker line into the Poisson match model: a favourite
#   against a weak defence lifts attacking points, a priced-up shutout lifts
#   clean-sheet points. The optimizer is unchanged — it still maximises
#   expected_points.
MODEL_VERSION = "1.3.0"

# Model names persisted alongside every forecast row.
MODEL_EVENT = "poisson_events"
MODEL_MEAN = "season_mean"
MODEL_RECENT = "recent_form"

# Version of the scoring table below. It is empirically reconstructed from the
# 2025/2026 RPL season's per-match ``points`` (the authoritative fantasy score),
# because Sports.ru only publishes the scoring rules as an image and the
# structured per-event breakdown (``statDetails``) is empty.
#
# ``.2`` corrected the goalkeeper row: keepers are *not* paid for ball
# recoveries. They record ~8.4 of them per 90 minutes — every claimed cross and
# collected back-pass — so paying the outfield rate of 1 point per 3 credited a
# keeper with roughly 2.3 points he never scored, in every single match. The
# error was invisible in the pooled accuracy figure because keepers are 6% of
# the rows, and it made the optimizer buy cheap goalkeepers instead of forwards.
# Dropping the reward moves the goalkeeper reconstruction from 3.7% to 94.6%
# exact on the RPL and from 2.4% to 94.7% on La Liga; the outfield rows were
# re-fitted at the same time and came out unchanged (see
# :mod:`fantasy_analytics.scoring_audit`).
SCORING_VERSION = "rpl-2025-2026.2"

# Minutes threshold for a "full" appearance (a start): clean sheets and the
# 2-point appearance bonus require it.
START_MINUTES = 60

ROLES = ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")

# The additive breakdown every event forecast reports, in a fixed order so a
# tour with no fixture at all still produces the same shape.
_COMPONENT_KEYS = (
    "appearance",
    "goals",
    "assists",
    "clean_sheet",
    "conceded",
    "saves",
    "recoveries",
    "yellow_cards",
)


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
# (assist +3, yellow -1) are repeated per role for clarity. The ball-recovery
# reward is *not* role-independent: only outfield players are paid for it.
SCORING: dict[str, RoleScoring] = {
    "GOALKEEPER": RoleScoring(
        appearance_sub=1,
        appearance_full=2,
        goal=6,
        assist=3,
        clean_sheet=4,
        conceded_per_two=-1,
        save_per_three=1,
        recovery_per_three=0,
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


def reconstruct_points(
    *,
    role: str,
    minutes: int,
    goals: int,
    assists: int,
    saves: int,
    ball_recoveries: int,
    yellow_cards: int,
    goals_conceded: int,
) -> int:
    """Score one played match from its events using :data:`SCORING`.

    This is the scoring table's definition made executable: the same rules the
    event model applies in expectation, applied to what actually happened. It is
    what :mod:`fantasy_analytics.scoring_audit` compares against the authoritative
    ``points`` column, which is how ``SCORING_VERSION`` is justified — and how a
    league whose point values differ from the RPL's would be caught.

    Deliberately incomplete: red cards, own goals, conceded penalties, missed
    penalties and the indirect "fantasy assist" are not modelled, because the
    imported per-match columns do not carry the events behind them.
    """
    if minutes <= 0:
        return 0
    scoring = SCORING[role]
    full = minutes >= START_MINUTES
    total = scoring.appearance_full if full else scoring.appearance_sub
    total += goals * scoring.goal
    total += assists * scoring.assist
    if full and goals_conceded == 0:
        total += scoring.clean_sheet
    total += (goals_conceded // 2) * scoring.conceded_per_two
    total += (saves // 3) * scoring.save_per_three
    total += (ball_recoveries // 3) * scoring.recovery_per_three
    total += yellow_cards * scoring.yellow_card
    return total


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


def expected_threshold_count(mean: float, step: int) -> float:
    """``E[floor(N / step)]`` for a Poisson count ``N`` with the given mean.

    Several rewards are paid in whole blocks rather than per event: one point
    per *three* recoveries or saves, one penalty per *two* goals conceded. Two
    recoveries are therefore worth nothing, and scoring them as ``2 / 3`` of a
    point is not a rounding detail — averaged over a season it overpays every
    threshold reward by roughly a third of a point per match, which is most of a
    goalkeeper's or a defender's whole edge.

    The expectation is summed directly from the Poisson probability mass, whose
    tail is cut where it can no longer move the result.
    """
    if step <= 0:
        raise ValueError("Threshold step must be positive")
    lam = max(0.0, float(mean))
    if lam == 0.0:
        return 0.0
    limit = int(lam + 12.0 * math.sqrt(lam)) + 4 * step + 12
    total = 0.0
    pmf = math.exp(-lam)
    for count in range(limit + 1):
        if count:
            pmf *= lam / count
        total += pmf * (count // step)
    return total


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


def tour_fixtures(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Every match of the target tour a feature row covers.

    Feature version 1.4.0 reports them in ``tour_fixtures``; an older row (or a
    hand-built one in a test) carries a single fixture flattened onto the row
    itself, which is read as a one-match tour.
    """
    fixtures = row.get("tour_fixtures")
    if fixtures:
        return list(fixtures)
    return [
        {
            "match_id": row.get("match_id"),
            "is_home": row.get("is_home"),
            "opponent_club_id": row.get("opponent_club_id"),
            "opponent_name": row.get("opponent_name"),
            "club_attack": row.get("club_attack"),
            "club_defense": row.get("club_defense"),
            "opponent_attack": row.get("opponent_attack"),
            "opponent_defense": row.get("opponent_defense"),
            "odds_goals_for": row.get("odds_goals_for"),
            "odds_goals_against": row.get("odds_goals_against"),
            "odds_weight": row.get("odds_weight"),
            "odds_line": row.get("odds_line"),
        }
    ]


def forecast_event_model(row: dict[str, Any]) -> dict[str, Any]:
    """Interpretable event-based forecast for one feature row.

    Returns a dict with ``expected_points`` (the exact sum of ``components``),
    an ``uncertainty`` standard deviation, the per-match ``fixtures`` exposures
    the optimizer prices and the intermediate expectations. An unavailable
    player (``is_available`` false or ``p_appearance`` 0) scores a flat zero so
    it never enters a squad.

    A tour is a slice of the calendar rather than a round, so a club whose
    postponed match was re-attached here plays *twice* and its players are
    scored for both matches. Everything that is a property of the player — the
    per-90 rates, how likely he is to feature, how long he stays on — is shared
    between them; everything that is a property of the match — the opponent, the
    venue, the clean sheet — is computed per fixture and the results are added.
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

    # Per-match event counts, which do not depend on which match it is.
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
    # The three block rewards are paid per completed threshold, so each one is
    # the expected number of *whole* blocks rather than the count divided by the
    # block size.
    save_pts = scoring.save_per_three * expected_threshold_count(exp_saves, 3)
    recovery_pts = scoring.recovery_per_three * expected_threshold_count(
        exp_recoveries, 3
    )
    yellow_pts = scoring.yellow_card * exp_yellows

    playing = is_available and p_appearance > 0.0

    components = {key: 0.0 for key in _COMPONENT_KEYS}
    fixtures: list[dict[str, Any]] = []
    per_fixture: list[dict[str, Any]] = []
    variance = 0.0
    # ``shutout_stake`` prices what a goal against costs; the marginal rate is
    # the concession penalty per goal, which is the same in every match.
    conceded_per_goal = (
        (-scoring.conceded_per_two / 2.0) * p_appearance if playing else 0.0
    )

    for fixture in tour_fixtures(row):
        hist_for, hist_against = team_goal_means(
            float(fixture.get("club_attack") or 0.0),
            float(fixture.get("club_defense") or 0.0),
            float(fixture.get("opponent_attack") or 0.0),
            float(fixture.get("opponent_defense") or 0.0),
        )
        odds_for = fixture.get("odds_goals_for")
        odds_against = fixture.get("odds_goals_against")
        weight = float(fixture.get("odds_weight") or DEFAULT_ODDS_WEIGHT)
        goals_for, goals_against, attack_scale = blend_goal_means(
            hist_for,
            hist_against,
            None if odds_for is None else float(odds_for),
            None if odds_against is None else float(odds_against),
            weight=weight,
        )
        p_clean_sheet = clean_sheet_probability(goals_against)
        # A clean sheet only counts for a full appearance.
        clean_sheet_pts = scoring.clean_sheet * p_full * p_clean_sheet
        # The concession penalty is weighted by playing at all, since a player
        # who never comes on concedes nothing.
        conceded_pts = (
            scoring.conceded_per_two
            * expected_threshold_count(goals_against, 2)
            * p_appearance
        )
        # Attacking points follow the match scoring rate: a favourite against
        # a weak defence (priced or historical) scales goals/assists up, an
        # underdog scales them down. Appearance / cards / volume events stay
        # on the player's own rates.
        goal_pts = scoring.goal * exp_goals * attack_scale
        assist_pts = scoring.assist * exp_assists * attack_scale
        match = {
            "appearance": round(appearance_pts, 4),
            "goals": round(goal_pts, 4),
            "assists": round(assist_pts, 4),
            "clean_sheet": round(clean_sheet_pts, 4),
            "conceded": round(conceded_pts, 4),
            "saves": round(save_pts, 4),
            "recoveries": round(recovery_pts, 4),
            "yellow_cards": round(yellow_pts, 4),
        }
        if not playing:
            match = {key: 0.0 for key in match}
        for key, value in match.items():
            components[key] = round(components[key] + value, 4)

        # Fixture-linked exposures (step 16). ``goal_upside`` is the part of the
        # forecast that only materialises when the player's own club scores, and
        # ``shutout_stake`` is what the player forfeits per goal their opponent
        # scores: the clean sheet they lose plus the concession penalty. For two
        # players on opposite sides of one fixture, ``goal_upside *
        # shutout_stake`` (both ways round) is exactly the magnitude of the
        # covariance of their two forecasts under this Poisson goal model,
        # because the Poisson mean cancels out of
        # ``Cov(1{G=0}, G) = -P(G=0) * lambda`` and ``Var(G) = lambda``.
        fixtures.append(
            {
                "match_id": fixture.get("match_id"),
                "opponent_club_id": fixture.get("opponent_club_id"),
                "opponent_name": fixture.get("opponent_name"),
                "is_home": fixture.get("is_home"),
                "goal_upside": round(match["goals"] + match["assists"], 4),
                "shutout_stake": round(match["clean_sheet"] + conceded_per_goal, 4)
                if playing
                else 0.0,
            }
        )
        per_fixture.append(
            {
                "match_id": fixture.get("match_id"),
                "expected_points": round(sum(match.values()), 4),
                "team_goals_for": goals_for,
                "team_goals_against": goals_against,
                "clean_sheet_probability": round(p_clean_sheet, 4),
                "historical_goals_for": hist_for,
                "historical_goals_against": hist_against,
                "attack_scale": attack_scale,
                "odds_goals_for": (
                    None if odds_for is None else round(float(odds_for), 4)
                ),
                "odds_goals_against": (
                    None if odds_against is None else round(float(odds_against), 4)
                ),
            }
        )

        # Uncertainty: standard deviation from independent component variances.
        # Counts are treated as Poisson (Var = mean); the two Bernoulli terms
        # use p(1-p). This is a documented approximation, not a calibrated
        # interval. Matches are treated as independent, so variances add.
        if playing:
            variance += (
                scoring.goal**2 * exp_goals * attack_scale
                + scoring.assist**2 * exp_assists * attack_scale
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

    expected_points = round(sum(components.values()), 4)
    uncertainty = round(math.sqrt(max(0.0, variance)), 4) if playing else 0.0
    matches = len(fixtures)

    return {
        "expected_points": expected_points,
        "uncertainty": uncertainty,
        "components": components,
        "fixtures": fixtures,
        # The exposure of the tour as a whole, kept for readers that never had
        # to think about a club playing twice.
        "fixture": {
            "goal_upside": round(sum(f["goal_upside"] for f in fixtures), 4),
            "shutout_stake": round(sum(f["shutout_stake"] for f in fixtures), 4),
        },
        "expected": {
            "fixture_count": matches,
            "goals": round(exp_goals * matches, 4),
            "assists": round(exp_assists * matches, 4),
            "saves": round(exp_saves * matches, 4),
            "recoveries": round(exp_recoveries * matches, 4),
            "yellow_cards": round(exp_yellows * matches, 4),
            "team_goals_for": per_fixture[0]["team_goals_for"] if per_fixture else 0.0,
            "team_goals_against": (
                per_fixture[0]["team_goals_against"] if per_fixture else 0.0
            ),
            "clean_sheet_probability": (
                per_fixture[0]["clean_sheet_probability"] if per_fixture else 0.0
            ),
            "p_full_appearance": p_full,
            "p_sub_appearance": p_sub,
            "per_fixture": per_fixture,
        },
    }


def _fixture_count(row: dict[str, Any]) -> int:
    """How many matches of the target tour the row's club plays."""
    count = row.get("fixture_count")
    if count is not None:
        return max(0, int(count))
    return len(tour_fixtures(row))


def forecast_mean_baseline(row: dict[str, Any]) -> dict[str, Any]:
    """Baseline: season mean points per appearance scaled by play probability.

    The totals are the feature builder's blended ones, so early in a season they
    are fractional: last season's matches are still in there, at the weight the
    blend gives them. A club playing twice in the tour scores twice.
    """
    p_appearance = float(row.get("p_appearance") or 0.0)
    total_appearances = float(row.get("total_appearances") or 0.0)
    total_points = float(row.get("total_points") or 0.0)
    is_available = bool(row.get("is_available", True))
    matches = _fixture_count(row)

    mean_points = total_points / total_appearances if total_appearances else 0.0
    expected_points = mean_points * p_appearance * matches if is_available else 0.0
    return {
        "expected_points": round(expected_points, 4),
        "uncertainty": None,
        "components": {
            "mean_points_per_appearance": round(mean_points, 4),
            "p_appearance": round(p_appearance, 4),
            "fixture_count": matches,
        },
    }


def forecast_recent_baseline(row: dict[str, Any]) -> dict[str, Any]:
    """Baseline: mean points over the last 5 appearances times play probability."""
    p_appearance = float(row.get("p_appearance") or 0.0)
    points_avg_5 = float(row.get("points_avg_5") or 0.0)
    is_available = bool(row.get("is_available", True))
    matches = _fixture_count(row)

    expected_points = points_avg_5 * p_appearance * matches if is_available else 0.0
    return {
        "expected_points": round(expected_points, 4),
        "uncertainty": None,
        "components": {
            "points_avg_5": round(points_avg_5, 4),
            "p_appearance": round(p_appearance, 4),
            "fixture_count": matches,
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
    # A tour is a slice of the calendar, so a club can play twice in it. The
    # flat ``match_id`` above names the first of those matches; the list names
    # all of them.
    "tour_fixtures",
    "fixture_count",
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
            "params": {
                "expected": event["expected"],
                "fixture": event["fixture"],
                "fixtures": event["fixtures"],
            },
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
    double_fixture_rows = sum(
        1 for r in features["rows"] if (r.get("fixture_count") or 1) > 1
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
            "double_fixture_rows": double_fixture_rows,
        },
        "rows": rows,
    }


def build_forecast_dataset(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    season_ref: str | None = None,
    tour_ref: str | None = None,
    competition_ref: str | None = None,
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
            competition_ref=competition_ref,
            now=generated_at,
        )
    except Exception as error:  # noqa: BLE001 - normalise to a forecast error
        raise ForecastError(str(error)) from error
    with session_scope(session_factory) as session:
        attach_odds_to_features(session, features)
    return forecast_from_features(features, now=generated_at)


def run_forecast(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    season_ref: str | None = None,
    tour_ref: str | None = None,
    competition_ref: str | None = None,
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
        competition_ref=competition_ref,
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
    "reconstruct_points",
    "poisson_pmf",
    "clean_sheet_probability",
    "expected_threshold_count",
    "team_goal_means",
    "appearance_probabilities",
    "tour_fixtures",
    "forecast_event_model",
    "forecast_mean_baseline",
    "forecast_recent_baseline",
    "forecast_from_features",
    "build_forecast_dataset",
    "run_forecast",
]
