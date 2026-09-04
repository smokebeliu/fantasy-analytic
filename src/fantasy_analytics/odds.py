"""Match betting odds as a forecast input, not an optimizer constraint.

Sports.ru publishes a 1x2 line on the football calendar widget. This module
turns that line into expected goals (by inverting a Poisson match model) and
attaches them to the feature rows the event forecast already scores.

The two signals the squad cares about then fall out of the same Poisson
arithmetic the model already uses:

* a heavy favourite against a weak defence has a high scoring rate, so the
  attacking components (goals, assists) of its players go up;
* a side whose opponent is priced to score very little has a higher clean-sheet
  probability, so keepers and defenders pick up shutout points.

Nothing here is added to the CP-SAT objective: the optimizer keeps maximising
``expected_points``.
"""

from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import MatchOdds

# Weight of the market-implied Poisson means when they are blended with the
# historical venue strengths. 0.6 is enough for a mismatch (favourite vs weak
# defence, or a shutout price) to move expected points, without discarding the
# season-to-date rates when the line is noisy.
DEFAULT_ODDS_WEIGHT = 0.6

# Bound the attacking scale so a 20.0 home price cannot explode a striker's
# projection, and so a huge underdog is not zeroed out.
_ATTACK_SCALE_MIN = 0.5
_ATTACK_SCALE_MAX = 1.8
_HIST_GOALS_FLOOR = 0.05

# Poisson inversion search. Coarse then fine keeps the helper well under a
# millisecond per match while recovering λ to ~0.05.
_COARSE_STEP = 0.25
_COARSE_MAX = 4.0
_FINE_RADIUS = 0.4
_FINE_STEP = 0.05
_MAX_GOALS = 12


def implied_1x2(
    home: float, draw: float, away: float
) -> tuple[float, float, float]:
    """Convert decimal 1x2 odds into overround-free implied probabilities."""
    if min(home, draw, away) <= 1.0:
        raise ValueError("Decimal odds must be greater than 1")
    raw_home = 1.0 / home
    raw_draw = 1.0 / draw
    raw_away = 1.0 / away
    total = raw_home + raw_draw + raw_away
    if total <= 0:
        raise ValueError("Implied probabilities must be positive")
    return raw_home / total, raw_draw / total, raw_away / total


def _poisson_pmf(k: int, lam: float) -> float:
    """Probability of exactly ``k`` events for a Poisson mean ``lam``."""
    if lam < 0:
        raise ValueError("Poisson mean must be non-negative")
    if lam == 0:
        return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam**k / math.factorial(k)


def poisson_match_probs(
    lam_home: float, lam_away: float, *, max_goals: int = _MAX_GOALS
) -> tuple[float, float, float]:
    """P(home win, draw, away win) under independent Poisson goal counts."""
    if lam_home < 0 or lam_away < 0:
        raise ValueError("Poisson means must be non-negative")
    home_pmf = [_poisson_pmf(i, lam_home) for i in range(max_goals + 1)]
    away_pmf = [_poisson_pmf(j, lam_away) for j in range(max_goals + 1)]
    p_home = p_draw = p_away = 0.0
    for i, ph in enumerate(home_pmf):
        for j, pa in enumerate(away_pmf):
            p = ph * pa
            if i > j:
                p_home += p
            elif i == j:
                p_draw += p
            else:
                p_away += p
    return p_home, p_draw, p_away


def _search_means(
    p_home: float,
    p_draw: float,
    p_away: float,
    *,
    lo: float,
    hi: float,
    step: float,
) -> tuple[float, float, float]:
    """Return (λ_home, λ_away, squared error) on a rectangular grid."""
    best = (1.35, 1.15, math.inf)
    lam = lo
    while lam <= hi + 1e-12:
        other = lo
        while other <= hi + 1e-12:
            ph, pd, pa = poisson_match_probs(lam, other)
            err = (ph - p_home) ** 2 + (pd - p_draw) ** 2 + (pa - p_away) ** 2
            if err < best[2]:
                best = (lam, other, err)
            other = round(other + step, 10)
        lam = round(lam + step, 10)
    return best


