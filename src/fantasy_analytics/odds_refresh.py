"""Fetch Sports.ru 1x2 lines and rebuild the next-tour forecast from them.

A league's odds are pulled from the football calendar widget the day before
that league's next tour starts (the in-process nightly loop), and on demand
from the admin «Обновить котировки» button. Persistence is a small upsert;
the effect on squads is entirely through
:func:`fantasy_analytics.forecast.run_forecast`, which blends the stored
means into ``expected_points``.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .client import GraphQLRequestError, SportsGraphQLClient
from .competitions import DEFAULT_TOURNAMENT_SLUG, resolve_slug
from .db import session_scope
from .db.models import Competition, FantasyTour, Match, Season
from .db.odds_repository import OddsRepository
from .nightly_refresh import NightlyRefreshSettings, list_imported_active_leagues
from .odds import parse_line1x2
from .queries import (
    CALENDAR_ODDS_OPERATION,
    CALENDAR_ODDS_QUERY,
    TOURNAMENT_HUB_QUERY,
)

logger = logging.getLogger(__name__)

# Upcoming (and in-play) fixtures; finished matches have no line we can use.
_ODDS_STATUSES = ("LIVE", "NOT_STARTED", "POSTPONED", "DELAYED")

# Bookmaker geo for the calendar widget. RU is where Sports.ru's lead
# bookmaker (Winline) is licensed; other ISO codes still return a line but
# may pick a different partner.
DEFAULT_ODDS_COUNTRY = "RU"

_SEASON_SUFFIX = re.compile(r"_\d{2}-\d{2}$")


class OddsRefreshError(RuntimeError):
    """Raised when the calendar / odds payload cannot be used."""


def hub_tournament_id(stat_season_id: str) -> str:
    """Strip the ``_YY-YY`` suffix off a stat season id to get the hub id.

    ``rfpl_26-27`` → ``rfpl``, ``serie_a_25-26`` → ``serie_a``. A value that
    is already a hub id is returned unchanged.
    """
    stripped = _SEASON_SUFFIX.sub("", str(stat_season_id or "").strip())
    return stripped or str(stat_season_id)


def season_slug_from_name(name: str) -> str:
    """Sports.ru calendar slugs use a hyphen: ``2026/2027`` → ``2026-2027``."""
    return str(name or "").strip().replace("/", "-")


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def flatten_calendar_matches(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Walk ``groupMatches.days.matches`` into a flat list of match dicts."""
    tournament = (
        ((payload.get("data") or {}).get("statQueries") or {}).get("football") or {}
    ).get("tournament") or {}
    season = tournament.get("seasonBySlug") or tournament.get("currentSeason") or {}
    groups = season.get("groupMatches") or []
    matches: list[dict[str, Any]] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        stage = group.get("stageName")
        for day in group.get("days") or []:
            if not isinstance(day, dict):
                continue
            date = day.get("date")
            for match in day.get("matches") or []:
                if isinstance(match, dict):
                    matches.append({**match, "stage_name": stage, "day": date})
    return matches


def extract_match_odds(match: dict[str, Any]) -> dict[str, Any] | None:
    """Parse one calendar match into a persistable odds row, or ``None``."""
    stat_match_id = str(match.get("id") or "").strip()
    if not stat_match_id:
        return None
    entries = match.get("bettingOdds") or []
    parsed = None
    if isinstance(entries, list):
        for entry in entries:
            parsed = parse_line1x2(entry)
            if parsed is not None:
                break
    if parsed is None:
        return None
    home = (match.get("home") or {}).get("team") or {}
    away = (match.get("away") or {}).get("team") or {}
    return {
        "stat_match_id": stat_match_id,
        "home_stat_team_id": str(home.get("id") or "") or None,
        "away_stat_team_id": str(away.get("id") or "") or None,
        "scheduled_at": match.get("scheduledAt"),
        "stage_name": match.get("stage_name"),
        "bookmaker": parsed.get("bookmaker"),
        "home_odds": parsed["home_odds"],
        "draw_odds": parsed["draw_odds"],
        "away_odds": parsed["away_odds"],
        "implied_home": parsed["implied_home"],
        "implied_draw": parsed["implied_draw"],
        "implied_away": parsed["implied_away"],
        "expected_home_goals": parsed["expected_home_goals"],
        "expected_away_goals": parsed["expected_away_goals"],
        "raw": {
            "match_status": match.get("matchStatus"),
            "scheduled_at": match.get("scheduledAt"),
            "home_name": home.get("name"),
            "away_name": away.get("name"),
            "stage_name": match.get("stage_name"),
            "bookmaker": parsed.get("bookmaker"),
        },
    }


