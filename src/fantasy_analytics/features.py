"""Analytical feature engineering (development-plan step 6).

This module turns the *active* snapshot published by the quality gate (step 4)
into a reproducible, leakage-free dataset that later steps use to predict the
fantasy points a player will score in an upcoming tour.

Design guarantees
-----------------

* **Reproducible.** Every dataset is keyed to a single ingestion run (the active
  snapshot of a season by default) and a feature-schema version, so rebuilding
  it from the same run yields identical rows.
* **Leakage-free.** Every feature is computed strictly from matches that kicked
  off *before* the target tour's transfer deadline (the ``cutoff``). Matches
  belonging to the target tour itself are excluded from the history set as a
  second line of defence, even if a tour's deadline is mis-dated in the source.
* **Explicit fills.** Missing values follow a documented strategy (see
  ``FEATURE_DICTIONARY`` and ``docs/feature-dictionary.md``) rather than leaking
  ``NaN`` into consumers.

The module only reads domain data; it never calls the Sports.ru API and never
writes to the database. The final ML algorithm is deliberately out of scope
(step 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .db import session_scope
from .db.models import (
    ClubMatchStats,
    FantasyPlayerSnapshot,
    FantasyTour,
    Match,
    Player,
    PlayerMatchStats,
    PlayerSeason,
    Season,
    SeasonClub,
)

# Bumped whenever the feature set or its computation changes so that datasets
# built by different code revisions never get silently mixed.
# 1.1.0 added the per-90 rates the event-based forecast (step 7) consumes.
# 1.2.0 added cross-season sourcing (step 14): when the target season has not
#   started yet, a player's history is drawn from the prior season by the shared
#   cross-season identity, every row carries a ``stat_source`` label, and
#   newcomers without any prior history get documented role priors.
FEATURE_VERSION = "1.2.0"

ROLES = ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD")

# Rolling look-back windows (in appearances) required by the plan.
ROLLING_WINDOWS = (3, 5, 10)

# ``stat_source`` marks where a row's history came from so the frontend can
# visually separate last season's numbers from the ones collected this season.
STAT_SOURCE_CURRENT = "current_season"
STAT_SOURCE_PRIOR = "prior_season"

# Documented priors applied to a newcomer that has no history in the prior
# season (step 14). Offensive/defensive per-90 rates default to the role average
# from the prior season, discounted because an unknown player is riskier than a
# proven one; the appearance probability is a conservative constant. These are
# deliberately position-based; refining them by price/club is left to step 18.
NEWCOMER_P_APPEARANCE = 0.5
NEWCOMER_RATE_FACTOR = 0.7

# A player is credited with a "start" when they played at least this many
# minutes. Sports.ru does not import an explicit lineup flag, so start share is
# a documented approximation based on minutes played.
START_MINUTES_THRESHOLD = 60

# Look-back window (in the club's most recent matches / the player's most recent
# appearances) used to estimate appearance probability and expected minutes.
AVAILABILITY_WINDOW = 5

# Fantasy availability statuses that make a player unavailable for the tour.
# Everything else (``FIERY``, ``UNKNOWN`` and any not-yet-seen value) is treated
# as available, so a new status never silently zeroes a player out.
UNAVAILABLE_STATUSES = frozenset(
    {
        "INJURY",
        "INJURED",
        "DISQUALIFICATION",
        "DISQUALIFIED",
        "SUSPENDED",
        "SUSPENSION",
        "OUT",
        "LEFT",
    }
)


class FeaturesError(RuntimeError):
    """Raised when a run, season or target tour cannot be resolved."""


@dataclass(frozen=True)
class Appearance:
    """One player-match observation used to build rolling features."""

    match_id: int
    scheduled_at: datetime
    minutes: int
    points: int
    goals: int
    assists: int
    saves: int = 0
    ball_recoveries: int = 0
    yellow_cards: int = 0


@dataclass(frozen=True)
class ClubMatch:
    """One finished club match used to derive strength and rest features."""

    match_id: int
    scheduled_at: datetime
    is_home: bool
    goals_scored: int
    goals_conceded: int


@dataclass(frozen=True)
class Fixture:
    """A single upcoming match a club plays in the target tour."""

    match_id: int
    scheduled_at: datetime
    club_id: int
    opponent_club_id: int
    is_home: bool


@dataclass(frozen=True)
class RolePrior:
    """Position-based per-90 priors used for newcomers (step 14)."""

    goals_per90: float
    assists_per90: float
    saves_per90: float
    recoveries_per90: float
    yellows_per90: float
    mean_minutes: float


@dataclass(frozen=True)
class PriorPlayer:
    """A player's prior-season identity, resolved by the shared ``player_id``."""

    player_season_id: int
    club_id: int | None
    role: str


@dataclass(frozen=True)
class PriorContext:
    """Everything needed to source a target-tour row from the prior season."""

    run_id: int
    season_id: int
    appearances: dict[int, list[Appearance]]
    club_matches: dict[int, list[ClubMatch]]
    by_player_id: dict[int, PriorPlayer]
    role_priors: dict[str, RolePrior]


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in isolation).
# ---------------------------------------------------------------------------


