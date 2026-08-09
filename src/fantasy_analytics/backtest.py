"""Walk-forward backtesting and model comparison (development-plan step 19).

The forecast (step 7) and the optimizer (step 8) are only worth trusting if they
would have helped on tours that have already been played. This module replays a
finished season tour by tour and answers three questions:

* **Is the forecast accurate?** For every tour it rebuilds the leakage-free
  feature dataset at that tour's cutoff, forecasts every model, and compares the
  projection against the fantasy points the player actually scored. Errors are
  reported per tour and per position (MAE/RMSE/bias).
* **Does it pick a better squad?** For every tour it solves the real squad
  problem on each model's projections and scores the produced eleven with the
  *actual* points, next to a hindsight optimum (the best squad the tour allowed)
  and the best eleven that could have been fielded from the same 15 players. The
  captain choice and the bench are scored separately.
* **Is the main model better than a simple rule?** The event model is compared
  against the two baselines (season mean and recent form) on both accuracy and
  squad points, and the run ends with an explicit decision.

Two guarantees make the answers meaningful:

* **No future data.** Every tour is evaluated strictly from its own cutoff. This
  is not merely trusted: :func:`audit_tour` independently recomputes each row's
  history totals from the raw appearance table and reports any row that could
  only have been produced with post-cutoff data, plus any tour whose cutoff is
  not before its first kickoff.
* **Reproducible.** The whole run is a pure function of
  ``(run_id, tours, models, versions, fixture_conflict_weight)``; the CLI writes
  those parameters next to the results, and the solver is deterministic.

The module only reads the database and never calls the Sports.ru API.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .db import session_scope
from .db.models import FantasyTour, Match, PlayerMatchStats, PlayerSeason, Season
from .features import (
    FEATURE_VERSION,
    STAT_SOURCE_CURRENT,
    Appearance,
    build_feature_dataset,
    load_appearances,
    recent_before_cutoff,
    resolve_run,
)
from .forecast import (
    MODEL_EVENT,
    MODEL_MEAN,
    MODEL_RECENT,
    MODEL_VERSION,
    SCORING_VERSION,
    forecast_from_features,
)
from .optimizer import (
    OPTIMIZER_VERSION,
    ROLES,
    Candidate,
    OptimizerError,
    SquadRules,
    candidates_from_forecast,
    load_squad_rules,
    solve_squad,
    validate_squad,
)

# Bumped whenever the backtest procedure or its metrics change, so results from
# different code revisions are never compared as if they were the same run.
BACKTEST_VERSION = "1.0.0"

# The model under test and the baselines it must beat to be adopted.
DEFAULT_MODELS: tuple[str, ...] = (MODEL_EVENT, MODEL_MEAN, MODEL_RECENT)
PRIMARY_MODEL = MODEL_EVENT

# Decisions the run can conclude with.
ACCEPT_MODEL = "accept_model"
REVISE_MODEL = "revise_model"
KEEP_BASELINE = "keep_baseline"

# Feature columns that are identifiers, labels or timestamps rather than signals;
# they are excluded from the instability ranking.
_NON_SIGNAL_FEATURES = frozenset(
    {
        "player_season_id",
        "fantasy_player_id",
        "club_id",
        "opponent_club_id",
        "match_id",
        "feature_version",
        "is_home",
        "is_available",
        "is_newcomer",
        "has_history",
    }
)


class BacktestError(RuntimeError):
    """Raised when a backtest cannot be run (no run, season or played tour)."""


@dataclass(frozen=True)
class TourRef:
    """A tour of the backtested season, in chronological order."""

    tour_id: int
    fantasy_tour_id: str
    name: str
    status: str
    starts_at: datetime | None


# ---------------------------------------------------------------------------
# Pure metrics.
# ---------------------------------------------------------------------------


def error_metrics(pairs: Sequence[tuple[float, float]]) -> dict[str, Any]:
    """Accuracy of ``(predicted, actual)`` pairs.

    ``bias`` is the mean signed error (positive = the model over-predicts), which
    separates "wrong by a lot" from "systematically too optimistic".
    """
    if not pairs:
        return {
            "n": 0,
            "mae": None,
            "rmse": None,
            "bias": None,
            "mean_predicted": None,
            "mean_actual": None,
        }
    errors = [predicted - actual for predicted, actual in pairs]
    n = len(pairs)
    return {
        "n": n,
        "mae": round(sum(abs(error) for error in errors) / n, 4),
        "rmse": round(math.sqrt(sum(error**2 for error in errors) / n), 4),
        "bias": round(sum(errors) / n, 4),
        "mean_predicted": round(sum(p for p, _ in pairs) / n, 4),
        "mean_actual": round(sum(a for _, a in pairs) / n, 4),
    }


def role_metrics(
    rows: Sequence[tuple[str, float, float]],
) -> dict[str, dict[str, Any]]:
    """Per-position accuracy from ``(role, predicted, actual)`` triples."""
    by_role: dict[str, list[tuple[float, float]]] = {role: [] for role in ROLES}
    for role, predicted, actual in rows:
        by_role.setdefault(role, []).append((predicted, actual))
    return {role: error_metrics(pairs) for role, pairs in by_role.items()}


def _pooled(metrics: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Combine per-tour metrics into one number set, weighted by row count.

    MAE pools linearly and RMSE pools through the mean squared error, so the
    result equals what a single pass over all rows would have produced.
    """
    total = 0
    abs_sum = 0.0
    sq_sum = 0.0
    signed_sum = 0.0
    predicted_sum = 0.0
    actual_sum = 0.0
    for entry in metrics:
        n = int(entry.get("n") or 0)
        if n == 0 or entry.get("mae") is None:
            continue
        total += n
        abs_sum += float(entry["mae"]) * n
        sq_sum += float(entry["rmse"]) ** 2 * n
        signed_sum += float(entry["bias"]) * n
        predicted_sum += float(entry["mean_predicted"]) * n
        actual_sum += float(entry["mean_actual"]) * n
    if total == 0:
        return error_metrics([])
    return {
        "n": total,
        "mae": round(abs_sum / total, 4),
        "rmse": round(math.sqrt(sq_sum / total), 4),
        "bias": round(signed_sum / total, 4),
        "mean_predicted": round(predicted_sum / total, 4),
        "mean_actual": round(actual_sum / total, 4),
    }