def invert_poisson_means(
    p_home: float, p_draw: float, p_away: float
) -> tuple[float, float]:
    """Find Poisson (λ_home, λ_away) that best match a 1x2 probability triple.

    A coarse grid locates the basin; a finer grid around the winner recovers
    the means to the 0.05 that the forecast already rounds team goals to.
    """
    coarse_home, coarse_away, _ = _search_means(
        p_home,
        p_draw,
        p_away,
        lo=_COARSE_STEP,
        hi=_COARSE_MAX,
        step=_COARSE_STEP,
    )
    # The two means can sit far apart (a 1.20 favourite), so the fine window
    # covers a radius around each coarse winner.
    lo = min(
        max(_FINE_STEP, coarse_home - _FINE_RADIUS),
        max(_FINE_STEP, coarse_away - _FINE_RADIUS),
    )
    hi = max(
        min(_COARSE_MAX + _FINE_RADIUS, coarse_home + _FINE_RADIUS),
        min(_COARSE_MAX + _FINE_RADIUS, coarse_away + _FINE_RADIUS),
    )
    home, away, _ = _search_means(
        p_home, p_draw, p_away, lo=lo, hi=hi, step=_FINE_STEP
    )
    return round(home, 4), round(away, 4)


def line_to_expected_goals(
    home_odds: float, draw_odds: float, away_odds: float
) -> dict[str, float]:
    """Turn a decimal 1x2 line into implied probabilities and Poisson means."""
    implied_home, implied_draw, implied_away = implied_1x2(
        home_odds, draw_odds, away_odds
    )
    exp_home, exp_away = invert_poisson_means(
        implied_home, implied_draw, implied_away
    )
    return {
        "home_odds": float(home_odds),
        "draw_odds": float(draw_odds),
        "away_odds": float(away_odds),
        "implied_home": round(implied_home, 6),
        "implied_draw": round(implied_draw, 6),
        "implied_away": round(implied_away, 6),
        "expected_home_goals": exp_home,
        "expected_away_goals": exp_away,
    }


def blend_goal_means(
    historical_for: float,
    historical_against: float,
    odds_for: float | None,
    odds_against: float | None,
    *,
    weight: float = DEFAULT_ODDS_WEIGHT,
) -> tuple[float, float, float]:
    """Blend historical and market Poisson means.

    Returns ``(goals_for, goals_against, attack_scale)``. ``attack_scale`` is
    1.0 when there is no line, so a tour without odds keeps the historical
    event model bit-identical.
    """
    hist_for = max(0.0, float(historical_for))
    hist_against = max(0.0, float(historical_against))
    if odds_for is None or odds_against is None:
        return round(hist_for, 4), round(hist_against, 4), 1.0
    w = min(max(float(weight), 0.0), 1.0)
    goals_for = (1.0 - w) * hist_for + w * max(0.0, float(odds_for))
    goals_against = (1.0 - w) * hist_against + w * max(0.0, float(odds_against))
    if hist_for <= _HIST_GOALS_FLOOR:
        scale = 1.0
    else:
        scale = min(_ATTACK_SCALE_MAX, max(_ATTACK_SCALE_MIN, goals_for / hist_for))
    return round(goals_for, 4), round(goals_against, 4), round(scale, 4)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _apply_odds_row(fixture: dict[str, Any], odds: Any) -> None:
    """Stamp one fixture (or a flattened feature row) with club-perspective odds."""
    is_home = bool(fixture.get("is_home"))
    home_goals = _as_float(odds.expected_home_goals)
    away_goals = _as_float(odds.expected_away_goals)
    if home_goals is None or away_goals is None:
        return
    if is_home:
        fixture["odds_goals_for"] = home_goals
        fixture["odds_goals_against"] = away_goals
    else:
        fixture["odds_goals_for"] = away_goals
        fixture["odds_goals_against"] = home_goals
    fixture["odds_weight"] = DEFAULT_ODDS_WEIGHT
    fixture["odds_line"] = {
        "home": _as_float(odds.home_odds),
        "draw": _as_float(odds.draw_odds),
        "away": _as_float(odds.away_odds),
        "implied_home": _as_float(odds.implied_home),
        "implied_draw": _as_float(odds.implied_draw),
        "implied_away": _as_float(odds.implied_away),
        "bookmaker": odds.bookmaker,
    }