def recent_before_cutoff(
    appearances: list[Appearance],
    cutoff: datetime,
    *,
    exclude_match_ids: frozenset[int] = frozenset(),
) -> list[Appearance]:
    """Return appearances strictly before ``cutoff``, most recent first.

    ``exclude_match_ids`` drops matches that belong to the target tour so they
    can never leak into the history even when a tour deadline is mis-dated.
    """
    kept = [
        appearance
        for appearance in appearances
        if appearance.scheduled_at < cutoff
        and appearance.match_id not in exclude_match_ids
    ]
    kept.sort(key=lambda a: a.scheduled_at, reverse=True)
    return kept


def _mean(values: list[float | int]) -> float:
    return sum(values) / len(values) if values else 0.0


def per90(total: int, minutes: int) -> float:
    """Per-90 rate; zero when no minutes were played (documented fill)."""
    if minutes <= 0:
        return 0.0
    return round(total / minutes * 90, 4)


def _window_stats(window: list[Appearance]) -> dict[str, Any]:
    return {
        "appearances": len(window),
        "points_avg": round(_mean([a.points for a in window]), 4),
        "points_sum": sum(a.points for a in window),
        "goals_sum": sum(a.goals for a in window),
        "assists_sum": sum(a.assists for a in window),
        "minutes_avg": round(_mean([a.minutes for a in window]), 2),
    }


# ---------------------------------------------------------------------------
# Feature dictionary (also mirrored in docs/feature-dictionary.md).
# ---------------------------------------------------------------------------

FEATURE_DICTIONARY: tuple[dict[str, str], ...] = (
    {"name": "player_season_id", "description": "Internal player-season surrogate key."},
    {"name": "fantasy_player_id", "description": "External Sports.ru fantasy player id."},
    {"name": "player_name", "description": "Canonical player name."},
    {"name": "role", "description": "GOALKEEPER / DEFENDER / MIDFIELDER / FORWARD."},
    {"name": "club_id", "description": "Internal club id the player belongs to at cutoff."},
    {"name": "club_name", "description": "Club display name."},
    {"name": "is_home", "description": "True when the club hosts the target-tour fixture."},
    {"name": "opponent_club_id", "description": "Internal club id of the target-tour opponent."},
    {"name": "opponent_name", "description": "Opponent club display name."},
    {"name": "match_scheduled_at", "description": "Kickoff of the target-tour fixture (ISO 8601)."},
    {"name": "rest_days", "description": "Days between the club's last match before cutoff and the fixture; null when the club has no prior match."},
    {"name": "availability_status", "description": "Fantasy availability status from the active snapshot."},
    {"name": "is_available", "description": "False when availability_status marks the player out (see UNAVAILABLE_STATUSES)."},
    {"name": "price", "description": "Fantasy price from the active snapshot; null when absent."},
    {"name": "selected_by", "description": "Ownership percent from the active snapshot; null when absent."},
    {"name": "form", "description": "Fantasy form value from the active snapshot; null when absent."},
    {"name": "points_avg_{N}", "description": "Mean fantasy points over the last N appearances (N in 3/5/10); 0.0 when none."},
    {"name": "points_sum_{N}", "description": "Total fantasy points over the last N appearances."},
    {"name": "goals_sum_{N}", "description": "Goals over the last N appearances."},
    {"name": "assists_sum_{N}", "description": "Assists over the last N appearances."},
    {"name": "minutes_avg_{N}", "description": "Mean minutes over the last N appearances; 0.0 when none."},
    {"name": "appearances_{N}", "description": "Number of appearances actually found in the last-N window."},
    {"name": "total_appearances", "description": "All appearances before cutoff."},
    {"name": "total_minutes", "description": "Total minutes before cutoff."},
    {"name": "total_points", "description": "Total fantasy points before cutoff."},
    {"name": "points_per90", "description": "Season-to-date points per 90 minutes; 0.0 when no minutes."},
    {"name": "goals_per90", "description": "Season-to-date goals per 90 minutes; 0.0 when no minutes."},
    {"name": "assists_per90", "description": "Season-to-date assists per 90 minutes; 0.0 when no minutes."},
    {"name": "saves_per90", "description": "Season-to-date goalkeeper saves per 90 minutes; 0.0 when no minutes."},
    {"name": "recoveries_per90", "description": "Season-to-date ball recoveries per 90 minutes; 0.0 when no minutes."},
    {"name": "yellows_per90", "description": "Season-to-date yellow cards per 90 minutes; 0.0 when no minutes."},
    {"name": "club_matches_before", "description": "Club matches played before cutoff (denominator for share features)."},
    {"name": "appearance_share", "description": "Share of the club's matches the player appeared in; 0.0 when the club has no prior match."},
    {"name": "start_share", "description": "Share of the club's matches the player started (>= 60 minutes); 0.0 when none."},
    {"name": "p_appearance", "description": "Estimated probability of playing the fixture from the last 5 club matches; 0.0 when unavailable."},
    {"name": "expected_minutes", "description": "Expected minutes: p_appearance x recent mean minutes when appearing."},
    {"name": "club_attack", "description": "Club goals scored per match at the fixture venue (home/away); league mean when no venue matches yet."},
    {"name": "club_defense", "description": "Club goals conceded per match at the fixture venue; league mean when no venue matches yet."},
    {"name": "opponent_attack", "description": "Opponent goals scored per match at their fixture venue; league mean fallback."},
    {"name": "opponent_defense", "description": "Opponent goals conceded per match at their fixture venue; league mean fallback."},
    {"name": "has_history", "description": "True when the player has at least one appearance in the sourced history."},
    {"name": "stat_source", "description": "Where the history came from: 'current_season' or 'prior_season' (cross-season backfill while the target season has not started)."},
    {"name": "is_newcomer", "description": "True when the player has no prior-season history and is scored from documented role priors."},
)


