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
FEATURE_VERSION = "1.0.0"

# Rolling look-back windows (in appearances) required by the plan.
ROLLING_WINDOWS = (3, 5, 10)

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
    {"name": "club_matches_before", "description": "Club matches played before cutoff (denominator for share features)."},
    {"name": "appearance_share", "description": "Share of the club's matches the player appeared in; 0.0 when the club has no prior match."},
    {"name": "start_share", "description": "Share of the club's matches the player started (>= 60 minutes); 0.0 when none."},
    {"name": "p_appearance", "description": "Estimated probability of playing the fixture from the last 5 club matches; 0.0 when unavailable."},
    {"name": "expected_minutes", "description": "Expected minutes: p_appearance x recent mean minutes when appearing."},
    {"name": "club_attack", "description": "Club goals scored per match at the fixture venue (home/away); league mean when no venue matches yet."},
    {"name": "club_defense", "description": "Club goals conceded per match at the fixture venue; league mean when no venue matches yet."},
    {"name": "opponent_attack", "description": "Opponent goals scored per match at their fixture venue; league mean fallback."},
    {"name": "opponent_defense", "description": "Opponent goals conceded per match at their fixture venue; league mean fallback."},
    {"name": "has_history", "description": "True when the player has at least one appearance before cutoff."},
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
        )
        .join(Match, PlayerMatchStats.match_id == Match.id)
        .join(PlayerSeason, PlayerMatchStats.player_season_id == PlayerSeason.id)
        .where(
            PlayerSeason.season_id == season_id,
            PlayerMatchStats.ingestion_run_id == run_id,
        )
    ).all()
    by_player: dict[int, list[Appearance]] = {}
    for player_season_id, match_id, scheduled_at, minutes, points, goals, assists in rows:
        by_player.setdefault(player_season_id, []).append(
            Appearance(
                match_id=match_id,
                scheduled_at=scheduled_at,
                minutes=minutes,
                points=points,
                goals=goals,
                assists=assists,
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
            "fantasy_player_id": fantasy_id,
            "role": role,
            "current_season_club_id": season_club_id,
            "player_name": name,
        }
        for pid, fantasy_id, role, season_club_id, name in rows
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
    row["total_appearances"] = len(history)
    row["total_minutes"] = total_minutes
    row["total_points"] = total_points
    row["points_per90"] = per90(total_points, total_minutes)
    row["goals_per90"] = per90(total_goals, total_minutes)
    row["assists_per90"] = per90(total_assists, total_minutes)
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

    # Rest days since the club's previous match.
    if club_matches:
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

    return row


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

        strengths, league = _club_strengths(club_matches)

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
        for player in players:
            season_club_id = player["current_season_club_id"]
            club_info = season_clubs.get(season_club_id) if season_club_id else None
            club_id = club_info["club_id"] if club_info else None
            fixture = fixtures_by_club.get(club_id) if club_id is not None else None
            if fixture is None:
                players_without_fixture += 1
                continue
            rows.append(
                _build_row(
                    player=player,
                    fixture=fixture,
                    club_name=club_names.get(club_id, ""),
                    opponent_name=club_names.get(fixture.opponent_club_id, ""),
                    cutoff=cutoff,
                    target_match_ids=target_match_ids,
                    appearances=appearances.get(player["player_season_id"], []),
                    club_matches=club_matches.get(club_id, []),
                    snapshot=snapshots.get(player["player_season_id"]),
                    strengths=strengths,
                    league=league,
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
            "counts": {
                "rows": len(rows),
                "fixtures": len(target_match_ids),
                "players_without_fixture": players_without_fixture,
                "clubs_with_history": len(club_matches),
            },
            "feature_dictionary": list(FEATURE_DICTIONARY),
            "rows": rows,
        }


__all__ = [
    "FEATURE_VERSION",
    "ROLLING_WINDOWS",
    "START_MINUTES_THRESHOLD",
    "AVAILABILITY_WINDOW",
    "UNAVAILABLE_STATUSES",
    "FEATURE_DICTIONARY",
    "FeaturesError",
    "Appearance",
    "ClubMatch",
    "Fixture",
    "recent_before_cutoff",
    "per90",
    "resolve_target_tour",
    "build_feature_dataset",
]