def best_eleven_points(
    players: Sequence[tuple[str, float]], rules: SquadRules
) -> float:
    """Highest actual points any legal eleven from these 15 players could score.

    Within a position the best scorers are always preferable, so the optimum is
    found by enumerating the legal role distributions and taking the top
    performers of each role. It measures the *lineup* decision separately from
    the squad decision: the same 15 players could have scored this much.
    """
    by_role: dict[str, list[float]] = {role: [] for role in ROLES}
    for role, points in players:
        by_role.setdefault(role, []).append(points)
    for values in by_role.values():
        values.sort(reverse=True)

    ranges: dict[str, range] = {}
    for role in ROLES:
        low, high = rules.starting_limits.get(role, (0, rules.starting_players))
        high = min(high, len(by_role.get(role, [])))
        ranges[role] = range(low, high + 1)

    best: float | None = None
    for gk in ranges["GOALKEEPER"]:
        for df in ranges["DEFENDER"]:
            for mf in ranges["MIDFIELDER"]:
                fw = rules.starting_players - gk - df - mf
                if fw not in ranges["FORWARD"]:
                    continue
                counts = {
                    "GOALKEEPER": gk,
                    "DEFENDER": df,
                    "MIDFIELDER": mf,
                    "FORWARD": fw,
                }
                total = sum(
                    sum(by_role[role][: counts[role]]) for role in ROLES
                )
                if best is None or total > best:
                    best = total
    return round(best or 0.0, 4)


# ---------------------------------------------------------------------------
# Cutoff audit (does not trust the feature builder).
# ---------------------------------------------------------------------------