# ---------------------------------------------------------------------------
# Loading and resolution.
# ---------------------------------------------------------------------------


def _resolve_run(session, run_id: int | None, season_ref: str | None):
    """Resolve the ingestion run whose snapshot the features are built from.

    With an explicit ``run_id`` that run is used. Otherwise the single active
    run (published by the quality gate) is selected, optionally filtered to a
    season referenced by its fantasy/stat id or name.
    """
    from .db.models import IngestionRun

    if run_id is not None:
        run = session.get(IngestionRun, run_id)
        if run is None:
            raise FeaturesError(f"Ingestion run {run_id} does not exist")
        if run.season_id is None:
            raise FeaturesError(
                f"Ingestion run {run_id} has no season; run the quality gate first"
            )
        return run

    query = (
        select(IngestionRun)
        .where(IngestionRun.is_active.is_(True))
        .order_by(IngestionRun.id.desc())
    )
    if season_ref is not None:
        query = query.join(Season, IngestionRun.season_id == Season.id).where(
            (Season.fantasy_season_id == season_ref)
            | (Season.stat_season_id == season_ref)
            | (Season.name == season_ref)
        )
    run = session.execute(query.limit(1)).scalar_one_or_none()
    if run is None:
        raise FeaturesError(
            "No active snapshot found; run 'fantasy-ingest' then 'fantasy-quality'"
            + (f" for season '{season_ref}'" if season_ref else "")
        )
    return run


def resolve_target_tour(
    session, season_id: int, tour_ref: str | None
) -> FantasyTour:
    """Resolve the tour to build features for.

    A ``tour_ref`` matches a tour by its fantasy id or name. Without one, the
    earliest tour that is not ``FINISHED`` is chosen (the natural "next tour");
    a fully finished season therefore requires an explicit tour, which is what
    backtesting (step 12) needs.
    """
    tours = list(
        session.execute(
            select(FantasyTour)
            .where(FantasyTour.season_id == season_id)
            .order_by(FantasyTour.starts_at.is_(None), FantasyTour.starts_at)
        ).scalars()
    )
    if not tours:
        raise FeaturesError(f"Season {season_id} has no tours")

    if tour_ref is not None:
        for tour in tours:
            if tour.fantasy_tour_id == tour_ref or tour.name == tour_ref:
                return tour
        raise FeaturesError(
            f"Tour '{tour_ref}' not found in season {season_id}"
        )

    for tour in tours:
        if (tour.status or "").upper() != "FINISHED":
            return tour
    raise FeaturesError(
        "Every tour in the season is FINISHED; pass an explicit --tour to build "
        "features for a historical tour"
    )


def _tour_cutoff(tour: FantasyTour, fixtures: list[Fixture]) -> datetime:
    """Cutoff before which data may be used: deadline, else start, else kickoff."""
    if tour.transfers_deadline_at is not None:
        return tour.transfers_deadline_at
    if tour.starts_at is not None:
        return tour.starts_at
    if fixtures:
        return min(fixture.scheduled_at for fixture in fixtures)
    raise FeaturesError(
        f"Tour {tour.fantasy_tour_id} has no deadline, start or scheduled match"
    )


def _load_fixtures(session, tour_id: int) -> list[Fixture]:
    rows = session.execute(
        select(
            Match.id,
            Match.scheduled_at,
            Match.home_club_id,
            Match.away_club_id,
        ).where(Match.tour_id == tour_id)
    ).all()
    fixtures: list[Fixture] = []
    for match_id, scheduled_at, home_club_id, away_club_id in rows:
        fixtures.append(
            Fixture(
                match_id=match_id,
                scheduled_at=scheduled_at,
                club_id=home_club_id,
                opponent_club_id=away_club_id,
                is_home=True,
            )
        )
        fixtures.append(
            Fixture(
                match_id=match_id,
                scheduled_at=scheduled_at,
                club_id=away_club_id,
                opponent_club_id=home_club_id,
                is_home=False,
            )
        )
    return fixtures