def _next_unfinished_tour(session: Session, season_id: int) -> FantasyTour | None:
    from sqlalchemy import func

    return session.execute(
        select(FantasyTour)
        .where(
            FantasyTour.season_id == season_id,
            func.upper(func.coalesce(FantasyTour.status, "")) != "FINISHED",
        )
        .order_by(
            FantasyTour.starts_at.is_(None),
            FantasyTour.starts_at,
            FantasyTour.id,
        )
        .limit(1)
    ).scalar_one_or_none()


def tour_kickoff(tour: FantasyTour) -> datetime | None:
    """The instant a tour is treated as having started."""
    return tour.starts_at or tour.transfers_deadline_at


def is_day_before_tour(
    tour: FantasyTour,
    now: datetime,
    tz,
    *,
    include_start_day: bool = True,
) -> bool:
    """True when ``now`` is the local calendar day before (or of) kickoff.

    ``include_start_day`` is the catch-up: a process that missed yesterday
    still fetches on the morning of the tour rather than waiting a week.
    """
    kickoff = tour_kickoff(tour)
    if kickoff is None:
        return False
    local_now = now.astimezone(tz).date()
    local_start = kickoff.astimezone(tz).date()
    if local_now == local_start - timedelta(days=1):
        return True
    return include_start_day and local_now == local_start


def resolve_imported_season(session: Session, competition: Competition) -> Season | None:
    """Prefer the active imported season, else the newest imported one."""
    seasons = list(
        session.execute(
            select(Season)
            .where(Season.competition_id == competition.id)
            .order_by(
                Season.is_active.desc(),
                Season.starts_at.is_(None),
                Season.starts_at.desc(),
                Season.id.desc(),
            )
        ).scalars()
    )
    return seasons[0] if seasons else None


def resolve_sports_tag(
    client: SportsGraphQLClient,
    session: Session,
    *,
    competition: Competition,
    season: Season,
) -> str:
    """Return the Sports.ru tag id the calendar widget wants.

    Cached on ``competitions.sports_tag_id`` after the first successful hub
    lookup so a button press does not need two GraphQL round-trips.
    """
    cached = str(competition.sports_tag_id or "").strip()
    if cached:
        return cached
    hub_id = hub_tournament_id(season.stat_season_id)
    try:
        payload = client.execute(TOURNAMENT_HUB_QUERY, {"id": hub_id})
    except GraphQLRequestError as error:
        raise OddsRefreshError(
            f"Could not resolve Sports.ru tag for {hub_id!r}: {error}"
        ) from error
    tournament = (
        ((payload.get("data") or {}).get("statQueries") or {}).get("football") or {}
    ).get("tournament") or {}
    tag = (tournament.get("ubersetzer") or {}).get("sportsTag")
    if tag is None or str(tag).strip() == "":
        raise OddsRefreshError(
            f"Sports.ru hub tournament {hub_id!r} has no sportsTag"
        )
    competition.sports_tag_id = str(tag)
    session.flush()
    return str(tag)


def fetch_calendar_odds(
    client: SportsGraphQLClient,
    *,
    sports_tag_id: str,
    season_name: str,
    iso2_country: str = DEFAULT_ODDS_COUNTRY,
) -> list[dict[str, Any]]:
    """Download the upcoming calendar with 1x2 lines for one tournament tag."""
    slug = season_slug_from_name(season_name)
    variables = {
        "tournamentTagId": str(sports_tag_id),
        "seasonSlug": slug,
        "hasSeasonSlug": bool(slug),
        "statuses": list(_ODDS_STATUSES),
        "iso2Country": iso2_country,
        "withOdds": True,
    }
    try:
        payload = client.execute(
            CALENDAR_ODDS_QUERY,
            variables,
            operation_name=CALENDAR_ODDS_OPERATION,
        )
    except GraphQLRequestError as error:
        raise OddsRefreshError(f"Calendar odds request failed: {error}") from error
    matches = flatten_calendar_matches(payload)
    if matches or not slug:
        return matches
    # The imported season name did not resolve (or has no upcoming fixtures);
    # fall back to whatever Sports.ru currently lists as the live season.
    variables["hasSeasonSlug"] = False
    payload = client.execute(
        CALENDAR_ODDS_QUERY,
        variables,
        operation_name=CALENDAR_ODDS_OPERATION,
    )
    return flatten_calendar_matches(payload)


