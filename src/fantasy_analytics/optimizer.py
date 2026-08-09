"""Squad optimizer (development-plan step 8).

Given the expected-points forecast (step 7), this module picks a *valid* fantasy
squad for a target tour: the full roster (usually 15 players), the starting
eleven, the captain, the vice-captain and the ordered bench. It is a genuine
integer program solved with OR-Tools CP-SAT.

Design guarantees
-----------------

* **Rules, not constants.** The budget and the per-role squad/starting limits
  come from ``season_rules``; the club limit and the transfer limit come from
  the target ``fantasy_tours`` row. Nothing is hard-coded (the values
  demonstrably vary by season and tour).
* **Two modes.** ``squad`` builds a fresh roster from scratch; ``transfers``
  keeps an existing roster and changes at most ``total_transfers`` players.
* **Objective matches the plan.** The solver maximises the expected points of
  the starting eleven *plus* the captain (whose points are counted twice),
  which is exactly the fantasy scoring of a lineup.
* **Deterministic.** A single search worker and a fixed random seed, together
  with a deterministic candidate ordering and a strict spend tie-break, make the
  same inputs always produce the same squad.
* **Independently validated.** :func:`validate_squad` re-checks every rule on the
  produced solution without trusting the solver, and the builders raise
  :class:`OptimizerError` with a clear message when a problem is infeasible.

The module only reads the database (through the forecast/feature builders) and
never writes to it or calls the Sports.ru API. Automatic squad submission is out
of scope.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Sequence

from ortools.sat.python import cp_model
from sqlalchemy.orm import sessionmaker

from .db import session_scope
from .db.models import FantasyTour, SeasonRules
from .forecast import MODEL_EVENT, ForecastError, build_forecast_dataset

# Bumped whenever the optimizer model or its constraints change so squads built
# by different code revisions never get silently compared.
OPTIMIZER_VERSION = "1.0.0"

ROLES = ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")

# Short labels used to render a formation such as "4-4-2".
ROLE_SHORT = {
    "GOALKEEPER": "GK",
    "DEFENDER": "DEF",
    "MIDFIELDER": "MID",
    "FORWARD": "FWD",
}

# Expected points are rounded to 4 decimals upstream, so scaling by 10^4 keeps
# the objective an exact integer. Prices are Numeric(6,2), so cents are exact.
_POINTS_SCALE = 10_000
_PRICE_SCALE = 100

# The primary (points) term is multiplied by this factor before the secondary
# spend tie-break is subtracted, so the tie-break can never overturn a better
# points solution. It only has to exceed the largest possible spend in cents.
_TIE_BREAK_HEADROOM = 10_000_000


class OptimizerError(RuntimeError):
    """Raised when a squad cannot be resolved or the problem is infeasible."""


@dataclass(frozen=True)
class Candidate:
    """One selectable player with the numbers the optimizer needs."""

    player_season_id: int
    fantasy_player_id: str | None
    player_name: str | None
    role: str
    club_id: int
    club_name: str | None
    price: float
    expected_points: float
    # Purely descriptive fields carried into the explanation.
    opponent_name: str | None = None
    is_home: bool | None = None
    match_id: int | None = None
    p_appearance: float | None = None
    expected_minutes: float | None = None
    stat_source: str | None = None
    is_newcomer: bool | None = None

    @property
    def price_cents(self) -> int:
        return int(round(self.price * _PRICE_SCALE))

    @property
    def points_scaled(self) -> int:
        return int(round(self.expected_points * _POINTS_SCALE))


@dataclass(frozen=True)
class SquadRules:
    """Budget and roster limits resolved from season_rules and the tour."""

    total_budget: float
    total_players: int
    starting_players: int
    full_limits: dict[str, tuple[int, int]]
    starting_limits: dict[str, tuple[int, int]]
    max_same_team: int
    total_transfers: int | None

    @property
    def budget_cents(self) -> int:
        return int(round(self.total_budget * _PRICE_SCALE))


def parse_role_limits(
    constraints: Iterable[dict[str, Any]] | None,
) -> dict[str, tuple[int, int]]:
    """Turn the API roster-constraint list into ``{role: (min, max)}``.

    The API delivers a list of ``{"role", "minCount", "maxCount"}`` dicts. A
    role missing from the list is treated as unconstrained (0..total).
    """
    limits: dict[str, tuple[int, int]] = {}
    for entry in constraints or []:
        role = entry.get("role")
        if role is None:
            continue
        min_count = entry.get("minCount")
        max_count = entry.get("maxCount")
        limits[role] = (
            int(min_count) if min_count is not None else 0,
            int(max_count) if max_count is not None else 10**6,
        )
    return limits


# ---------------------------------------------------------------------------
# Candidate assembly.
# ---------------------------------------------------------------------------


def candidates_from_forecast(
    rows: Sequence[dict[str, Any]], model: str
) -> list[Candidate]:
    """Build the deterministic candidate pool for one forecast model.

    Only players with a known price and club (i.e. a real fixture and snapshot)
    can be selected; the rest are dropped so the budget and club-limit
    constraints are always well defined.
    """
    candidates: list[Candidate] = []
    seen: set[int] = set()
    for row in rows:
        if row.get("model_name") != model:
            continue
        price = row.get("price")
        club_id = row.get("club_id")
        role = row.get("role")
        psid = row.get("player_season_id")
        if price is None or club_id is None or role not in ROLES or psid is None:
            continue
        if psid in seen:
            continue
        seen.add(psid)
        candidates.append(
            Candidate(
                player_season_id=int(psid),
                fantasy_player_id=row.get("fantasy_player_id"),
                player_name=row.get("player_name"),
                role=role,
                club_id=int(club_id),
                club_name=row.get("club_name"),
                price=float(price),
                expected_points=float(row.get("expected_points") or 0.0),
                opponent_name=row.get("opponent_name"),
                is_home=row.get("is_home"),
                match_id=row.get("match_id"),
                p_appearance=row.get("p_appearance"),
                expected_minutes=row.get("expected_minutes"),
                stat_source=row.get("stat_source"),
                is_newcomer=row.get("is_newcomer"),
            )
        )
    # Deterministic order so the CP-SAT model is built identically every run.
    candidates.sort(key=lambda c: c.player_season_id)
    return candidates


# ---------------------------------------------------------------------------
# Core solver (pure: no database).
# ---------------------------------------------------------------------------


def _candidate_public(candidate: Candidate) -> dict[str, Any]:
    return {
        "player_season_id": candidate.player_season_id,
        "fantasy_player_id": candidate.fantasy_player_id,
        "player_name": candidate.player_name,
        "role": candidate.role,
        "club_id": candidate.club_id,
        "club_name": candidate.club_name,
        "price": round(candidate.price, 2),
        "expected_points": round(candidate.expected_points, 4),
        "opponent_name": candidate.opponent_name,
        "is_home": candidate.is_home,
        "match_id": candidate.match_id,
        "p_appearance": candidate.p_appearance,
        "expected_minutes": candidate.expected_minutes,
        "stat_source": candidate.stat_source,
        "is_newcomer": candidate.is_newcomer,
    }


def _formation(starters: Sequence[Candidate]) -> str:
    counts = {role: 0 for role in ROLES}
    for candidate in starters:
        counts[candidate.role] += 1
    return "-".join(
        str(counts[role]) for role in ("DEFENDER", "MIDFIELDER", "FORWARD")
    )


def solve_squad(
    candidates: Sequence[Candidate],
    rules: SquadRules,
    *,
    current_ids: Sequence[int] | None = None,
    max_transfers: int | None = None,
) -> dict[str, Any]:
    """Solve the squad-selection integer program and return an explanation.

    With ``current_ids`` the solver runs in *limited-transfers* mode: at most
    ``max_transfers`` (defaulting to the tour's ``total_transfers``) of the
    current players may be replaced. Otherwise it builds a fresh squad.

    Raises :class:`OptimizerError` when the pool is too small or the constraints
    cannot be satisfied.
    """
    if not candidates:
        raise OptimizerError("No priced candidates available for the target tour")

    model = cp_model.CpModel()
    n = len(candidates)
    pick = [model.NewBoolVar(f"pick_{i}") for i in range(n)]
    start = [model.NewBoolVar(f"start_{i}") for i in range(n)]
    captain = [model.NewBoolVar(f"captain_{i}") for i in range(n)]

    # Squad and starting-eleven sizes.
    model.Add(sum(pick) == rules.total_players)
    model.Add(sum(start) == rules.starting_players)
    for i in range(n):
        model.Add(start[i] <= pick[i])
        model.Add(captain[i] <= start[i])
    model.Add(sum(captain) == 1)

    # Per-role limits for the full squad and the starting eleven.
    by_role: dict[str, list[int]] = {role: [] for role in ROLES}
    for i, candidate in enumerate(candidates):
        by_role[candidate.role].append(i)
    for role in ROLES:
        idx = by_role[role]
        full_min, full_max = rules.full_limits.get(role, (0, rules.total_players))
        start_min, start_max = rules.starting_limits.get(
            role, (0, rules.starting_players)
        )
        model.Add(sum(pick[i] for i in idx) >= full_min)
        model.Add(sum(pick[i] for i in idx) <= full_max)
        model.Add(sum(start[i] for i in idx) >= start_min)
        model.Add(sum(start[i] for i in idx) <= start_max)

    # Budget.
    model.Add(
        sum(candidates[i].price_cents * pick[i] for i in range(n))
        <= rules.budget_cents
    )

    # Club limit.
    by_club: dict[int, list[int]] = {}
    for i, candidate in enumerate(candidates):
        by_club.setdefault(candidate.club_id, []).append(i)
    for idx in by_club.values():
        model.Add(sum(pick[i] for i in idx) <= rules.max_same_team)

    # Limited-transfers mode.
    transfers_meta: dict[str, Any] | None = None
    if current_ids is not None:
        allowed = max_transfers if max_transfers is not None else rules.total_transfers
        if allowed is None:
            raise OptimizerError(
                "The tour has no transfer limit; pass an explicit max_transfers"
            )
        if allowed < 0:
            raise OptimizerError("max_transfers must be non-negative")
        id_to_index = {c.player_season_id: i for i, c in enumerate(candidates)}
        present = [id_to_index[pid] for pid in current_ids if pid in id_to_index]
        missing = [pid for pid in current_ids if pid not in id_to_index]
        # Players kept = current players still picked. Transfers = squad size
        # minus kept, so keeping at least (size - allowed) caps transfers.
        model.Add(sum(pick[i] for i in present) >= rules.total_players - allowed)
        transfers_meta = {
            "allowed": allowed,
            "current_present": present,
            "missing": missing,
        }

    # Objective: maximise starting + captain points, break ties by spending
    # less (more unused budget). The headroom keeps spend strictly secondary.
    points_term = sum(
        candidates[i].points_scaled * (start[i] + captain[i]) for i in range(n)
    )
    spend_term = sum(candidates[i].price_cents * pick[i] for i in range(n))
    model.Maximize(points_term * _TIE_BREAK_HEADROOM - spend_term)

    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.Solve(model)

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        raise OptimizerError(
            "No valid squad satisfies the budget, roster and club constraints "
            f"(solver status: {solver.StatusName(status)})"
        )

    picked = [i for i in range(n) if solver.Value(pick[i]) == 1]
    starters_idx = [i for i in picked if solver.Value(start[i]) == 1]
    captain_idx = next(i for i in picked if solver.Value(captain[i]) == 1)

    starters = [candidates[i] for i in starters_idx]
    bench_idx = [i for i in picked if i not in set(starters_idx)]

    # Vice-captain: the best remaining starter (deterministic tie-break by id).
    ordered_starters = sorted(
        starters_idx,
        key=lambda i: (-candidates[i].points_scaled, candidates[i].player_season_id),
    )
    vice_idx = next((i for i in ordered_starters if i != captain_idx), captain_idx)

    # Bench order: outfield subs first by expected points, reserve keeper last.
    def _bench_key(i: int) -> tuple[int, int, int]:
        is_keeper = 1 if candidates[i].role == "GOALKEEPER" else 0
        return (is_keeper, -candidates[i].points_scaled, candidates[i].player_season_id)

    bench_ordered = sorted(bench_idx, key=_bench_key)

    total_price = round(sum(candidates[i].price for i in picked), 2)
    starting_points = round(sum(candidates[i].expected_points for i in starters_idx), 4)
    objective_points = round(
        starting_points + candidates[captain_idx].expected_points, 4
    )

    squad: list[dict[str, Any]] = []
    bench_rank = {idx: rank for rank, idx in enumerate(bench_ordered)}
    for i in picked:
        entry = _candidate_public(candidates[i])
        entry["is_starter"] = i in set(starters_idx)
        entry["is_captain"] = i == captain_idx
        entry["is_vice_captain"] = i == vice_idx
        entry["bench_order"] = bench_rank.get(i)
        squad.append(entry)
    squad.sort(
        key=lambda e: (
            0 if e["is_starter"] else 1,
            ROLES.index(e["role"]),
            -e["expected_points"],
            e["player_season_id"],
        )
    )

    result: dict[str, Any] = {
        "status": solver.StatusName(status),
        "objective_expected_points": objective_points,
        "starting_expected_points": starting_points,
        "formation": _formation(starters),
        "total_price": total_price,
        "unused_budget": round(rules.total_budget - total_price, 2),
        "captain": _candidate_public(candidates[captain_idx]),
        "vice_captain": _candidate_public(candidates[vice_idx]),
        "squad": squad,
        "starting": [e for e in squad if e["is_starter"]],
        "bench": [
            _candidate_public(candidates[i]) | {"bench_order": bench_rank[i]}
            for i in bench_ordered
        ],
    }

    if transfers_meta is not None:
        kept = [
            candidates[i].player_season_id
            for i in transfers_meta["current_present"]
            if i in set(picked)
        ]
        current_set = set(current_ids or [])
        brought_in = [
            _candidate_public(candidates[i])
            for i in picked
            if candidates[i].player_season_id not in current_set
        ]
        transferred_out = sorted(current_set - set(kept))
        result["transfers"] = {
            "allowed": transfers_meta["allowed"],
            "made": len(brought_in),
            "kept": len(kept),
            "in": brought_in,
            "out": transferred_out,
            "missing_from_pool": transfers_meta["missing"],
        }
    else:
        result["transfers"] = None

    return result


# ---------------------------------------------------------------------------
# Independent validator (does not trust the solver).
# ---------------------------------------------------------------------------


def validate_squad(solution: dict[str, Any], rules: SquadRules) -> list[str]:
    """Re-check every rule on a produced squad; return a list of violations.

    An empty list means the squad is valid. This is intentionally independent of
    the solver so a bug in the model surfaces as a validation failure.
    """
    violations: list[str] = []
    squad = solution.get("squad", [])
    starters = [p for p in squad if p.get("is_starter")]

    if len(squad) != rules.total_players:
        violations.append(
            f"squad size {len(squad)} != required {rules.total_players}"
        )
    if len(starters) != rules.starting_players:
        violations.append(
            f"starting size {len(starters)} != required {rules.starting_players}"
        )

    # Duplicate players.
    ids = [p["player_season_id"] for p in squad]
    if len(set(ids)) != len(ids):
        violations.append("squad contains duplicate players")

    # Per-role limits.
    full_counts = {role: 0 for role in ROLES}
    start_counts = {role: 0 for role in ROLES}
    for player in squad:
        full_counts[player["role"]] = full_counts.get(player["role"], 0) + 1
    for player in starters:
        start_counts[player["role"]] = start_counts.get(player["role"], 0) + 1
    for role in ROLES:
        full_min, full_max = rules.full_limits.get(role, (0, rules.total_players))
        if not full_min <= full_counts[role] <= full_max:
            violations.append(
                f"squad has {full_counts[role]} {role}; allowed {full_min}..{full_max}"
            )
        start_min, start_max = rules.starting_limits.get(
            role, (0, rules.starting_players)
        )
        if not start_min <= start_counts[role] <= start_max:
            violations.append(
                f"starting has {start_counts[role]} {role}; allowed "
                f"{start_min}..{start_max}"
            )

    # Budget.
    total_price = round(sum(p["price"] for p in squad), 2)
    if total_price > rules.total_budget + 1e-9:
        violations.append(
            f"squad price {total_price} exceeds budget {rules.total_budget}"
        )

    # Club limit.
    club_counts: dict[int, int] = {}
    for player in squad:
        club_counts[player["club_id"]] = club_counts.get(player["club_id"], 0) + 1
    for club_id, count in club_counts.items():
        if count > rules.max_same_team:
            violations.append(
                f"club {club_id} has {count} players; limit {rules.max_same_team}"
            )

    # Captain and vice-captain.
    captains = [p for p in squad if p.get("is_captain")]
    vices = [p for p in squad if p.get("is_vice_captain")]
    if len(captains) != 1:
        violations.append(f"expected exactly one captain, found {len(captains)}")
    elif not captains[0].get("is_starter"):
        violations.append("captain is not in the starting eleven")
    if len(vices) != 1:
        violations.append(
            f"expected exactly one vice-captain, found {len(vices)}"
        )
    elif not vices[0].get("is_starter"):
        violations.append("vice-captain is not in the starting eleven")
    if captains and vices and captains[0]["player_season_id"] == vices[0][
        "player_season_id"
    ]:
        violations.append("captain and vice-captain are the same player")

    # Transfer limit.
    transfers = solution.get("transfers")
    if transfers is not None and transfers.get("made", 0) > transfers.get(
        "allowed", 0
    ):
        violations.append(
            f"made {transfers['made']} transfers; limit {transfers['allowed']}"
        )

    return violations


# ---------------------------------------------------------------------------
# Database-backed builder.
# ---------------------------------------------------------------------------


def _load_rules(session, season_id: int, tour_id: int) -> SquadRules:
    season_rules = session.get(SeasonRules, season_id)
    if season_rules is None:
        raise OptimizerError(
            f"Season {season_id} has no season_rules row; import the season first"
        )
    tour = session.get(FantasyTour, tour_id)
    if tour is None:
        raise OptimizerError(f"Tour {tour_id} does not exist")
    if tour.max_same_team_players is None:
        raise OptimizerError(
            f"Tour {tour.fantasy_tour_id} has no club limit (max_same_team_players)"
        )
    return SquadRules(
        total_budget=float(season_rules.total_budget),
        total_players=int(season_rules.total_players),
        starting_players=int(season_rules.starting_players),
        full_limits=parse_role_limits(season_rules.full_roster_constraints),
        starting_limits=parse_role_limits(season_rules.starting_roster_constraints),
        max_same_team=int(tour.max_same_team_players),
        total_transfers=(
            int(tour.total_transfers) if tour.total_transfers is not None else None
        ),
    )


def _resolve_current_ids(
    current_squad: Sequence[str | int] | None, candidates: Sequence[Candidate]
) -> list[int] | None:
    """Map the caller's current squad (fantasy ids or season ids) to season ids."""
    if current_squad is None:
        return None
    by_fantasy = {
        c.fantasy_player_id: c.player_season_id
        for c in candidates
        if c.fantasy_player_id is not None
    }
    known_season = {c.player_season_id for c in candidates}
    resolved: list[int] = []
    for ref in current_squad:
        ref_str = str(ref)
        if ref_str in by_fantasy:
            resolved.append(by_fantasy[ref_str])
        elif ref_str.isdigit() and int(ref_str) in known_season:
            resolved.append(int(ref_str))
        else:
            # Keep unknown ids so they are reported as forced transfers out.
            try:
                resolved.append(int(ref_str))
            except ValueError:
                raise OptimizerError(
                    f"Current-squad reference {ref!r} is not a valid player id"
                ) from None
    return resolved


def build_squad_optimization(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    season_ref: str | None = None,
    tour_ref: str | None = None,
    model: str = MODEL_EVENT,
    current_squad: Sequence[str | int] | None = None,
    max_transfers: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the optimal squad for a tour and return a JSON-serialisable report.

    The forecast is rebuilt in-process from the active snapshot (or ``run_id``),
    so the result is reproducible from ``(run_id, tour, model, optimizer)``. When
    ``current_squad`` is given the optimizer runs in limited-transfers mode.
    """
    generated_at = now or datetime.now(UTC)
    try:
        forecast = build_forecast_dataset(
            session_factory,
            run_id=run_id,
            season_ref=season_ref,
            tour_ref=tour_ref,
            now=generated_at,
        )
    except ForecastError as error:
        raise OptimizerError(str(error)) from error

    candidates = candidates_from_forecast(forecast["rows"], model)
    if not candidates:
        raise OptimizerError(
            f"No priced candidates for model {model!r} in the target tour"
        )

    with session_scope(session_factory) as session:
        rules = _load_rules(
            session, forecast["season_id"], forecast["tour"]["tour_id"]
        )

    mode = "transfers" if current_squad is not None else "squad"
    current_ids = _resolve_current_ids(current_squad, candidates)

    solution = solve_squad(
        candidates,
        rules,
        current_ids=current_ids,
        max_transfers=max_transfers,
    )

    violations = validate_squad(solution, rules)
    if violations:
        raise OptimizerError(
            "Optimizer produced an invalid squad: " + "; ".join(violations)
        )

    return {
        "optimizer_version": OPTIMIZER_VERSION,
        "model": model,
        "mode": mode,
        "generated_at": generated_at.isoformat(),
        "run_id": forecast["run_id"],
        "season_id": forecast["season_id"],
        "season": forecast["season"],
        "tour": forecast["tour"],
        "cutoff": forecast["cutoff"],
        "rules": {
            "total_budget": rules.total_budget,
            "total_players": rules.total_players,
            "starting_players": rules.starting_players,
            "full_limits": {r: list(v) for r, v in rules.full_limits.items()},
            "starting_limits": {
                r: list(v) for r, v in rules.starting_limits.items()
            },
            "max_same_team": rules.max_same_team,
            "total_transfers": rules.total_transfers,
        },
        "counts": {
            "candidates": len(candidates),
        },
        "solution": solution,
        "valid": True,
    }


__all__ = [
    "OPTIMIZER_VERSION",
    "ROLES",
    "Candidate",
    "SquadRules",
    "OptimizerError",
    "parse_role_limits",
    "candidates_from_forecast",
    "solve_squad",
    "validate_squad",
    "build_squad_optimization",
]