def _load_club_matches(
    session, season_id: int, run_id: int, cutoff: datetime
) -> dict[int, list[ClubMatch]]:
    rows = session.execute(
        select(
            ClubMatchStats.club_id,
            Match.id,
            Match.scheduled_at,
            ClubMatchStats.is_home,
            ClubMatchStats.goals_scored,
            ClubMatchStats.goals_conceded,
        )
        .join(Match, ClubMatchStats.match_id == Match.id)
        .where(
            Match.season_id == season_id,
            ClubMatchStats.ingestion_run_id == run_id,
            Match.scheduled_at < cutoff,
            ClubMatchStats.goals_scored.isnot(None),
            ClubMatchStats.goals_conceded.isnot(None),
        )
    ).all()
    by_club: dict[int, list[ClubMatch]] = {}
    for club_id, match_id, scheduled_at, is_home, gf, ga in rows:
        by_club.setdefault(club_id, []).append(
            ClubMatch(
                match_id=match_id,
                scheduled_at=scheduled_at,
                is_home=bool(is_home),
                goals_scored=gf,
                goals_conceded=ga,
            )
        )
    for matches in by_club.values():
        matches.sort(key=lambda m: m.scheduled_at, reverse=True)
    return by_club


def _load_appearances(
    session, season_id: int, run_id: int
) -> dict[int, list[Appearance]]:
    rows = session.execute(
        select(
            PlayerMatchStats.player_season_id,
            PlayerMatchStats.match_id,
            Match.scheduled_at,
            PlayerMatchStats.field_minutes,
            PlayerMatchStats.points,
            PlayerMatchStats.goals,
            PlayerMatchStats.assists,
            PlayerMatchStats.saves,
            PlayerMatchStats.ball_recoveries,
            PlayerMatchStats.yellow_cards,
        )
        .join(Match, PlayerMatchStats.match_id == Match.id)
        .join(PlayerSeason, PlayerMatchStats.player_season_id == PlayerSeason.id)
        .where(
            PlayerSeason.season_id == season_id,
            PlayerMatchStats.ingestion_run_id == run_id,
        )
    ).all()
    by_player: dict[int, list[Appearance]] = {}
    for row in rows:
        by_player.setdefault(row.player_season_id, []).append(
            Appearance(
                match_id=row.match_id,
                scheduled_at=row.scheduled_at,
                minutes=row.field_minutes,
                points=row.points,
                goals=row.goals,
                assists=row.assists,
                saves=row.saves,
                ball_recoveries=row.ball_recoveries,
                yellow_cards=row.yellow_cards,
            )
        )
    return by_player


def _load_snapshots(session, run_id: int) -> dict[int, dict[str, Any]]:
    rows = session.execute(
        select(
            FantasyPlayerSnapshot.player_season_id,
            FantasyPlayerSnapshot.availability_status,
            FantasyPlayerSnapshot.status_description,
            FantasyPlayerSnapshot.price,
            FantasyPlayerSnapshot.selected_by,
            FantasyPlayerSnapshot.form,
        ).where(FantasyPlayerSnapshot.ingestion_run_id == run_id)
    ).all()
    snapshots: dict[int, dict[str, Any]] = {}
    for player_season_id, status, description, price, selected_by, form in rows:
        snapshots[player_season_id] = {
            "availability_status": status,
            "status_description": description,
            "price": float(price) if price is not None else None,
            "selected_by": float(selected_by) if selected_by is not None else None,
            "form": form,
        }
    return snapshots


def _load_players(session, season_id: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(
            PlayerSeason.id,
            PlayerSeason.player_id,
            PlayerSeason.fantasy_player_id,
            PlayerSeason.role,
            PlayerSeason.current_season_club_id,
            Player.canonical_name,
        )
        .join(Player, PlayerSeason.player_id == Player.id)
        .where(PlayerSeason.season_id == season_id)
    ).all()
    return [
        {
            "player_season_id": pid,
            "player_id": player_id,
            "fantasy_player_id": fantasy_id,
            "role": role,
            "current_season_club_id": season_club_id,
            "player_name": name,
        }
        for pid, player_id, fantasy_id, role, season_club_id, name in rows
    ]


def _load_season_clubs(session, season_id: int) -> dict[int, dict[str, Any]]:
    rows = session.execute(
        select(
            SeasonClub.id,
            SeasonClub.club_id,
            SeasonClub.display_name,
        ).where(SeasonClub.season_id == season_id)
    ).all()
    return {
        season_club_id: {"club_id": club_id, "display_name": name}
        for season_club_id, club_id, name in rows
    }


# ---------------------------------------------------------------------------
# Cross-season sourcing (step 14).
# ---------------------------------------------------------------------------


def _role_priors(
    appearances_by_ps: dict[int, list[Appearance]],
    role_by_ps: dict[int, str],
) -> dict[str, RolePrior]:
    """Aggregate the prior season into per-role per-90 priors for newcomers.

    Rates are pooled across every appearance of a role (so they are minute-
    weighted), and ``mean_minutes`` is the average minutes of a single
    appearance. A role without any prior appearance yields an all-zero prior.
    """
    totals: dict[str, dict[str, int]] = {}
    for ps_id, appearances in appearances_by_ps.items():
        role = role_by_ps.get(ps_id)
        if role is None:
            continue
        agg = totals.setdefault(
            role,
            {
                "minutes": 0,
                "count": 0,
                "goals": 0,
                "assists": 0,
                "saves": 0,
                "recoveries": 0,
                "yellows": 0,
            },
        )
        for appearance in appearances:
            agg["minutes"] += appearance.minutes
            agg["count"] += 1
            agg["goals"] += appearance.goals
            agg["assists"] += appearance.assists
            agg["saves"] += appearance.saves
            agg["recoveries"] += appearance.ball_recoveries
            agg["yellows"] += appearance.yellow_cards

    priors: dict[str, RolePrior] = {}
    for role, agg in totals.items():
        minutes = agg["minutes"]
        priors[role] = RolePrior(
            goals_per90=per90(agg["goals"], minutes),
            assists_per90=per90(agg["assists"], minutes),
            saves_per90=per90(agg["saves"], minutes),
            recoveries_per90=per90(agg["recoveries"], minutes),
            yellows_per90=per90(agg["yellows"], minutes),
            mean_minutes=round(minutes / agg["count"], 2) if agg["count"] else 0.0,
        )
    return priors