def _index_matches(session: Session, season_id: int) -> dict[str, Match]:
    rows = session.execute(select(Match).where(Match.season_id == season_id)).scalars()
    return {str(match.stat_match_id): match for match in rows}


def persist_match_odds(
    session: Session,
    *,
    season: Season,
    extracted: list[dict[str, Any]],
    captured_at: datetime,
) -> dict[str, int]:
    """Upsert parsed calendar lines, joining them by ``stat_match_id``.

    Club-pair fallback is deliberately not used: the same home/away pairing
    repeats every season, so a 2026/2027 line would otherwise attach to a
    2025/2026 fixture. Unmatched rows are still stored so a later import of
    the current season can join them.
    """
    by_stat = _index_matches(session, season.id)
    rows: list[dict[str, Any]] = []
    linked = 0
    skipped = 0
    for item in extracted:
        match = by_stat.get(item["stat_match_id"])
        if match is None:
            skipped += 1
        else:
            linked += 1
        rows.append(
            {
                "season_id": season.id,
                "match_id": match.id if match is not None else None,
                "tour_id": match.tour_id if match is not None else None,
                "stat_match_id": item["stat_match_id"],
                "home_stat_team_id": item["home_stat_team_id"],
                "away_stat_team_id": item["away_stat_team_id"],
                "bookmaker": item["bookmaker"],
                "home_odds": _decimal(item["home_odds"]),
                "draw_odds": _decimal(item["draw_odds"]),
                "away_odds": _decimal(item["away_odds"]),
                "implied_home": _decimal(item["implied_home"]),
                "implied_draw": _decimal(item["implied_draw"]),
                "implied_away": _decimal(item["implied_away"]),
                "expected_home_goals": _decimal(item["expected_home_goals"]),
                "expected_away_goals": _decimal(item["expected_away_goals"]),
                "captured_at": captured_at,
                "raw": item["raw"],
            }
        )
    written = OddsRepository(session).upsert_many(rows)
    season.odds_synced_at = captured_at
    session.flush()
    return {
        "fetched": len(extracted),
        "stored": written,
        "linked": linked,
        "unmatched": skipped,
    }


def _rebuild_target_forecast(
    session_factory: sessionmaker,
    *,
    season_id: int,
    now: datetime,
) -> dict[str, Any]:
    """Recompute expected points for the next unfinished tour of ``season``."""
    from .db.models import IngestionRun
    from .forecast_service import ensure_tour_forecasts

    with session_scope(session_factory) as session:
        tour = _next_unfinished_tour(session, season_id)
        if tour is None:
            return {"tour_id": None, "forecast_rows": 0}
        run = session.execute(
            select(IngestionRun).where(
                IngestionRun.season_id == season_id,
                IngestionRun.is_active.is_(True),
            )
        ).scalar_one_or_none()
        payload = {
            "tour_id": tour.id,
            "tour_name": tour.name,
            "run_id": run.id if run is not None else None,
        }
    if payload["run_id"] is None:
        payload["forecast_rows"] = 0
        return payload
    payload["forecast_rows"] = ensure_tour_forecasts(
        session_factory,
        run_id=payload["run_id"],
        tour_id=payload["tour_id"],
        now=now,
        force=True,
    )
    return payload