def attach_odds_to_features(
    session: Session, features: dict[str, Any], *, weight: float | None = None
) -> int:
    """Fill ``tour_fixtures`` with odds-implied goals from the stored lines.

    Mutates ``features`` in place and returns how many fixtures received a
    line. A tour with no stored odds is left untouched, so recomputing a
    snapshot before the day-before refresh stays bit-identical to 1.2.0.

    The line used is the latest capture in ``match_odds_history`` taken
    before the dataset's cutoff (step 23), so a replayed tour sees only what
    was known at its deadline; ``match_odds`` (the latest line, captured for a
    fixture still to be played) fills in for fixtures without history.
    """
    season_id = features.get("season_id")
    if season_id is None:
        return 0
    cutoff_text = features.get("cutoff")
    cutoff = datetime.fromisoformat(cutoff_text) if cutoff_text else None
    by_match: dict[int, Any] = {}
    if cutoff is not None:
        from .db.odds_repository import OddsRepository

        by_match.update(OddsRepository(session).history_before(int(season_id), cutoff))
    rows = list(
        session.execute(
            select(MatchOdds).where(
                MatchOdds.season_id == int(season_id),
                MatchOdds.match_id.is_not(None),
            )
        ).scalars()
    )
    for row in rows:
        if row.match_id is None or int(row.match_id) in by_match:
            continue
        # The latest line stands in where no history predates the cutoff. It
        # is captured for a fixture still to be played, so for the live
        # forecast it is exactly what the refresh just fetched; only the
        # history can make a *replayed* tour honest.
        by_match[int(row.match_id)] = row
    if not by_match:
        return 0
    attached = 0
    for feature_row in features.get("rows") or []:
        fixtures = feature_row.get("tour_fixtures")
        targets: Iterable[dict[str, Any]]
        if fixtures:
            targets = fixtures
        else:
            targets = (feature_row,)
        for fixture in targets:
            match_id = fixture.get("match_id")
            if match_id is None:
                continue
            odds = by_match.get(int(match_id))
            if odds is None:
                continue
            _apply_odds_row(fixture, odds)
            if weight is not None:
                fixture["odds_weight"] = float(weight)
            attached += 1
    features["odds_fixtures"] = attached
    return attached


def parse_line1x2(payload: Mapping[str, Any] | None) -> dict[str, float] | None:
    """Read ``line1x2 { h x a }`` off a ``bettingOdds`` entry, or ``None``."""
    if not isinstance(payload, Mapping):
        return None
    line = payload.get("line1x2") or {}
    if not isinstance(line, Mapping):
        return None
    try:
        home = float(line["h"])
        draw = float(line["x"])
        away = float(line["a"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(home, draw, away) <= 1.0:
        return None
    parsed = line_to_expected_goals(home, draw, away)
    bookmaker = payload.get("bookmaker") or {}
    lead = None
    if isinstance(bookmaker, Mapping):
        lead = bookmaker.get("lead")
    parsed["bookmaker"] = str(lead) if lead else None
    return parsed


__all__ = [
    "DEFAULT_ODDS_WEIGHT",
    "attach_odds_to_features",
    "blend_goal_means",
    "implied_1x2",
    "invert_poisson_means",
    "line_to_expected_goals",
    "parse_line1x2",
    "poisson_match_probs",
]