def _resolve_prior_run(session, season: Season):
    """Return the active run of the season preceding ``season``, if any.

    A "prior season" is another season of the same competition with its own
    published (active) snapshot. The immediately-preceding season (the latest
    one starting before the target) is preferred; when none starts earlier the
    latest other season is used as a fallback. Returns ``None`` when the target
    season is the only one imported, which disables cross-season sourcing.
    """
    from .db.models import IngestionRun

    rows = session.execute(
        select(IngestionRun, Season.starts_at)
        .join(Season, IngestionRun.season_id == Season.id)
        .where(
            IngestionRun.is_active.is_(True),
            Season.competition_id == season.competition_id,
            Season.id != season.id,
        )
    ).all()
    if not rows:
        return None

    target_start = season.starts_at
    earlier = [
        item
        for item in rows
        if item[1] is not None
        and target_start is not None
        and item[1] < target_start
    ]
    pool = earlier or rows

    def _key(item):
        run, starts_at = item
        return (starts_at is not None, starts_at, run.id)

    return max(pool, key=_key)[0]


def _load_prior_context(session, prior_run, cutoff: datetime) -> PriorContext:
    """Load the prior season's history keyed by the cross-season identities."""
    season_id = prior_run.season_id
    run_id = prior_run.id
    appearances = _load_appearances(session, season_id, run_id)
    club_matches = _load_club_matches(session, season_id, run_id, cutoff)
    players = _load_players(session, season_id)
    season_clubs = _load_season_clubs(session, season_id)

    role_by_ps = {p["player_season_id"]: p["role"] for p in players}
    by_player_id: dict[int, PriorPlayer] = {}
    for player in players:
        season_club_id = player["current_season_club_id"]
        club_info = season_clubs.get(season_club_id) if season_club_id else None
        by_player_id[player["player_id"]] = PriorPlayer(
            player_season_id=player["player_season_id"],
            club_id=club_info["club_id"] if club_info else None,
            role=player["role"],
        )
    role_priors = _role_priors(appearances, role_by_ps)
    return PriorContext(
        run_id=run_id,
        season_id=season_id,
        appearances=appearances,
        club_matches=club_matches,
        by_player_id=by_player_id,
        role_priors=role_priors,
    )


def _resolve_history_source(
    player: dict[str, Any],
    active_club_id: int | None,
    *,
    cross_season: bool,
    appearances: dict[int, list[Appearance]],
    current_club_matches: dict[int, list[ClubMatch]],
    prior: PriorContext | None,
) -> dict[str, Any]:
    """Pick where one player's history comes from and how to label it.

    In a normal (started) season a player uses their own current-season
    history. While the target season has not started (``cross_season``), a
    player is instead sourced from the prior season by the shared ``player_id``:
    the appearances and the denominator club are those of the club they played
    for last season (so a transfer keeps their real track record), while the
    fixture, venue and opponent come from the active season. A player with no
    prior history is a newcomer and gets role priors.
    """
    active_ps_id = player["player_season_id"]
    if not cross_season or prior is None:
        return {
            "appearances": appearances.get(active_ps_id, []),
            "club_matches": current_club_matches.get(active_club_id, [])
            if active_club_id is not None
            else [],
            "stat_source": STAT_SOURCE_CURRENT,
            "is_newcomer": False,
            "newcomer_prior": None,
        }

    prior_player = prior.by_player_id.get(player["player_id"])
    prior_appearances = (
        prior.appearances.get(prior_player.player_season_id, [])
        if prior_player is not None
        else []
    )
    if prior_appearances:
        return {
            "appearances": prior_appearances,
            "club_matches": prior.club_matches.get(prior_player.club_id, [])
            if prior_player.club_id is not None
            else [],
            "stat_source": STAT_SOURCE_PRIOR,
            "is_newcomer": False,
            "newcomer_prior": None,
        }

    # Newcomer: registered in the active season with no prior-season history.
    return {
        "appearances": [],
        "club_matches": [],
        "stat_source": STAT_SOURCE_PRIOR,
        "is_newcomer": True,
        "newcomer_prior": prior.role_priors.get(player["role"]),
    }


# ---------------------------------------------------------------------------
# Strength aggregation.
# ---------------------------------------------------------------------------