def refresh_league_odds(
    client: SportsGraphQLClient,
    session_factory: sessionmaker,
    *,
    tournament_slug: str = DEFAULT_TOURNAMENT_SLUG,
    now: datetime | None = None,
    iso2_country: str = DEFAULT_ODDS_COUNTRY,
    rebuild_forecasts: bool = True,
) -> dict[str, Any]:
    """Fetch, persist and (optionally) re-forecast one league's upcoming lines."""
    moment = now or datetime.now(UTC)
    with session_scope(session_factory) as session:
        competition = resolve_slug(session, tournament_slug)
        if competition is None:
            raise OddsRefreshError(f"Unknown tournament slug {tournament_slug!r}")
        season = resolve_imported_season(session, competition)
        if season is None:
            raise OddsRefreshError(
                f"League {tournament_slug!r} has no imported season to attach odds to"
            )
        sports_tag_id = resolve_sports_tag(
            client, session, competition=competition, season=season
        )
        season_id = season.id
        season_name = season.name
        competition_name = competition.name

    calendar = fetch_calendar_odds(
        client,
        sports_tag_id=sports_tag_id,
        season_name=season_name,
        iso2_country=iso2_country,
    )
    extracted = [row for row in (extract_match_odds(m) for m in calendar) if row]
    with session_scope(session_factory) as session:
        season = session.get(Season, season_id)
        if season is None:
            raise OddsRefreshError(f"Season {season_id} disappeared during odds refresh")
        counts = persist_match_odds(
            session, season=season, extracted=extracted, captured_at=moment
        )
        odds_synced_at = season.odds_synced_at.isoformat() if season.odds_synced_at else None

    forecast: dict[str, Any] = {"tour_id": None, "forecast_rows": 0}
    if rebuild_forecasts:
        forecast = _rebuild_target_forecast(
            session_factory, season_id=season_id, now=moment
        )

    report = {
        "tournament_slug": tournament_slug,
        "competition_name": competition_name,
        "season_id": season_id,
        "season_name": season_name,
        "sports_tag_id": sports_tag_id,
        "synced_at": odds_synced_at or moment.isoformat(),
        "calendar_matches": len(calendar),
        **counts,
        **forecast,
    }
    logger.info(
        "Odds refresh for %s: fetched %s, stored %s, linked %s, forecasts %s",
        tournament_slug,
        counts["fetched"],
        counts["stored"],
        counts["linked"],
        forecast.get("forecast_rows") or 0,
    )
    return report


def odds_status(session: Session, *, tournament_slug: str) -> dict[str, Any] | None:
    """Compact odds block for the admin status endpoint."""
    competition = resolve_slug(session, tournament_slug)
    if competition is None:
        return None
    season = resolve_imported_season(session, competition)
    if season is None:
        return None
    repo = OddsRepository(session)
    tour = _next_unfinished_tour(session, season.id)
    return {
        "season_id": season.id,
        "synced_at": season.odds_synced_at.isoformat() if season.odds_synced_at else None,
        "matches": repo.count_for_season(season.id),
        "tour_id": tour.id if tour is not None else None,
        "tour_name": tour.name if tour is not None else None,
        "sports_tag_id": competition.sports_tag_id,
    }


def refresh_due_odds(
    client: SportsGraphQLClient,
    session_factory: sessionmaker,
    *,
    settings: NightlyRefreshSettings | None = None,
    now: datetime | None = None,
    force: bool = False,
    rebuild_forecasts: bool = True,
) -> dict[str, Any]:
    """Refresh odds for every imported active league whose next tour is due.

    ``force`` refreshes every eligible league regardless of kickoff day
    (the admin "run now" analogue); the nightly loop leaves it false so a
    league is only hit the day before its own tour starts.
    """
    settings = settings or NightlyRefreshSettings()
    moment = now or datetime.now(UTC)
    with session_scope(session_factory) as session:
        eligible = list_imported_active_leagues(session)

    refreshed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for league in eligible:
        with session_scope(session_factory) as session:
            tour = _next_unfinished_tour(session, league.season_id)
            if tour is None:
                skipped.append(
                    {
                        "tournament_slug": league.tournament_slug,
                        "reason": "no_upcoming_tour",
                    }
                )
                continue
            due = force or is_day_before_tour(tour, moment, settings.tz)
            if not due:
                skipped.append(
                    {
                        "tournament_slug": league.tournament_slug,
                        "reason": "not_day_before_tour",
                    }
                )
                continue
        try:
            report = refresh_league_odds(
                client,
                session_factory,
                tournament_slug=league.tournament_slug,
                now=moment,
                rebuild_forecasts=rebuild_forecasts,
            )
        except OddsRefreshError as error:
            logger.warning(
                "Odds refresh skipped for %s: %s", league.tournament_slug, error
            )
            skipped.append(
                {
                    "tournament_slug": league.tournament_slug,
                    "reason": str(error),
                }
            )
            continue
        refreshed.append(report)

    return {
        "ran_at": moment.astimezone(UTC).isoformat(),
        "force": force,
        "refreshed": refreshed,
        "skipped": skipped,
    }


__all__ = [
    "DEFAULT_ODDS_COUNTRY",
    "OddsRefreshError",
    "extract_match_odds",
    "fetch_calendar_odds",
    "flatten_calendar_matches",
    "hub_tournament_id",
    "is_day_before_tour",
    "odds_status",
    "persist_match_odds",
    "refresh_due_odds",
    "refresh_league_odds",
    "resolve_imported_season",
    "resolve_sports_tag",
    "season_slug_from_name",
    "tour_kickoff",
]