def audit_tour(
    features: dict[str, Any],
    *,
    appearances: Mapping[int, list[Appearance]],
    tour_match_ids: frozenset[int],
    first_kickoff: datetime | None,
) -> dict[str, Any]:
    """Verify a tour's dataset could not have seen the tour it predicts.

    The decisive check is a *recomputation*: every row's history totals must be
    exactly reproducible from the raw appearance table restricted to matches
    before the cutoff and outside the tour, so a single leaked match changes
    ``total_points`` and is reported as a violation.

    A cutoff that falls after the tour's own first kickoff is reported as a
    warning rather than a violation: the source occasionally dates a transfer
    deadline late, but the feature builder also excludes the target tour's
    matches by id, so no post-kickoff data can enter the history anyway — which
    is precisely what the recomputation above proves.

    Cross-season rows (a not-yet-started season sourcing the previous one) are
    counted but not recomputed: their history comes from another season's run.
    """
    cutoff = datetime.fromisoformat(features["cutoff"])
    violations: list[str] = []
    warnings: list[str] = []

    if first_kickoff is not None and cutoff > first_kickoff:
        warnings.append(
            f"cutoff {cutoff.isoformat()} is after the tour's first kickoff "
            f"{first_kickoff.isoformat()}; the tour's own matches are excluded "
            f"by id, so the history stays clean"
        )

    checked = 0
    skipped = 0
    for row in features["rows"]:
        if row.get("stat_source") != STAT_SOURCE_CURRENT:
            skipped += 1
            continue
        checked += 1
        history = recent_before_cutoff(
            appearances.get(row["player_season_id"], []),
            cutoff,
            exclude_match_ids=tour_match_ids,
        )
        expected = {
            "total_appearances": len(history),
            "total_minutes": sum(item.minutes for item in history),
            "total_points": sum(item.points for item in history),
        }
        for key, value in expected.items():
            if row.get(key) != value:
                violations.append(
                    f"player {row['player_season_id']} reports {key}="
                    f"{row.get(key)} but only {value} is available before the "
                    f"cutoff"
                )

    return {
        "cutoff": features["cutoff"],
        "first_kickoff": first_kickoff.isoformat() if first_kickoff else None,
        "rows_checked": checked,
        "rows_cross_season": skipped,
        "violations": violations,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Squad simulation.
# ---------------------------------------------------------------------------


def _actual_of(actuals: Mapping[int, float], player_season_id: int) -> float:
    """Actual points of a player; an absent row means they did not play (0)."""
    return float(actuals.get(player_season_id, 0.0))


def simulate_squad(
    candidates: Sequence[Candidate],
    rules: SquadRules,
    actuals: Mapping[int, float],
    *,
    fixture_conflict_weight: float | None = None,
) -> dict[str, Any]:
    """Solve a tour's squad on projections and score it with the real points.

    Returns the projected and the realised points of the same squad, so the
    difference is exactly what the model cost or gained. The lineup and captain
    decisions are scored separately:

    * ``best_eleven_actual`` is the most the chosen 15 could have scored, so
      ``lineup_efficiency`` isolates the starting-eleven choice;
    * ``captain.was_best_starter`` says whether the doubled player was in fact
      the eleven's top scorer.
    """
    solution = solve_squad(
        candidates, rules, fixture_conflict_weight=fixture_conflict_weight
    )
    violations = validate_squad(solution, rules)
    if violations:
        raise BacktestError(
            "Backtest produced an invalid squad: " + "; ".join(violations)
        )

    squad = solution["squad"]
    starters = [player for player in squad if player.get("is_starter")]
    bench = [player for player in squad if not player.get("is_starter")]
    captain = solution["captain"]

    starting_actual = round(
        sum(_actual_of(actuals, player["player_season_id"]) for player in starters), 4
    )
    captain_actual = _actual_of(actuals, captain["player_season_id"])
    bench_actual = round(
        sum(_actual_of(actuals, player["player_season_id"]) for player in bench), 4
    )
    best_starter_actual = max(
        (_actual_of(actuals, player["player_season_id"]) for player in starters),
        default=0.0,
    )
    best_eleven = best_eleven_points(
        [
            (player["role"], _actual_of(actuals, player["player_season_id"]))
            for player in squad
        ],
        rules,
    )

    return {
        "formation": solution["formation"],
        "total_price": solution["total_price"],
        "projected_points": solution["objective_expected_points"],
        "projected_starting_points": solution["starting_expected_points"],
        "actual_points": round(starting_actual + captain_actual, 4),
        "actual_starting_points": starting_actual,
        "bench_actual_points": bench_actual,
        "best_eleven_actual_points": best_eleven,
        "lineup_efficiency": (
            round(starting_actual / best_eleven, 4) if best_eleven > 0 else None
        ),
        "captain": {
            "player_season_id": captain["player_season_id"],
            "player_name": captain["player_name"],
            "role": captain["role"],
            "projected_points": captain["expected_points"],
            "actual_points": round(captain_actual, 4),
            "best_starter_actual_points": round(best_starter_actual, 4),
            "was_best_starter": bool(
                starters and captain_actual >= best_starter_actual - 1e-9
            ),
        },
        "squad": [
            {
                "player_season_id": player["player_season_id"],
                "player_name": player["player_name"],
                "role": player["role"],
                "club_name": player["club_name"],
                "price": player["price"],
                "projected_points": player["expected_points"],
                "actual_points": round(
                    _actual_of(actuals, player["player_season_id"]), 4
                ),
                "is_starter": bool(player.get("is_starter")),
                "is_captain": bool(player.get("is_captain")),
            }
            for player in squad
        ],
    }


def hindsight_squad(
    candidates: Sequence[Candidate],
    rules: SquadRules,
    actuals: Mapping[int, float],
) -> dict[str, Any]:
    """The best squad the tour allowed, solved on the *actual* points.

    Optimising the same integer program with hindsight gives the ceiling every
    model is measured against, which turns "42 points" into "42 of a possible
    78". The schedule penalty is switched off because with known outcomes there
    is nothing left to hedge.
    """
    perfect = [
        replace(
            candidate,
            expected_points=_actual_of(actuals, candidate.player_season_id),
            goal_upside=0.0,
            shutout_stake=0.0,
        )
        for candidate in candidates
    ]
    solution = solve_squad(perfect, rules, fixture_conflict_weight=0)
    return {
        "formation": solution["formation"],
        "actual_points": solution["objective_expected_points"],
        "actual_starting_points": solution["starting_expected_points"],
        "captain": {
            "player_season_id": solution["captain"]["player_season_id"],
            "player_name": solution["captain"]["player_name"],
            "actual_points": solution["captain"]["expected_points"],
        },
    }


# ---------------------------------------------------------------------------
# Feature stability.
# ---------------------------------------------------------------------------


def feature_instability(
    per_tour_rows: Sequence[Sequence[dict[str, Any]]], *, top: int = 10
) -> list[dict[str, Any]]:
    """Rank numeric features by how much they move between consecutive tours.

    ``volatility`` is the mean absolute tour-to-tour change of a player's value
    divided by the feature's own standard deviation, so it is comparable across
    features with different units: a value near 0 means the feature is stable
    (it mostly separates players), while a value above 1 means a player's own
    value jumps more than players differ from each other, which makes the
    feature a weak signal for the next tour.
    """
    names: list[str] = []
    values: dict[str, list[float]] = {}
    changes: dict[str, list[float]] = {}
    previous: dict[int, dict[str, float]] = {}

    for rows in per_tour_rows:
        current: dict[int, dict[str, float]] = {}
        for row in rows:
            numeric = {
                key: float(value)
                for key, value in row.items()
                if key not in _NON_SIGNAL_FEATURES
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            }
            current[row["player_season_id"]] = numeric
            for key, value in numeric.items():
                if key not in values:
                    values[key] = []
                    changes[key] = []
                    names.append(key)
                values[key].append(value)
            before = previous.get(row["player_season_id"])
            if before is None:
                continue
            for key, value in numeric.items():
                if key in before:
                    changes[key].append(abs(value - before[key]))
        previous = current

    ranking: list[dict[str, Any]] = []
    for name in names:
        observed = values[name]
        if len(observed) < 2:
            continue
        mean = sum(observed) / len(observed)
        variance = sum((value - mean) ** 2 for value in observed) / len(observed)
        std = math.sqrt(variance)
        deltas = changes[name]
        mean_change = sum(deltas) / len(deltas) if deltas else 0.0
        if std <= 0 or not deltas:
            continue
        ranking.append(
            {
                "feature": name,
                "mean": round(mean, 4),
                "std": round(std, 4),
                "mean_abs_change": round(mean_change, 4),
                "volatility": round(mean_change / std, 4),
                "transitions": len(deltas),
            }
        )
    ranking.sort(key=lambda item: (-item["volatility"], item["feature"]))
    return ranking[:top]


# ---------------------------------------------------------------------------
# Verdict.
# ---------------------------------------------------------------------------


# The criteria the main model is judged on, as
# ``(key, label, "lower"|"higher", extractor path)``. Accuracy is judged twice on
# purpose: over players who actually played (where a wrong projection costs real
# points) and over every selectable player (where correctly predicting a zero for
# a player who never appeared also matters).
_CRITERIA: tuple[tuple[str, str, str, tuple[str, str]], ...] = (
    ("mae_played", "accuracy on players who played", "lower", ("metrics_played", "mae")),
    ("mae_all", "accuracy on every selectable player", "lower", ("metrics", "mae")),
    ("squad_points", "realised squad points", "higher", ("squad", "actual_points_total")),
)


def _criterion_value(
    summary: Mapping[str, Any], path: tuple[str, str]
) -> float | None:
    section = summary.get(path[0]) or {}
    value = section.get(path[1])
    return None if value is None else float(value)


def decide(models: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """Turn the comparison into an explicit decision about the main model.

    The main model is adopted only when it beats the best baseline on *every*
    criterion that could be measured (see :data:`_CRITERIA`): it must be more
    accurate both on players who played and across the whole selectable pool, and
    its squads must have scored at least as many real points. Winning some
    criteria is a "revise" signal and the reason names exactly which ones were
    lost; winning none means a baseline is the better default.
    """
    primary = models.get(PRIMARY_MODEL)
    baselines = {
        name: summary for name, summary in models.items() if name != PRIMARY_MODEL
    }
    if primary is None or not baselines:
        return {
            "decision": REVISE_MODEL,
            "reason": "not enough models were evaluated to compare against a baseline",
            "primary_model": PRIMARY_MODEL,
            "criteria": [],
        }

    criteria: list[dict[str, Any]] = []
    for key, label, direction, path in _CRITERIA:
        primary_value = _criterion_value(primary, path)
        rated = [
            (name, _criterion_value(summary, path))
            for name, summary in baselines.items()
            if _criterion_value(summary, path) is not None
        ]
        if primary_value is None or not rated:
            continue
        chooser = min if direction == "lower" else max
        best_name, best_value = chooser(rated, key=lambda item: item[1])
        won = (
            primary_value <= best_value
            if direction == "lower"
            else primary_value >= best_value
        )
        criteria.append(
            {
                "criterion": key,
                "label": label,
                "better": direction,
                "primary": round(primary_value, 4),
                "best_baseline": round(best_value, 4),
                "best_baseline_model": best_name,
                "won": won,
            }
        )

    if not criteria:
        return {
            "decision": REVISE_MODEL,
            "reason": "no comparable metrics were produced",
            "primary_model": PRIMARY_MODEL,
            "criteria": [],
        }

    won = [item for item in criteria if item["won"]]
    lost = [item for item in criteria if not item["won"]]

    def _phrase(items: Sequence[dict[str, Any]]) -> str:
        return "; ".join(
            f"{item['label']} ({item['primary']} vs {item['best_baseline']} "
            f"for {item['best_baseline_model']})"
            for item in items
        )

    if not lost:
        decision = ACCEPT_MODEL
        reason = f"{PRIMARY_MODEL} beats every baseline on {_phrase(won)}"
    elif not won:
        decision = KEEP_BASELINE
        reason = f"baselines win on every criterion: {_phrase(lost)}"
    else:
        decision = REVISE_MODEL
        reason = (
            f"{PRIMARY_MODEL} wins on {_phrase(won)} but loses on {_phrase(lost)}"
        )

    return {
        "decision": decision,
        "reason": reason,
        "primary_model": PRIMARY_MODEL,
        "criteria": criteria,
    }


# ---------------------------------------------------------------------------
# Database loading.
# ---------------------------------------------------------------------------


def _load_tours(session, season_id: int) -> list[TourRef]:
    rows = session.execute(
        select(FantasyTour)
        .where(FantasyTour.season_id == season_id)
        .order_by(FantasyTour.starts_at.is_(None), FantasyTour.starts_at, FantasyTour.id)
    ).scalars()
    return [
        TourRef(
            tour_id=tour.id,
            fantasy_tour_id=tour.fantasy_tour_id,
            name=tour.name,
            status=tour.status or "",
            starts_at=tour.starts_at,
        )
        for tour in rows
    ]


def _load_tour_matches(session, season_id: int) -> dict[int, list[tuple[int, datetime]]]:
    rows = session.execute(
        select(Match.tour_id, Match.id, Match.scheduled_at).where(
            Match.season_id == season_id
        )
    ).all()
    by_tour: dict[int, list[tuple[int, datetime]]] = {}
    for tour_id, match_id, scheduled_at in rows:
        if tour_id is None:
            continue
        by_tour.setdefault(tour_id, []).append((match_id, scheduled_at))
    return by_tour


def _load_actual_points(
    session, season_id: int, run_id: int
) -> tuple[dict[int, dict[int, float]], dict[int, set[int]]]:
    """Actual fantasy points per tour per player from the snapshot's own stats.

    Also returns who actually took the field in each tour, which separates "the
    model was wrong about a player who played" from the far easier "the model
    correctly gave zero to a player who never appeared".
    """
    rows = session.execute(
        select(
            PlayerMatchStats.tour_id,
            PlayerMatchStats.player_season_id,
            PlayerMatchStats.points,
            PlayerMatchStats.field_minutes,
        )
        .join(PlayerSeason, PlayerMatchStats.player_season_id == PlayerSeason.id)
        .where(
            PlayerSeason.season_id == season_id,
            PlayerMatchStats.ingestion_run_id == run_id,
        )
    ).all()
    by_tour: dict[int, dict[int, float]] = {}
    played: dict[int, set[int]] = {}
    for tour_id, player_season_id, points, minutes in rows:
        bucket = by_tour.setdefault(tour_id, {})
        # A player can appear twice in a tour only if the source duplicates a
        # fixture; summing keeps the total truthful either way.
        bucket[player_season_id] = bucket.get(player_season_id, 0.0) + float(points)
        if minutes and minutes > 0:
            played.setdefault(tour_id, set()).add(player_season_id)
    return by_tour, played


# ---------------------------------------------------------------------------
# The backtest itself.
# ---------------------------------------------------------------------------


def _tour_payload(tour: TourRef) -> dict[str, Any]:
    return {
        "tour_id": tour.tour_id,
        "fantasy_tour_id": tour.fantasy_tour_id,
        "name": tour.name,
        "status": tour.status,
        "starts_at": tour.starts_at.isoformat() if tour.starts_at else None,
    }


def _select_tours(
    tours: Sequence[TourRef],
    tour_refs: Sequence[str] | None,
    actual_points: Mapping[int, dict[int, float]],
) -> tuple[list[TourRef], list[dict[str, Any]]]:
    """Pick the tours to replay, recording why the others were left out.

    A tour is only backtestable when the snapshot knows what actually happened in
    it, so tours without per-player stats (unplayed, or never imported) are
    skipped explicitly instead of silently scoring everyone zero.
    """
    if tour_refs:
        wanted = {str(ref) for ref in tour_refs}
        matched = [
            tour
            for tour in tours
            if tour.fantasy_tour_id in wanted or tour.name in wanted
        ]
        unknown = wanted - {tour.fantasy_tour_id for tour in matched} - {
            tour.name for tour in matched
        }
        if unknown:
            raise BacktestError(
                "Unknown tour(s): " + ", ".join(sorted(unknown))
            )
        pool = matched
    else:
        pool = list(tours)

    selected: list[TourRef] = []
    skipped: list[dict[str, Any]] = []
    for tour in pool:
        if not actual_points.get(tour.tour_id):
            skipped.append(
                _tour_payload(tour) | {"reason": "no actual player stats for the tour"}
            )
            continue
        selected.append(tour)
    return selected, skipped


def run_backtest(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    season_ref: str | None = None,
    tour_refs: Sequence[str] | None = None,
    models: Sequence[str] = DEFAULT_MODELS,
    optimize: bool = True,
    fixture_conflict_weight: float | None = None,
    top_errors: int = 20,
    top_unstable: int = 10,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replay a season tour by tour and compare the models.

    Returns a JSON-serialisable report: the run parameters, the cutoff audit, per
    tour and pooled accuracy (overall and per position), the simulated squads
    with their realised points, the largest individual errors, the least stable
    features and the resulting decision.
    """
    generated_at = now or datetime.now(UTC)
    if not models:
        raise BacktestError("At least one model must be backtested")

    with session_scope(session_factory) as session:
        run = resolve_run(session, run_id, season_ref)
        resolved_run_id = run.id
        season_id = run.season_id
        season = session.get(Season, season_id)
        season_payload = {
            "fantasy_id": season.fantasy_season_id if season else None,
            "name": season.name if season else None,
        }
        tours = _load_tours(session, season_id)
        tour_matches = _load_tour_matches(session, season_id)
        actual_points, played_players = _load_actual_points(
            session, season_id, resolved_run_id
        )
        appearances = load_appearances(session, season_id, resolved_run_id)

    if not tours:
        raise BacktestError(f"Season {season_id} has no tours")

    selected, skipped = _select_tours(tours, tour_refs, actual_points)
    if not selected:
        raise BacktestError(
            "No tour with imported player statistics to backtest; import a "
            "finished season first"
        )

    audits: list[dict[str, Any]] = []
    tour_reports: list[dict[str, Any]] = []
    feature_history: list[list[dict[str, Any]]] = []
    per_model_tour_metrics: dict[str, list[dict[str, Any]]] = {
        model: [] for model in models
    }
    per_model_tour_played: dict[str, list[dict[str, Any]]] = {
        model: [] for model in models
    }
    per_model_role_rows: dict[str, list[tuple[str, float, float]]] = {
        model: [] for model in models
    }
    per_model_squads: dict[str, list[dict[str, Any]]] = {model: [] for model in models}
    all_errors: list[dict[str, Any]] = []

    rules_cache: dict[int, SquadRules] = {}

    for tour in selected:
        features = build_feature_dataset(
            session_factory,
            run_id=resolved_run_id,
            tour_ref=tour.fantasy_tour_id,
            now=generated_at,
        )
        forecast = forecast_from_features(features, now=generated_at)
        matches = tour_matches.get(tour.tour_id, [])
        audit = audit_tour(
            features,
            appearances=appearances,
            tour_match_ids=frozenset(match_id for match_id, _ in matches),
            first_kickoff=min((kickoff for _, kickoff in matches), default=None),
        )
        audits.append(_tour_payload(tour) | audit)
        feature_history.append(features["rows"])

        actuals = actual_points.get(tour.tour_id, {})
        played = played_players.get(tour.tour_id, set())
        rows_by_model: dict[str, list[dict[str, Any]]] = {model: [] for model in models}
        for row in forecast["rows"]:
            if row["model_name"] in rows_by_model:
                rows_by_model[row["model_name"]].append(row)

        if optimize:
            if tour.tour_id not in rules_cache:
                with session_scope(session_factory) as session:
                    rules_cache[tour.tour_id] = load_squad_rules(
                        session, season_id, tour.tour_id
                    )
            rules = rules_cache[tour.tour_id]
        else:
            rules = None

        tour_models: dict[str, Any] = {}
        for model in models:
            pairs: list[tuple[float, float]] = []
            played_pairs: list[tuple[float, float]] = []
            triples: list[tuple[str, float, float]] = []
            for row in rows_by_model[model]:
                predicted = float(row["expected_points"] or 0.0)
                actual = _actual_of(actuals, row["player_season_id"])
                pairs.append((predicted, actual))
                triples.append((row["role"], predicted, actual))
                if row["player_season_id"] in played:
                    played_pairs.append((predicted, actual))
                if model == PRIMARY_MODEL:
                    all_errors.append(
                        {
                            "tour": tour.name,
                            "fantasy_tour_id": tour.fantasy_tour_id,
                            "player_season_id": row["player_season_id"],
                            "player_name": row["player_name"],
                            "role": row["role"],
                            "club_name": row["club_name"],
                            "predicted": round(predicted, 4),
                            "actual": round(actual, 4),
                            "error": round(predicted - actual, 4),
                        }
                    )

            metrics = error_metrics(pairs)
            metrics_played = error_metrics(played_pairs)
            per_model_tour_metrics[model].append(metrics)
            per_model_tour_played[model].append(metrics_played)
            per_model_role_rows[model].extend(triples)
            entry: dict[str, Any] = {
                "metrics": metrics,
                "metrics_played": metrics_played,
                "by_role": role_metrics(triples),
            }

            if optimize and rules is not None:
                candidates = candidates_from_forecast(forecast["rows"], model)
                if candidates:
                    try:
                        squad = simulate_squad(
                            candidates,
                            rules,
                            actuals,
                            fixture_conflict_weight=fixture_conflict_weight,
                        )
                    except OptimizerError as error:
                        squad = {"error": str(error)}
                    else:
                        per_model_squads[model].append(squad)
                    entry["squad"] = squad
                else:
                    entry["squad"] = {"error": "no priced candidates for the tour"}
            tour_models[model] = entry

        tour_report: dict[str, Any] = {
            **_tour_payload(tour),
            "cutoff": features["cutoff"],
            "counts": {
                "players": features["counts"]["rows"],
                "fixtures": features["counts"]["fixtures"],
                "players_with_actuals": len(actuals),
                "players_who_played": len(played),
            },
            "models": tour_models,
        }
        if optimize and rules is not None:
            candidates = candidates_from_forecast(forecast["rows"], PRIMARY_MODEL)
            if candidates:
                try:
                    tour_report["hindsight"] = hindsight_squad(
                        candidates, rules, actuals
                    )
                except OptimizerError as error:
                    tour_report["hindsight"] = {"error": str(error)}
        tour_reports.append(tour_report)

    model_summaries: dict[str, Any] = {}
    for model in models:
        squads = per_model_squads[model]
        summary: dict[str, Any] = {
            "metrics": _pooled(per_model_tour_metrics[model]),
            # Restricted to players who actually took the field: the full
            # population is dominated by trivially correct zeros for players who
            # never appeared, which flattens the difference between models.
            "metrics_played": _pooled(per_model_tour_played[model]),
            "by_role": role_metrics(per_model_role_rows[model]),
            "by_tour": [
                {
                    **_tour_payload(tour),
                    "metrics": metrics,
                    "metrics_played": played_metrics,
                }
                for tour, metrics, played_metrics in zip(
                    selected,
                    per_model_tour_metrics[model],
                    per_model_tour_played[model],
                )
            ],
        }
        if squads:
            actual_total = sum(entry["actual_points"] for entry in squads)
            projected_total = sum(entry["projected_points"] for entry in squads)
            efficiencies = [
                entry["lineup_efficiency"]
                for entry in squads
                if entry["lineup_efficiency"] is not None
            ]
            captain_hits = sum(
                1 for entry in squads if entry["captain"]["was_best_starter"]
            )
            summary["squad"] = {
                "tours": len(squads),
                "actual_points_total": round(actual_total, 4),
                "actual_points_mean": round(actual_total / len(squads), 4),
                "projected_points_total": round(projected_total, 4),
                "projection_gap_total": round(projected_total - actual_total, 4),
                "bench_actual_points_total": round(
                    sum(entry["bench_actual_points"] for entry in squads), 4
                ),
                "best_eleven_actual_points_total": round(
                    sum(entry["best_eleven_actual_points"] for entry in squads), 4
                ),
                "lineup_efficiency_mean": (
                    round(sum(efficiencies) / len(efficiencies), 4)
                    if efficiencies
                    else None
                ),
                "captain_actual_points_total": round(
                    sum(entry["captain"]["actual_points"] for entry in squads), 4
                ),
                "captain_hit_rate": round(captain_hits / len(squads), 4),
            }
        model_summaries[model] = summary

    hindsight_total = sum(
        float(report["hindsight"]["actual_points"])
        for report in tour_reports
        if isinstance(report.get("hindsight"), dict)
        and "actual_points" in report["hindsight"]
    )

    all_errors.sort(key=lambda item: (-item["error"], item["player_season_id"]))
    over_predicted = all_errors[:top_errors]
    under_predicted = sorted(
        all_errors, key=lambda item: (item["error"], item["player_season_id"])
    )[:top_errors]

    def _audit_digest(key: str) -> list[dict[str, Any]]:
        return [
            {
                **{k: audit[k] for k in ("tour_id", "name", "fantasy_tour_id")},
                key: audit[key],
            }
            for audit in audits
            if audit[key]
        ]

    audit_violations = _audit_digest("violations")
    audit_warnings = _audit_digest("warnings")

    return {
        "backtest_version": BACKTEST_VERSION,
        "generated_at": generated_at.isoformat(),
        "params": {
            "run_id": resolved_run_id,
            "season_ref": season_ref,
            "tours": [tour.fantasy_tour_id for tour in selected],
            "requested_tours": list(tour_refs) if tour_refs else None,
            "models": list(models),
            "optimize": optimize,
            "fixture_conflict_weight": fixture_conflict_weight,
            "top_errors": top_errors,
            "top_unstable": top_unstable,
        },
        "versions": {
            "backtest": BACKTEST_VERSION,
            "model": MODEL_VERSION,
            "feature": FEATURE_VERSION,
            "scoring": SCORING_VERSION,
            "optimizer": OPTIMIZER_VERSION,
        },
        "run_id": resolved_run_id,
        "season_id": season_id,
        "season": season_payload,
        "counts": {
            "tours_total": len(tours),
            "tours_evaluated": len(selected),
            "tours_skipped": len(skipped),
            "predictions": sum(
                int(metrics.get("n") or 0)
                for metrics in per_model_tour_metrics[models[0]]
            ),
        },
        "skipped_tours": skipped,
        "cutoff_audit": {
            "tours_checked": len(audits),
            "rows_checked": sum(audit["rows_checked"] for audit in audits),
            "passed": not audit_violations,
            "violations": audit_violations,
            "warnings": audit_warnings,
            "tours": audits,
        },
        "tours": tour_reports,
        "models": model_summaries,
        "hindsight": {
            "actual_points_total": round(hindsight_total, 4) if optimize else None
        },
        "errors": {
            "over_predicted": over_predicted,
            "under_predicted": under_predicted,
        },
        "feature_instability": feature_instability(
            feature_history, top=top_unstable
        ),
        "verdict": decide(model_summaries),
    }


__all__ = [
    "ACCEPT_MODEL",
    "BACKTEST_VERSION",
    "DEFAULT_MODELS",
    "KEEP_BASELINE",
    "PRIMARY_MODEL",
    "REVISE_MODEL",
    "BacktestError",
    "TourRef",
    "audit_tour",
    "best_eleven_points",
    "decide",
    "error_metrics",
    "feature_instability",
    "hindsight_squad",
    "role_metrics",
    "run_backtest",
    "simulate_squad",
]