def _club_strengths(
    club_matches: dict[int, list[ClubMatch]],
) -> tuple[dict[int, dict[str, float]], dict[str, float]]:
    """Per-club home/away attack & defence, plus league averages for fills."""
    league_home_gf: list[int] = []
    league_home_ga: list[int] = []
    league_away_gf: list[int] = []
    league_away_ga: list[int] = []
    for matches in club_matches.values():
        for match in matches:
            if match.is_home:
                league_home_gf.append(match.goals_scored)
                league_home_ga.append(match.goals_conceded)
            else:
                league_away_gf.append(match.goals_scored)
                league_away_ga.append(match.goals_conceded)

    league = {
        "home_attack": round(_mean(league_home_gf), 4),
        "home_defense": round(_mean(league_home_ga), 4),
        "away_attack": round(_mean(league_away_gf), 4),
        "away_defense": round(_mean(league_away_ga), 4),
    }

    strengths: dict[int, dict[str, float]] = {}
    for club_id, matches in club_matches.items():
        home = [m for m in matches if m.is_home]
        away = [m for m in matches if not m.is_home]
        strengths[club_id] = {
            "home_attack": round(_mean([m.goals_scored for m in home]), 4)
            if home
            else league["home_attack"],
            "home_defense": round(_mean([m.goals_conceded for m in home]), 4)
            if home
            else league["home_defense"],
            "away_attack": round(_mean([m.goals_scored for m in away]), 4)
            if away
            else league["away_attack"],
            "away_defense": round(_mean([m.goals_conceded for m in away]), 4)
            if away
            else league["away_defense"],
            "matches_home": len(home),
            "matches_away": len(away),
        }
    return strengths, league


def _venue_strength(
    club_id: int,
    is_home: bool,
    strengths: dict[int, dict[str, float]],
    league: dict[str, float],
) -> tuple[float, float]:
    """Return (attack, defence) for a club playing home or away."""
    stats = strengths.get(club_id)
    if is_home:
        if stats is None:
            return league["home_attack"], league["home_defense"]
        return stats["home_attack"], stats["home_defense"]
    if stats is None:
        return league["away_attack"], league["away_defense"]
    return stats["away_attack"], stats["away_defense"]


# ---------------------------------------------------------------------------
# Row builder.
# ---------------------------------------------------------------------------


