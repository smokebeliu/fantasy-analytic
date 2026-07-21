"""Orchestrate live data discovery and produce normalized samples."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import SportsGraphQLClient
from .queries import (
    PLAYER_HISTORY_QUERY,
    PLAYERS_QUERY,
    SEASON_QUERY,
    TOURNAMENT_QUERY,
    build_team_stats_query,
)


class DiscoveryError(RuntimeError):
    """Raised when the live response does not satisfy the expected contract."""


@dataclass(frozen=True)
class DiscoveryOptions:
    output_dir: Path
    tournament_slug: str = "russia"
    season_id: str | None = None
    season_name: str | None = None
    use_current_season: bool = False
    player_page_size: int = 100
    history_samples_per_role: int = 1
    history_page_size: int = 100


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiscoveryError(f"Expected an object at {path}")
    return value


def _season_sort_key(season: dict[str, Any]) -> tuple[int, ...]:
    name = str((season.get("statObject") or {}).get("name") or "")
    years = tuple(int(part) for part in name.split("/") if part.isdigit())
    return years or (0,)


def _select_season(
    tournament: dict[str, Any],
    options: DiscoveryOptions,
) -> dict[str, Any]:
    seasons = tournament.get("seasons")
    if not isinstance(seasons, list) or not seasons:
        raise DiscoveryError("Tournament does not contain seasons")

    if options.season_id:
        candidates = [
            season for season in seasons if str(season.get("id")) == options.season_id
        ]
    elif options.season_name:
        candidates = [
            season
            for season in seasons
            if str((season.get("statObject") or {}).get("name"))
            == options.season_name
        ]
    elif options.use_current_season:
        candidates = [season for season in seasons if season.get("isActive") is True]
    else:
        candidates = [season for season in seasons if season.get("isActive") is False]
        candidates.sort(key=_season_sort_key, reverse=True)

    if not candidates:
        raise DiscoveryError("Requested season was not found")
    return _require_mapping(candidates[0], "tournament.seasons[]")


def _is_match_finished(match_status: Any) -> bool:
    """True only for a fully played match whose score is authoritative.

    Sports.ru returns ``score: 0`` (not ``null``) for a not-yet-played match, so
    the score alone cannot tell a real 0:0 from an unplayed fixture. The
    ``matchStatus`` field disambiguates: only ``CLOSED`` matches carry a final
    score. Anything else (``NOT_STARTED``, live, postponed, ...) must be treated
    as having no score so unplayed fixtures never pollute results or team form.
    """
    return str(match_status or "").upper() == "CLOSED"


def _flatten_matches(season: dict[str, Any]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for tour in season.get("tours") or []:
        for match in tour.get("matches") or []:
            matches.append(
                {
                    "id": match.get("id"),
                    "tour_id": tour.get("id"),
                    "tour_name": tour.get("name"),
                    "tour_status": tour.get("status"),
                    "scheduled_at": match.get("scheduledAt"),
                    "match_status": match.get("matchStatus"),
                    "home": match.get("home"),
                    "away": match.get("away"),
                }
            )
    return matches


def _derive_team_match_stats(
    teams: list[dict[str, Any]],
    matches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names_by_stat_id = {
        str(team["statObject"]["id"]): team.get("name")
        for team in teams
        if isinstance(team.get("statObject"), dict)
        and team["statObject"].get("id") is not None
    }
    aggregates: dict[str, dict[str, Any]] = {
        team_id: {
            "stat_team_id": team_id,
            "name": name,
            "matches": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "goals_for": 0,
            "goals_against": 0,
            "clean_sheets": 0,
            "home_matches": 0,
            "home_goals_for": 0,
            "home_goals_against": 0,
            "away_matches": 0,
            "away_goals_for": 0,
            "away_goals_against": 0,
        }
        for team_id, name in names_by_stat_id.items()
    }

    for match in matches:
        if not _is_match_finished(match.get("match_status")):
            continue
        home = match.get("home") or {}
        away = match.get("away") or {}
        if home.get("score") is None or away.get("score") is None:
            continue

        home_id = str((home.get("team") or {}).get("id"))
        away_id = str((away.get("team") or {}).get("id"))
        if home_id not in aggregates or away_id not in aggregates:
            continue

        home_score = int(home["score"])
        away_score = int(away["score"])
        sides = (
            (aggregates[home_id], home_score, away_score, "home"),
            (aggregates[away_id], away_score, home_score, "away"),
        )
        for aggregate, goals_for, goals_against, venue in sides:
            aggregate["matches"] += 1
            aggregate["goals_for"] += goals_for
            aggregate["goals_against"] += goals_against
            aggregate[f"{venue}_matches"] += 1
            aggregate[f"{venue}_goals_for"] += goals_for
            aggregate[f"{venue}_goals_against"] += goals_against
            aggregate["clean_sheets"] += int(goals_against == 0)
            if goals_for > goals_against:
                aggregate["wins"] += 1
            elif goals_for == goals_against:
                aggregate["draws"] += 1
            else:
                aggregate["losses"] += 1

    return sorted(aggregates.values(), key=lambda item: item["name"] or "")


def _select_history_samples(
    players: list[dict[str, Any]],
    samples_per_role: int,
) -> list[dict[str, Any]]:
    if samples_per_role <= 0:
        return []

    selected: list[dict[str, Any]] = []
    for role in ("GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD"):
        candidates = [
            player
            for player in players
            if player.get("role") == role
            and player.get("team")
            and (player.get("gameStat") or {}).get("fieldMinutes", 0) > 0
        ]
        candidates.sort(
            key=lambda player: (
                (player.get("gameStat") or {}).get("points", 0),
                (player.get("gameStat") or {}).get("fieldMinutes", 0),
            ),
            reverse=True,
        )
        selected.extend(candidates[:samples_per_role])
    return selected


def _fetch_player_history(
    client: SportsGraphQLClient,
    raw_dir: Path,
    season_id: str,
    player: dict[str, Any],
    page_size: int,
) -> dict[str, Any]:
    page = 1
    matches: list[dict[str, Any]] = []
    player_info: dict[str, Any] = {
        "id": player.get("id"),
        "name": player.get("name"),
        "role": player.get("role"),
    }

    while True:
        payload = client.execute(
            PLAYER_HISTORY_QUERY,
            {
                "seasonID": season_id,
                "playerID": str(player["id"]),
                "pageNum": page,
                "pageSize": page_size,
            },
        )
        _write_json(
            raw_dir / f"player-history-{player['id']}-page-{page:03d}.json",
            payload,
        )
        season = (
            payload.get("data", {})
            .get("fantasyQueries", {})
            .get("season")
            or {}
        )
        player_list = ((season.get("players") or {}).get("list") or [])
        if not player_list:
            break

        history = player_list[0].get("matches") or {}
        matches.extend(history.get("matches") or [])
        page_info = history.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        page += 1

    player_info["matches"] = matches
    return player_info


def _normalize_players(players: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for player in players:
        normalized.append(
            {
                "fantasy_player_id": player.get("id"),
                "stat_player_id": (player.get("statObject") or {}).get("id"),
                "name": player.get("name"),
                "role": player.get("role"),
                "price": player.get("price"),
                "fantasy_team_id": (player.get("team") or {}).get("id"),
                "stat_team_id": (
                    (player.get("team") or {}).get("statObject") or {}
                ).get("id"),
                "team_name": (player.get("team") or {}).get("name"),
                "status": player.get("status"),
                "score": player.get("seasonScoreInfo"),
                "stats": player.get("gameStat"),
            }
        )
    return normalized


def _normalize_team_season_stats(
    payload: dict[str, Any],
    stat_teams: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seasons = payload.get("data", {}).get("stat_season") or []
    if not seasons:
        return []

    source_season = seasons[0]
    normalized: list[dict[str, Any]] = []
    for index, team in enumerate(stat_teams):
        normalized.append(
            {
                "fantasy_team_id": team.get("id"),
                "stat_team_id": (team.get("statObject") or {}).get("id"),
                "name": team.get("name"),
                "stats": source_season.get(f"team{index}"),
            }
        )
    return normalized


def _build_report(
    tournament: dict[str, Any],
    season: dict[str, Any],
    players: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    histories: list[dict[str, Any]],
) -> dict[str, Any]:
    role_counts: dict[str, int] = {}
    players_with_minutes = 0
    players_without_team = 0
    for player in players:
        role = str(player.get("role") or "UNKNOWN")
        role_counts[role] = role_counts.get(role, 0) + 1
        players_with_minutes += int(
            (player.get("gameStat") or {}).get("fieldMinutes", 0) > 0
        )
        players_without_team += int(not player.get("team"))

    history_matches = [
        match for history in histories for match in history.get("matches", [])
    ]
    details_count = sum(
        len(match.get("statDetails") or []) for match in history_matches
    )
    tours = season.get("tours") or []
    teams = (season.get("info") or {}).get("teams") or []
    tour_limits = sorted(
        {
            (
                (tour.get("constraints") or {}).get("totalTransfers"),
                (tour.get("constraints") or {}).get("maxSameTeamPlayers"),
            )
            for tour in tours
        }
    )

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "tournament": {
            "id": tournament.get("id"),
            "slug": tournament.get("webName"),
            "name": tournament.get("name"),
        },
        "season": {
            "fantasy_id": season.get("id"),
            "stat_id": (season.get("statObject") or {}).get("id"),
            "name": (season.get("statObject") or {}).get("name"),
            "is_active": season.get("isActive"),
        },
        "counts": {
            "teams": len(teams),
            "tours": len(tours),
            "matches": len(matches),
            "players": len(players),
            "players_with_minutes": players_with_minutes,
            "players_without_team": players_without_team,
            "sampled_player_histories": len(histories),
            "sampled_player_matches": len(history_matches),
            "sampled_point_details": details_count,
        },
        "players_by_role": dict(sorted(role_counts.items())),
        "season_constraints": (season.get("info") or {}).get("constraints"),
        "observed_tour_limits": [
            {
                "total_transfers": total_transfers,
                "max_same_team_players": max_same_team_players,
            }
            for total_transfers, max_same_team_players in tour_limits
        ],
        "model_findings": [
            "Fantasy player IDs belong to a season context; stat player IDs are the best available cross-season identity candidate.",
            "Fantasy team IDs and stat team IDs use different namespaces and must both be stored.",
            "Roster and transfer constraints vary by season or tour and must not be hard-coded.",
            "Player price, status, ownership and form are mutable snapshot attributes.",
            "Fantasy aggregate stats and per-match stats share fields but have different grains.",
            "Clean sheets and home/away club form can be derived from match scores.",
            "The stat API exposes club season totals independently of fantasy data.",
        ],
    }


def run_discovery(
    client: SportsGraphQLClient,
    options: DiscoveryOptions,
) -> dict[str, Any]:
    """Fetch the selected season and write raw plus normalized artifacts."""
    raw_dir = options.output_dir / "raw"
    normalized_dir = options.output_dir / "normalized"

    tournament_payload = client.execute(
        TOURNAMENT_QUERY,
        {"id": options.tournament_slug},
    )
    _write_json(raw_dir / "tournament.json", tournament_payload)
    tournament = _require_mapping(
        tournament_payload.get("data", {})
        .get("fantasyQueries", {})
        .get("tournament"),
        "data.fantasyQueries.tournament",
    )
    selected_season = _select_season(tournament, options)
    season_id = str(selected_season["id"])

    season_payload = client.execute(SEASON_QUERY, {"seasonID": season_id})
    _write_json(raw_dir / "season.json", season_payload)
    season = _require_mapping(
        season_payload.get("data", {})
        .get("fantasyQueries", {})
        .get("season"),
        "data.fantasyQueries.season",
    )

    players: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = client.execute(
            PLAYERS_QUERY,
            {
                "seasonID": season_id,
                "pageNum": page,
                "pageSize": options.player_page_size,
            },
        )
        _write_json(raw_dir / f"players-page-{page:03d}.json", payload)
        players_page = _require_mapping(
            payload.get("data", {})
            .get("fantasyQueries", {})
            .get("players"),
            "data.fantasyQueries.players",
        )
        players.extend(players_page.get("list") or [])
        page_info = players_page.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        page += 1

    teams = (season.get("info") or {}).get("teams") or []
    stat_teams = [
        team for team in teams if (team.get("statObject") or {}).get("id")
    ]
    team_variables: dict[str, Any] = {
        "seasonID": [str((season.get("statObject") or {}).get("id"))]
    }
    for index, team in enumerate(stat_teams):
        team_variables[f"team{index}"] = str(team["statObject"]["id"])
    if stat_teams:
        team_stats_payload = client.execute(
            build_team_stats_query(len(stat_teams)),
            team_variables,
        )
    else:
        team_stats_payload = {"data": {"stat_season": []}}
    _write_json(raw_dir / "team-season-stats.json", team_stats_payload)

    history_samples = _select_history_samples(
        players,
        options.history_samples_per_role,
    )
    histories = [
        _fetch_player_history(
            client,
            raw_dir,
            season_id,
            player,
            options.history_page_size,
        )
        for player in history_samples
    ]

    matches = _flatten_matches(season)
    normalized_players = _normalize_players(players)
    normalized_team_stats = _normalize_team_season_stats(
        team_stats_payload,
        stat_teams,
    )
    derived_team_stats = _derive_team_match_stats(teams, matches)
    report = _build_report(tournament, season, players, matches, histories)

    _write_json(normalized_dir / "season.json", season)
    _write_json(normalized_dir / "players.json", normalized_players)
    _write_json(normalized_dir / "matches.json", matches)
    _write_json(
        normalized_dir / "team-season-stats.json",
        normalized_team_stats,
    )
    _write_json(normalized_dir / "player-history-samples.json", histories)
    _write_json(
        normalized_dir / "derived-team-match-stats.json",
        derived_team_stats,
    )
    _write_json(options.output_dir / "report.json", report)
    return report
