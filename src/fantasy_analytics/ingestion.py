"""Full historical import of one Sports.ru fantasy season (development-plan step 2).

The season belongs to whichever competition ``IngestionOptions.tournament_slug``
names — the RPL by default, any of the leagues in the catalogue
(:mod:`fantasy_analytics.competitions`) otherwise. Nothing in the importer is
league-specific: rules, roster limits, club and tour counts all come from the
payload, so a 38-tour La Liga season and a 9-tour Champions League knockout stage
go through the same code.

The importer runs in two clearly separated stages:

* **Fetch** — every GraphQL page (tournament, season, all player pages, club
  season aggregates and every player's match history) is downloaded up front,
  reusing the retry/backoff built into :class:`SportsGraphQLClient` and a bounded
  thread pool for the many per-player history requests. Nothing touches the
  database yet, so a failed page simply aborts the run.
* **Persist** — the collected payloads are normalized and written inside a single
  transaction owned by the caller. Catalog rows are upserted idempotently by
  external identifier; run-scoped snapshots are inserted fresh. Because the whole
  stage is one transaction, a failure never publishes a partial snapshot.

The public entry point :func:`run_ingestion` wires this together with the
``ingestion_runs`` bookkeeping so each import is versioned and auditable.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import sessionmaker

from .client import SportsGraphQLClient
from .competitions import DEFAULT_TOURNAMENT_SLUG, catalogue_seasons
from .db import session_scope
from .db.import_repository import DomainImportRepository
from .db.repository import IngestionRepository
from .discovery import (
    DiscoveryOptions,
    DiscoveryError,
    _derive_team_match_stats,
    _flatten_matches,
    _is_match_finished,
    _normalize_team_season_stats,
    _require_mapping,
    _select_season,
)
from .queries import (
    PLAYER_HISTORY_QUERY,
    PLAYERS_QUERY,
    SEASON_QUERY,
    TOURNAMENT_QUERY,
    build_team_stats_query,
)

ProgressCallback = Callable[[str], None]

# GraphQL statistic field name -> model column shared by both stat grains.
_STAT_FIELD_MAP = {
    "points": "points",
    "goals": "goals",
    "assists": "assists",
    "saves": "saves",
    "penaltiesMissed": "penalties_missed",
    "penaltiesPost": "penalties_post",
    "penaltiesTarget": "penalties_target",
    "penaltiesSaved": "penalties_saved",
    "fieldMinutes": "field_minutes",
    "yellowCards": "yellow_cards",
    "redCards": "red_cards",
    "goalsConceded": "goals_conceded",
    "penaltyGoalsConceded": "penalty_goals_conceded",
    "penaltiesFaced": "penalties_faced",
    "penaltyConceded": "penalty_conceded",
    "ownGoals": "own_goals",
    "ballRecovery": "ball_recoveries",
}

_VALID_ROLES = {"GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD"}


@dataclass(frozen=True)
class IngestionOptions:
    tournament_slug: str = DEFAULT_TOURNAMENT_SLUG
    season_id: str | None = None
    season_name: str | None = None
    use_current_season: bool = False
    player_page_size: int = 100
    history_page_size: int = 100
    history_workers: int = 8


@dataclass
class FetchResult:
    tournament: dict[str, Any]
    season: dict[str, Any]
    players: list[dict[str, Any]]
    team_stats_payload: dict[str, Any]
    stat_teams: list[dict[str, Any]]
    histories: dict[str, list[dict[str, Any]]]
    raw_payloads: list[tuple[str, Any, Any]]
    timings: dict[str, float] = field(default_factory=dict)


def _noop(_message: str) -> None:
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _to_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    return datetime.fromisoformat(text)


def _map_stats(source: Any) -> dict[str, int]:
    stats = source or {}
    return {
        column: int(stats.get(graphql_name) or 0)
        for graphql_name, column in _STAT_FIELD_MAP.items()
    }


def fetch_all(
    client: SportsGraphQLClient,
    options: IngestionOptions,
    on_progress: ProgressCallback = _noop,
) -> FetchResult:
    """Download every payload required for a full season import."""
    raw_payloads: list[tuple[str, Any, Any]] = []
    timings: dict[str, float] = {}

    def record(operation: str, variables: Any, response: Any) -> None:
        raw_payloads.append((operation, variables, response))

    stage_start = time.monotonic()
    tournament_payload = client.execute(
        TOURNAMENT_QUERY, {"id": options.tournament_slug}
    )
    record("Tournament", {"id": options.tournament_slug}, tournament_payload)
    tournament = _require_mapping(
        tournament_payload.get("data", {})
        .get("fantasyQueries", {})
        .get("tournament"),
        "data.fantasyQueries.tournament",
    )
    selected_season = _select_season(
        tournament,
        DiscoveryOptions(
            output_dir=Path("."),
            tournament_slug=options.tournament_slug,
            season_id=options.season_id,
            season_name=options.season_name,
            use_current_season=options.use_current_season,
        ),
    )
    season_id = str(selected_season["id"])
    timings["tournament"] = time.monotonic() - stage_start
    on_progress(f"Selected season {season_id}")

    stage_start = time.monotonic()
    season_payload = client.execute(SEASON_QUERY, {"seasonID": season_id})
    record("Season", {"seasonID": season_id}, season_payload)
    season = _require_mapping(
        season_payload.get("data", {}).get("fantasyQueries", {}).get("season"),
        "data.fantasyQueries.season",
    )
    timings["season"] = time.monotonic() - stage_start

    stage_start = time.monotonic()
    players: list[dict[str, Any]] = []
    page = 1
    while True:
        variables = {
            "seasonID": season_id,
            "pageNum": page,
            "pageSize": options.player_page_size,
        }
        payload = client.execute(PLAYERS_QUERY, variables)
        record("Players", variables, payload)
        players_page = _require_mapping(
            payload.get("data", {}).get("fantasyQueries", {}).get("players"),
            "data.fantasyQueries.players",
        )
        players.extend(players_page.get("list") or [])
        page_info = players_page.get("pageInfo") or {}
        on_progress(f"Fetched player page {page} ({len(players)} players)")
        if not page_info.get("hasNextPage"):
            break
        page += 1
    timings["players"] = time.monotonic() - stage_start

    stage_start = time.monotonic()
    teams = (season.get("info") or {}).get("teams") or []
    stat_teams = [team for team in teams if (team.get("statObject") or {}).get("id")]
    if stat_teams:
        team_variables: dict[str, Any] = {
            "seasonID": [str((season.get("statObject") or {}).get("id"))]
        }
        for index, team in enumerate(stat_teams):
            team_variables[f"team{index}"] = str(team["statObject"]["id"])
        team_stats_payload = client.execute(
            build_team_stats_query(len(stat_teams)), team_variables
        )
        record("TeamSeasonStats", team_variables, team_stats_payload)
    else:
        team_stats_payload = {"data": {"stat_season": []}}
    timings["team_stats"] = time.monotonic() - stage_start

    stage_start = time.monotonic()
    history_targets = [
        player
        for player in players
        if int((player.get("gameStat") or {}).get("fieldMinutes") or 0) > 0
    ]
    on_progress(
        f"Fetching match history for {len(history_targets)} players "
        f"with {options.history_workers} workers"
    )
    histories: dict[str, list[dict[str, Any]]] = {}

    def fetch_history(player: dict[str, Any]) -> tuple[
        str, list[dict[str, Any]], list[tuple[str, Any, Any]]
    ]:
        fantasy_player_id = str(player["id"])
        matches: list[dict[str, Any]] = []
        local_raw: list[tuple[str, Any, Any]] = []
        history_page = 1
        while True:
            variables = {
                "seasonID": season_id,
                "playerID": fantasy_player_id,
                "pageNum": history_page,
                "pageSize": options.history_page_size,
            }
            payload = client.execute(PLAYER_HISTORY_QUERY, variables)
            local_raw.append(("PlayerHistory", variables, payload))
            season_data = (
                payload.get("data", {}).get("fantasyQueries", {}).get("season") or {}
            )
            player_list = (season_data.get("players") or {}).get("list") or []
            if not player_list:
                break
            history = player_list[0].get("matches") or {}
            matches.extend(history.get("matches") or [])
            page_info = history.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            history_page += 1
        return fantasy_player_id, matches, local_raw

    workers = max(1, options.history_workers)
    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for fantasy_player_id, matches, local_raw in executor.map(
            fetch_history, history_targets
        ):
            histories[fantasy_player_id] = matches
            raw_payloads.extend(local_raw)
            completed += 1
            if completed % 50 == 0 or completed == len(history_targets):
                on_progress(
                    f"Fetched history {completed}/{len(history_targets)}"
                )
    timings["histories"] = time.monotonic() - stage_start

    return FetchResult(
        tournament=tournament,
        season=season,
        players=players,
        team_stats_payload=team_stats_payload,
        stat_teams=stat_teams,
        histories=histories,
        raw_payloads=raw_payloads,
        timings=timings,
    )


def _persist(
    session,
    run,
    fetch: FetchResult,
    ingestion_repository: IngestionRepository,
    on_progress: ProgressCallback = _noop,
) -> dict[str, Any]:
    """Write all normalized entities inside the caller's transaction."""
    repo = DomainImportRepository(session)
    tournament = fetch.tournament
    season = fetch.season
    stat_object = season.get("statObject") or {}

    for operation_name, variables, response in fetch.raw_payloads:
        ingestion_repository.save_raw_response(
            run,
            operation_name=operation_name,
            variables=variables,
            response=response,
        )
    on_progress(f"Saved {len(fetch.raw_payloads)} raw responses")

    # The tournament payload already lists every season this league exposes, so
    # an import doubles as a catalogue refresh for its own competition and the
    # admin season picker never goes stale behind a successful refresh.
    competition_id = repo.upsert_competition(
        fantasy_tournament_id=str(tournament["id"]),
        slug=str(tournament.get("webName") or tournament.get("id")),
        name=str(tournament.get("name") or ""),
        available_seasons=[
            season.as_dict() for season in catalogue_seasons(tournament)
        ],
        catalogue_synced_at=datetime.now(UTC),
    )

    stat_seasons = (fetch.team_stats_payload.get("data") or {}).get("stat_season") or []
    stat_season = stat_seasons[0] if stat_seasons else {}
    season_pk = repo.upsert_season(
        competition_id=competition_id,
        fantasy_season_id=str(season["id"]),
        stat_season_id=str(stat_object.get("id")),
        name=str(stat_object.get("name") or ""),
        is_active=bool(season.get("isActive")),
        starts_at=_parse_dt(stat_season.get("startedAt")),
        ends_at=_parse_dt(stat_season.get("endedAt")),
    )

    info = season.get("info") or {}
    constraints = info.get("constraints") or {}
    repo.upsert_season_rules(
        season_id=season_pk,
        rules_html=str(season.get("rules") or ""),
        total_budget=_to_decimal(constraints.get("totalBalance")) or Decimal("0"),
        total_players=int(constraints.get("totalPlayersCount") or 0),
        starting_players=int(constraints.get("activePlayersCount") or 0),
        full_roster_constraints=constraints.get("fullRoster") or [],
        starting_roster_constraints=constraints.get("startingRoster") or [],
    )

    teams = info.get("teams") or []
    club_rows = [
        {
            "stat_team_id": str((team.get("statObject") or {}).get("id")),
            "canonical_name": str(
                (team.get("statObject") or {}).get("name") or team.get("name") or ""
            ),
        }
        for team in teams
        if (team.get("statObject") or {}).get("id")
    ]
    club_by_stat = repo.upsert_clubs(club_rows)

    season_club_rows = []
    fantasy_to_stat: dict[str, str] = {}
    for team in teams:
        stat_team_id = (team.get("statObject") or {}).get("id")
        if not stat_team_id:
            continue
        fantasy_team_id = str(team["id"])
        fantasy_to_stat[fantasy_team_id] = str(stat_team_id)
        season_club_rows.append(
            {
                "season_id": season_pk,
                "club_id": club_by_stat[str(stat_team_id)],
                "fantasy_team_id": fantasy_team_id,
                "display_name": str(team.get("name") or ""),
            }
        )
    season_club_by_fantasy = repo.upsert_season_clubs(season_club_rows)
    on_progress(f"Upserted {len(club_rows)} clubs")

    tours = season.get("tours") or []
    tour_rows = []
    for tour in tours:
        tour_constraints = tour.get("constraints") or {}
        tour_rows.append(
            {
                "season_id": season_pk,
                "fantasy_tour_id": str(tour["id"]),
                "name": str(tour.get("name") or ""),
                "status": str(tour.get("status") or ""),
                "starts_at": _parse_dt(tour.get("startedAt")),
                "finishes_at": _parse_dt(tour.get("finishedAt")),
                "transfers_start_at": _parse_dt(tour.get("transfersStartedAt")),
                "transfers_deadline_at": _parse_dt(tour.get("transfersFinishedAt")),
                "total_transfers": _to_int(tour_constraints.get("totalTransfers")),
                "max_same_team_players": _to_int(
                    tour_constraints.get("maxSameTeamPlayers")
                ),
            }
        )
    tour_by_fantasy = repo.upsert_tours(tour_rows)

    flattened_matches = _flatten_matches(season)
    match_rows = []
    for match in flattened_matches:
        home = match.get("home") or {}
        away = match.get("away") or {}
        home_stat = str((home.get("team") or {}).get("id"))
        away_stat = str((away.get("team") or {}).get("id"))
        if home_stat not in club_by_stat or away_stat not in club_by_stat:
            continue
        # Only a CLOSED match has an authoritative score; unplayed fixtures come
        # back as 0:0 and must be stored as null so they are not counted as
        # played draws (which would corrupt results, form and team strength).
        finished = _is_match_finished(match.get("match_status"))
        match_rows.append(
            {
                "season_id": season_pk,
                "tour_id": tour_by_fantasy.get(str(match.get("tour_id"))),
                "stat_match_id": str(match["id"]),
                "scheduled_at": _parse_dt(match.get("scheduled_at")),
                "home_club_id": club_by_stat[home_stat],
                "away_club_id": club_by_stat[away_stat],
                "home_score": _to_int(home.get("score")) if finished else None,
                "away_score": _to_int(away.get("score")) if finished else None,
            }
        )
    match_by_stat = repo.upsert_matches(match_rows)
    on_progress(f"Upserted {len(match_rows)} matches")

    club_match_rows = []
    for match in flattened_matches:
        stat_match_id = str(match["id"])
        match_pk = match_by_stat.get(stat_match_id)
        if match_pk is None:
            continue
        home = match.get("home") or {}
        away = match.get("away") or {}
        home_stat = str((home.get("team") or {}).get("id"))
        away_stat = str((away.get("team") or {}).get("id"))
        if home_stat not in club_by_stat or away_stat not in club_by_stat:
            continue
        finished = _is_match_finished(match.get("match_status"))
        home_score = _to_int(home.get("score")) if finished else None
        away_score = _to_int(away.get("score")) if finished else None
        club_match_rows.append(
            {
                "match_id": match_pk,
                "club_id": club_by_stat[home_stat],
                "opponent_club_id": club_by_stat[away_stat],
                "is_home": True,
                "goals_scored": home_score,
                "goals_conceded": away_score,
                "provider_metrics": {},
                "ingestion_run_id": run.id,
            }
        )
        club_match_rows.append(
            {
                "match_id": match_pk,
                "club_id": club_by_stat[away_stat],
                "opponent_club_id": club_by_stat[home_stat],
                "is_home": False,
                "goals_scored": away_score,
                "goals_conceded": home_score,
                "provider_metrics": {},
                "ingestion_run_id": run.id,
            }
        )
    repo.upsert_club_match_stats(club_match_rows)

    existing_players = repo.existing_player_ids(season_pk)
    players_with_stat = [
        {
            "stat_player_id": str((player.get("statObject") or {}).get("id")),
            "canonical_name": str(player.get("name") or ""),
        }
        for player in fetch.players
        if (player.get("statObject") or {}).get("id")
    ]
    player_by_stat = repo.upsert_players_by_stat(players_with_stat)

    player_season_rows = []
    for player in fetch.players:
        fantasy_player_id = str(player["id"])
        role = str(player.get("role") or "")
        if role not in _VALID_ROLES:
            raise DiscoveryError(f"Unexpected player role: {role!r}")
        stat_player_id = (player.get("statObject") or {}).get("id")
        if stat_player_id:
            player_pk = player_by_stat[str(stat_player_id)]
        elif fantasy_player_id in existing_players:
            player_pk = existing_players[fantasy_player_id]
        else:
            player_pk = repo.create_player(
                canonical_name=str(player.get("name") or "")
            )
        fantasy_team_id = str((player.get("team") or {}).get("id") or "")
        player_season_rows.append(
            {
                "season_id": season_pk,
                "player_id": player_pk,
                "fantasy_player_id": fantasy_player_id,
                "role": role,
                "current_season_club_id": season_club_by_fantasy.get(fantasy_team_id),
            }
        )
    player_season_by_fantasy = repo.upsert_player_seasons(player_season_rows)
    on_progress(f"Upserted {len(player_season_rows)} player seasons")

    snapshot_rows = []
    season_stats_rows = []
    for player in fetch.players:
        fantasy_player_id = str(player["id"])
        player_season_pk = player_season_by_fantasy[fantasy_player_id]
        fantasy_team_id = str((player.get("team") or {}).get("id") or "")
        status = player.get("status") or {}
        score = player.get("seasonScoreInfo") or {}
        snapshot_rows.append(
            {
                "player_season_id": player_season_pk,
                "ingestion_run_id": run.id,
                "season_club_id": season_club_by_fantasy.get(fantasy_team_id),
                "price": _to_decimal(player.get("price")) or Decimal("0"),
                "availability_status": str(status.get("status") or "UNKNOWN"),
                "status_description": str(status.get("description") or ""),
                "selected_by": _to_decimal(status.get("selectedBy")),
                "form": _to_int(status.get("form")),
                "rank": _to_int(score.get("place")),
                "season_score": _to_int(score.get("score")),
                "average_score": _to_decimal(score.get("averageScore")),
                "last_tour_score": _to_int(score.get("scoreForLastTour")),
                "top_percent": _to_decimal(score.get("topPercent")),
            }
        )
        season_stats_rows.append(
            {
                "player_season_id": player_season_pk,
                "ingestion_run_id": run.id,
                **_map_stats(player.get("gameStat")),
            }
        )
    repo.insert_player_snapshots(snapshot_rows)
    repo.insert_player_season_stats(season_stats_rows)

    normalized_team_stats = _normalize_team_season_stats(
        fetch.team_stats_payload, fetch.stat_teams
    )
    derived_by_stat = {
        item["stat_team_id"]: item
        for item in _derive_team_match_stats(teams, flattened_matches)
    }
    club_season_rows = []
    for entry in normalized_team_stats:
        fantasy_team_id = str(entry.get("fantasy_team_id") or "")
        season_club_pk = season_club_by_fantasy.get(fantasy_team_id)
        if season_club_pk is None:
            continue
        provider = entry.get("stats") or {}
        derived = derived_by_stat.get(str(entry.get("stat_team_id")), {})

        def pick(provider_key: str, derived_key: str) -> int:
            value = provider.get(provider_key)
            if value is None:
                value = derived.get(derived_key)
            return int(value or 0)

        club_season_rows.append(
            {
                "season_club_id": season_club_pk,
                "ingestion_run_id": run.id,
                "matches_played": pick("MatchesPlayed", "matches"),
                "matches_won": pick("MatchesWon", "wins"),
                "matches_drawn": pick("MatchesDrawn", "draws"),
                "matches_lost": pick("MatchesLost", "losses"),
                "goals_scored": pick("GoalsScored", "goals_for"),
                "goals_conceded": pick("GoalsConceded", "goals_against"),
                "yellow_cards": int(provider.get("YellowCards") or 0),
                "red_cards": int(provider.get("RedCards") or 0),
                "clean_sheets": _to_int(derived.get("clean_sheets")),
                "home_matches": _to_int(derived.get("home_matches")),
                "home_goals_scored": _to_int(derived.get("home_goals_for")),
                "home_goals_conceded": _to_int(derived.get("home_goals_against")),
                "away_matches": _to_int(derived.get("away_matches")),
                "away_goals_scored": _to_int(derived.get("away_goals_for")),
                "away_goals_conceded": _to_int(derived.get("away_goals_against")),
            }
        )
    repo.insert_club_season_stats(club_season_rows)

    match_stats_rows = []
    detail_specs: list[tuple[str, int, list[dict[str, Any]]]] = []
    skipped_history_matches = 0
    for fantasy_player_id, entries in fetch.histories.items():
        player_season_pk = player_season_by_fantasy.get(fantasy_player_id)
        if player_season_pk is None:
            continue
        for entry in entries:
            match_meta = entry.get("match") or {}
            stat_match_id = str(match_meta.get("id"))
            match_pk = match_by_stat.get(stat_match_id)
            tour_pk = tour_by_fantasy.get(str((entry.get("tour") or {}).get("id")))
            if match_pk is None or tour_pk is None:
                skipped_history_matches += 1
                continue
            fantasy_team_id = str((entry.get("team") or {}).get("id") or "")
            match_stats_rows.append(
                {
                    "player_season_id": player_season_pk,
                    "match_id": match_pk,
                    "tour_id": tour_pk,
                    "season_club_id": season_club_by_fantasy.get(fantasy_team_id),
                    "ingestion_run_id": run.id,
                    **_map_stats(entry.get("playerMatchInfo")),
                }
            )
            details = entry.get("statDetails") or []
            if details:
                detail_specs.append((fantasy_player_id, match_pk, details))
    player_match_ids = repo.upsert_player_match_stats(match_stats_rows)
    on_progress(f"Upserted {len(match_stats_rows)} player-match stats")

    detail_rows = []
    for fantasy_player_id, match_pk, details in detail_specs:
        player_season_pk = player_season_by_fantasy[fantasy_player_id]
        stat_pk = player_match_ids[(player_season_pk, match_pk)]
        for ordinal, detail in enumerate(details):
            detail_rows.append(
                {
                    "player_match_stat_id": stat_pk,
                    "ordinal": ordinal,
                    "reason": str(detail.get("reason") or ""),
                    "score": int(detail.get("score") or 0),
                }
            )
    repo.upsert_point_details(detail_rows)

    players_with_minutes = sum(
        1
        for player in fetch.players
        if int((player.get("gameStat") or {}).get("fieldMinutes") or 0) > 0
    )
    return {
        "season_id": season_pk,
        "counts": {
            "clubs": len(club_rows),
            "season_clubs": len(season_club_rows),
            "tours": len(tour_rows),
            "matches": len(match_rows),
            "club_match_stats": len(club_match_rows),
            "players": len(fetch.players),
            "player_seasons": len(player_season_rows),
            "player_snapshots": len(snapshot_rows),
            "player_season_stats": len(season_stats_rows),
            "club_season_stats": len(club_season_rows),
            "player_match_stats": len(match_stats_rows),
            "point_details": len(detail_rows),
            "raw_responses": len(fetch.raw_payloads),
            "players_with_minutes": players_with_minutes,
            "histories_fetched": len(fetch.histories),
            "skipped_history_matches": skipped_history_matches,
        },
    }