def _build_row(
    *,
    player: dict[str, Any],
    fixture: Fixture,
    club_name: str,
    opponent_name: str,
    cutoff: datetime,
    target_match_ids: frozenset[int],
    appearances: list[Appearance],
    club_matches: list[ClubMatch],
    snapshot: dict[str, Any] | None,
    strengths: dict[int, dict[str, float]],
    league: dict[str, float],
    stat_source: str = STAT_SOURCE_CURRENT,
    is_newcomer: bool = False,
    newcomer_prior: RolePrior | None = None,
    cross_season: bool = False,
) -> dict[str, Any]:
    history = recent_before_cutoff(
        appearances, cutoff, exclude_match_ids=target_match_ids
    )
    appeared_ids = {a.match_id for a in history}

    row: dict[str, Any] = {
        "feature_version": FEATURE_VERSION,
        "player_season_id": player["player_season_id"],
        "fantasy_player_id": player["fantasy_player_id"],
        "player_name": player["player_name"],
        "role": player["role"],
        "club_id": fixture.club_id,
        "club_name": club_name,
        "tour_cutoff": cutoff.isoformat(),
        "stat_source": stat_source,
        "is_newcomer": is_newcomer,
        "is_home": fixture.is_home,
        "opponent_club_id": fixture.opponent_club_id,
        "opponent_name": opponent_name,
        "match_id": fixture.match_id,
        "match_scheduled_at": fixture.scheduled_at.isoformat(),
    }

    # Rolling windows.
    for window_size in ROLLING_WINDOWS:
        stats = _window_stats(history[:window_size])
        row[f"appearances_{window_size}"] = stats["appearances"]
        row[f"points_avg_{window_size}"] = stats["points_avg"]
        row[f"points_sum_{window_size}"] = stats["points_sum"]
        row[f"goals_sum_{window_size}"] = stats["goals_sum"]
        row[f"assists_sum_{window_size}"] = stats["assists_sum"]
        row[f"minutes_avg_{window_size}"] = stats["minutes_avg"]

    # Season-to-date totals and per-90 rates.
    total_minutes = sum(a.minutes for a in history)
    total_points = sum(a.points for a in history)
    total_goals = sum(a.goals for a in history)
    total_assists = sum(a.assists for a in history)
    total_saves = sum(a.saves for a in history)
    total_recoveries = sum(a.ball_recoveries for a in history)
    total_yellows = sum(a.yellow_cards for a in history)
    row["total_appearances"] = len(history)
    row["total_minutes"] = total_minutes
    row["total_points"] = total_points
    row["points_per90"] = per90(total_points, total_minutes)
    row["goals_per90"] = per90(total_goals, total_minutes)
    row["assists_per90"] = per90(total_assists, total_minutes)
    row["saves_per90"] = per90(total_saves, total_minutes)
    row["recoveries_per90"] = per90(total_recoveries, total_minutes)
    row["yellows_per90"] = per90(total_yellows, total_minutes)
    row["has_history"] = bool(history)

    # Appearance and start shares over the club's matches before cutoff.
    club_matches_before = len(club_matches)
    club_match_ids = [m.match_id for m in club_matches]
    appearances_in_club = sum(1 for mid in club_match_ids if mid in appeared_ids)
    minutes_by_match = {a.match_id: a.minutes for a in history}
    starts = sum(
        1
        for mid in club_match_ids
        if minutes_by_match.get(mid, 0) >= START_MINUTES_THRESHOLD
    )
    row["club_matches_before"] = club_matches_before
    row["appearance_share"] = (
        round(appearances_in_club / club_matches_before, 4)
        if club_matches_before
        else 0.0
    )
    row["start_share"] = (
        round(starts / club_matches_before, 4) if club_matches_before else 0.0
    )

    # Availability status from the active snapshot.
    status = snapshot["availability_status"] if snapshot else None
    is_available = (status or "").upper() not in UNAVAILABLE_STATUSES
    row["availability_status"] = status
    row["status_description"] = snapshot["status_description"] if snapshot else None
    row["price"] = snapshot["price"] if snapshot else None
    row["selected_by"] = snapshot["selected_by"] if snapshot else None
    row["form"] = snapshot["form"] if snapshot else None
    row["is_available"] = is_available

    # Appearance probability and expected minutes, estimated separately.
    recent_club_ids = club_match_ids[:AVAILABILITY_WINDOW]
    if not is_available:
        p_appearance = 0.0
    elif recent_club_ids:
        appeared_recent = sum(1 for mid in recent_club_ids if mid in appeared_ids)
        p_appearance = round(appeared_recent / len(recent_club_ids), 4)
    else:
        p_appearance = 0.0
    row["p_appearance"] = p_appearance

    recent_minutes = [a.minutes for a in history[:AVAILABILITY_WINDOW]]
    mean_recent_minutes = _mean(recent_minutes)
    row["expected_minutes"] = round(p_appearance * mean_recent_minutes, 2)

    # Rest days since the club's previous match. Meaningless when the history
    # comes from the prior season (the active club has not played yet), so it is
    # left null rather than reporting a several-month gap.
    if club_matches and not cross_season:
        last_match = max(club_matches, key=lambda m: m.scheduled_at)
        row["rest_days"] = (fixture.scheduled_at - last_match.scheduled_at).days
    else:
        row["rest_days"] = None

    # Club and opponent strength at the relevant venue.
    club_attack, club_defense = _venue_strength(
        fixture.club_id, fixture.is_home, strengths, league
    )
    opponent_attack, opponent_defense = _venue_strength(
        fixture.opponent_club_id, not fixture.is_home, strengths, league
    )
    row["club_attack"] = club_attack
    row["club_defense"] = club_defense
    row["opponent_attack"] = opponent_attack
    row["opponent_defense"] = opponent_defense

    if is_newcomer and newcomer_prior is not None:
        _apply_newcomer_prior(row, newcomer_prior, is_available=row["is_available"])

    return row


def _apply_newcomer_prior(
    row: dict[str, Any], prior: RolePrior, *, is_available: bool
) -> None:
    """Overwrite the empty history-derived features with role priors in place.

    A newcomer has no appearances, so every rolling / per-90 feature is zero.
    The event forecast (step 7) reads the per-90 rates, the appearance
    probability, the expected minutes and the full/sub split, so those are set
    from the position prior while the totals stay zero and ``has_history`` stays
    ``False`` (the row is explicitly flagged as a newcomer).
    """
    p_appearance = round(NEWCOMER_P_APPEARANCE if is_available else 0.0, 4)
    mean_minutes = max(0.0, prior.mean_minutes)
    full_ratio = min(max(mean_minutes / 90.0, 0.0), 1.0)

    row["p_appearance"] = p_appearance
    row["expected_minutes"] = round(p_appearance * mean_minutes, 2)
    row["appearance_share"] = p_appearance
    row["start_share"] = round(p_appearance * full_ratio, 4)
    row["goals_per90"] = round(prior.goals_per90 * NEWCOMER_RATE_FACTOR, 4)
    row["assists_per90"] = round(prior.assists_per90 * NEWCOMER_RATE_FACTOR, 4)
    row["saves_per90"] = round(prior.saves_per90 * NEWCOMER_RATE_FACTOR, 4)
    row["recoveries_per90"] = round(prior.recoveries_per90 * NEWCOMER_RATE_FACTOR, 4)
    # Yellow cards are a penalty, so they are not discounted (staying cautious).
    row["yellows_per90"] = round(prior.yellows_per90, 4)


def build_feature_dataset(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    season_ref: str | None = None,
    tour_ref: str | None = None,
    now: datetime | None = None,
    feature_version: str = FEATURE_VERSION,
) -> dict[str, Any]:
    """Build the leakage-free feature dataset for a target tour.

    Returns a JSON-serialisable report with metadata, the feature dictionary
    and one row per player whose club plays the target tour. The dataset is
    reproducible from ``(run_id, tour, feature_version)``.
    """
    generated_at = now or datetime.now(UTC)
    with session_scope(session_factory) as session:
        run = _resolve_run(session, run_id, season_ref)
        season_id = run.season_id
        season = session.get(Season, season_id)

        tour = resolve_target_tour(session, season_id, tour_ref)
        fixtures = _load_fixtures(session, tour.id)
        cutoff = _tour_cutoff(tour, fixtures)
        target_match_ids = frozenset(f.match_id for f in fixtures)

        club_matches = _load_club_matches(session, season_id, run.id, cutoff)
        appearances = _load_appearances(session, season_id, run.id)
        snapshots = _load_snapshots(session, run.id)
        players = _load_players(session, season_id)
        season_clubs = _load_season_clubs(session, season_id)

        # Cross-season sourcing (step 14): while the target season has no played
        # match before the cutoff, source history from the prior season instead
        # of returning an all-zero forecast. Once the season starts (any club
        # match exists before the cutoff) the pure current-season path is used,
        # so backtesting a finished season is unaffected.
        prior_run = _resolve_prior_run(session, season) if season else None
        cross_season = prior_run is not None and not club_matches
        prior = (
            _load_prior_context(session, prior_run, cutoff) if cross_season else None
        )

        strength_matches = prior.club_matches if cross_season and prior else club_matches
        strengths, league = _club_strengths(strength_matches)

        # Map a club id to its display name via any of its season-club rows.
        club_names: dict[int, str] = {}
        for info in season_clubs.values():
            club_names.setdefault(info["club_id"], info["display_name"])

        # Index fixtures by the club that plays in them.
        fixtures_by_club: dict[int, Fixture] = {}
        for fixture in fixtures:
            # A club appears once per tour; keep the earliest kickoff if not.
            existing = fixtures_by_club.get(fixture.club_id)
            if existing is None or fixture.scheduled_at < existing.scheduled_at:
                fixtures_by_club[fixture.club_id] = fixture

        rows: list[dict[str, Any]] = []
        players_without_fixture = 0
        newcomers = 0
        prior_sourced = 0
        for player in players:
            season_club_id = player["current_season_club_id"]
            club_info = season_clubs.get(season_club_id) if season_club_id else None
            club_id = club_info["club_id"] if club_info else None
            fixture = fixtures_by_club.get(club_id) if club_id is not None else None
            if fixture is None:
                players_without_fixture += 1
                continue
            source = _resolve_history_source(
                player,
                club_id,
                cross_season=cross_season,
                appearances=appearances,
                current_club_matches=club_matches,
                prior=prior,
            )
            if source["stat_source"] == STAT_SOURCE_PRIOR:
                prior_sourced += 1
            if source["is_newcomer"]:
                newcomers += 1
            rows.append(
                _build_row(
                    player=player,
                    fixture=fixture,
                    club_name=club_names.get(club_id, ""),
                    opponent_name=club_names.get(fixture.opponent_club_id, ""),
                    cutoff=cutoff,
                    target_match_ids=target_match_ids,
                    appearances=source["appearances"],
                    club_matches=source["club_matches"],
                    snapshot=snapshots.get(player["player_season_id"]),
                    strengths=strengths,
                    league=league,
                    stat_source=source["stat_source"],
                    is_newcomer=source["is_newcomer"],
                    newcomer_prior=source["newcomer_prior"],
                    cross_season=cross_season,
                )
            )

        rows.sort(key=lambda r: (r["club_name"], -r["points_sum_5"], r["player_name"]))

        return {
            "feature_version": feature_version,
            "generated_at": generated_at.isoformat(),
            "run_id": run.id,
            "season_id": season_id,
            "season": {
                "fantasy_id": season.fantasy_season_id if season else None,
                "name": season.name if season else None,
            },
            "tour": {
                "tour_id": tour.id,
                "fantasy_tour_id": tour.fantasy_tour_id,
                "name": tour.name,
                "status": tour.status,
            },
            "cutoff": cutoff.isoformat(),
            "cross_season": cross_season,
            "prior_run_id": prior.run_id if prior else None,
            "counts": {
                "rows": len(rows),
                "fixtures": len(target_match_ids),
                "players_without_fixture": players_without_fixture,
                "clubs_with_history": len(strength_matches),
                "prior_sourced": prior_sourced,
                "newcomers": newcomers,
            },
            "feature_dictionary": list(FEATURE_DICTIONARY),
            "rows": rows,
        }


__all__ = [
    "FEATURE_VERSION",
    "ROLES",
    "ROLLING_WINDOWS",
    "START_MINUTES_THRESHOLD",
    "AVAILABILITY_WINDOW",
    "UNAVAILABLE_STATUSES",
    "STAT_SOURCE_CURRENT",
    "STAT_SOURCE_PRIOR",
    "NEWCOMER_P_APPEARANCE",
    "NEWCOMER_RATE_FACTOR",
    "FEATURE_DICTIONARY",
    "FeaturesError",
    "Appearance",
    "ClubMatch",
    "Fixture",
    "RolePrior",
    "PriorPlayer",
    "PriorContext",
    "recent_before_cutoff",
    "per90",
    "resolve_target_tour",
    "build_feature_dataset",
]