def run_ingestion(
    client: SportsGraphQLClient,
    session_factory: sessionmaker,
    options: IngestionOptions,
    on_progress: ProgressCallback = _noop,
) -> dict[str, Any]:
    """Execute a full historical import and return the run report.

    The ingestion run row is committed independently of the domain snapshot so a
    failure still records a ``failed`` run while the domain transaction rolls
    back, leaving no partially published data.
    """
    with session_scope(session_factory) as session:
        run = IngestionRepository(session).create_run(
            tournament_slug=options.tournament_slug,
            requested_season_id=options.season_id,
        )
        run_id = run.id
    on_progress(f"Created ingestion run {run_id}")

    with session_scope(session_factory) as session:
        ingestion_repository = IngestionRepository(session)
        ingestion_repository.mark_running(ingestion_repository.get_run(run_id))

    started_at = time.monotonic()
    try:
        fetch = fetch_all(client, options, on_progress)
        fetch_seconds = sum(fetch.timings.values())
        on_progress(f"Fetch stage finished in {fetch_seconds:.1f}s")

        persist_start = time.monotonic()
        with session_scope(session_factory) as session:
            ingestion_repository = IngestionRepository(session)
            run = ingestion_repository.get_run(run_id)
            summary = _persist(
                session, run, fetch, ingestion_repository, on_progress
            )
        persist_seconds = time.monotonic() - persist_start
    except Exception as error:
        with session_scope(session_factory) as session:
            ingestion_repository = IngestionRepository(session)
            ingestion_repository.mark_failed(
                ingestion_repository.get_run(run_id), str(error)
            )
        raise

    report = {
        "run_id": run_id,
        "season": {
            "fantasy_id": str(fetch.season["id"]),
            "stat_id": str((fetch.season.get("statObject") or {}).get("id")),
            "name": str((fetch.season.get("statObject") or {}).get("name")),
            "is_active": bool(fetch.season.get("isActive")),
        },
        "counts": summary["counts"],
        "durations_seconds": {
            **{f"fetch_{key}": round(value, 3) for key, value in fetch.timings.items()},
            "fetch_total": round(fetch_seconds, 3),
            "persist": round(persist_seconds, 3),
            "total": round(time.monotonic() - started_at, 3),
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }

    with session_scope(session_factory) as session:
        ingestion_repository = IngestionRepository(session)
        ingestion_repository.mark_succeeded(
            ingestion_repository.get_run(run_id), report=report
        )
    on_progress("Ingestion run marked succeeded")
    return report


__all__ = ["IngestionOptions", "FetchResult", "fetch_all", "run_ingestion"]
