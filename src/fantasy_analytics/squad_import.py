"""Load a manager's Sports.ru fantasy team from its public URL.

The squad builder needs a starting XI+bench so it can suggest transfers for the
next tour. Sports.ru already publishes that roster on
``/fantasy/football/{slug}/{squadId}/``; this module parses the link, fetches
``currentTourInfo`` from GraphQL and maps the player ids onto the imported
snapshot.

League matching is mandatory: a Portugal team must not populate an RPL pitch.
The URL slug is checked first (no network) and the tournament ``webName`` from
the payload is checked again after the fetch.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .client import GraphQLRequestError
from .competitions import DEFAULT_TOURNAMENT_SLUG
from .queries import LEAGUE_PROBE_QUERY, SQUAD_QUERY

# ``rpl`` is the project's historical alias for the Sports.ru ``russia`` slug.
_SLUG_ALIASES = {
    "rpl": DEFAULT_TOURNAMENT_SLUG,
}

# /fantasy/football/{slug}/{squadId} with an optional trailing slash / query.
_SQUAD_PATH_RE = re.compile(
    r"^/fantasy/football/(?P<slug>[a-z0-9_-]+)/(?P<squad_id>\d+)/?$",
    re.IGNORECASE,
)
_BARE_ID_RE = re.compile(r"^\d+$")


class SquadImportError(Exception):
    """A user-facing failure while importing a Sports.ru team link."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        type_: str = "bad_request",
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.type_ = type_
        self.details = details


@dataclass(frozen=True)
class ParsedSquadLink:
    """What a pasted Sports.ru team URL (or a bare squad id) resolved to."""

    squad_id: str
    slug: str | None


@dataclass(frozen=True)
class RemoteSquadPlayer:
    fantasy_player_id: str
    name: str
    role: str | None
    price: float | None
    is_starter: bool
    is_captain: bool
    is_vice_captain: bool
    substitute_priority: int | None
    club_name: str | None


@dataclass(frozen=True)
class RemoteSquad:
    squad_id: str
    name: str
    slug: str
    league_name: str
    season_id: str
    tour: dict[str, Any] | None
    players: tuple[RemoteSquadPlayer, ...]
    # The manager's money and transfer allowance as Sports.ru reports them for
    # the current tour: what the roster is worth, what is left in the bank,
    # and how many free transfers remain. ``None`` when the payload omits one.
    total_price: float | None = None
    current_balance: float | None = None
    transfers_left: int | None = None
    transfers_done: int | None = None

    @property
    def budget(self) -> float | None:
        """Team value plus bank: the money a transfer plan may actually spend."""
        if self.total_price is None or self.current_balance is None:
            return None
        return round(self.total_price + self.current_balance, 2)


FetchPayload = Callable[[str], dict[str, Any]]
ResolvePlayers = Callable[[Sequence[str]], list[dict[str, Any]]]


def canonical_slug(slug: str | None) -> str:
    """Lowercase a Sports.ru tournament slug and apply the RPL alias."""
    if not slug:
        return ""
    folded = slug.strip().lower()
    return _SLUG_ALIASES.get(folded, folded)


def slugs_match(left: str | None, right: str | None) -> bool:
    """True when two slugs name the same league (``rpl`` == ``russia``)."""
    return bool(left) and bool(right) and canonical_slug(left) == canonical_slug(right)


def parse_squad_url(raw: str) -> ParsedSquadLink:
    """Extract the squad id (and league slug, when present) from a pasted link.

    Accepts the public team page, the same path without a host, or a bare
    numeric id. Extra path segments (``/ratings/...``) are rejected so a league
    ratings URL is not silently treated as a team.
    """
    text = (raw or "").strip()
    if not text:
        raise SquadImportError(
            "Вставьте ссылку на команду Sports.ru.",
            type_="invalid_squad_url",
        )
    if _BARE_ID_RE.fullmatch(text):
        return ParsedSquadLink(squad_id=text, slug=None)
    if text.startswith("/fantasy/football/"):
        text = f"https://www.sports.ru{text}"

    parsed = urlparse(text if "://" in text else f"https://{text}")
    host = (parsed.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host and host != "sports.ru":
        raise SquadImportError(
            "Ссылка должна вести на sports.ru. "
            "Пример: https://www.sports.ru/fantasy/football/portugal/588960/",
            type_="invalid_squad_url",
        )
    path = parsed.path.rstrip("/") or "/"
    # Re-add a single trailing slash so the regex can treat both forms equally.
    match = _SQUAD_PATH_RE.match(parsed.path if parsed.path.endswith("/") else f"{path}/")
    if match is None:
        raise SquadImportError(
            "Ссылка не похожа на команду Sports.ru. "
            "Пример: https://www.sports.ru/fantasy/football/portugal/588960/",
            type_="invalid_squad_url",
        )
    return ParsedSquadLink(
        squad_id=match.group("squad_id"),
        slug=match.group("slug").lower(),
    )


def extract_remote_squad(payload: dict[str, Any]) -> RemoteSquad | None:
    """Turn a ``SQUAD_QUERY`` response into a typed roster, or ``None``."""
    squads = ((payload.get("data") or {}).get("fantasyQueries") or {}).get("squads")
    if not isinstance(squads, list) or not squads:
        return None
    raw = squads[0]
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    season = raw.get("season") or {}
    tournament = season.get("tournament") or {}
    slug = str(tournament.get("webName") or "").strip().lower()
    tour_info = raw.get("currentTourInfo") or {}
    tour_raw = tour_info.get("tour") if isinstance(tour_info, dict) else None
    tour = None
    if isinstance(tour_raw, dict) and tour_raw.get("id"):
        tour = {
            "fantasy_tour_id": str(tour_raw["id"]),
            "name": str(tour_raw.get("name") or ""),
            "status": str(tour_raw.get("status") or ""),
        }
    players: list[RemoteSquadPlayer] = []
    seen: set[str] = set()
    for entry in (tour_info.get("players") or []) if isinstance(tour_info, dict) else []:
        if not isinstance(entry, dict):
            continue
        season_player = entry.get("seasonPlayer") or {}
        player_id = str(season_player.get("id") or "").strip()
        if not player_id or player_id in seen:
            continue
        seen.add(player_id)
        team = season_player.get("team") or {}
        price = season_player.get("price")
        players.append(
            RemoteSquadPlayer(
                fantasy_player_id=player_id,
                name=str(season_player.get("name") or ""),
                role=str(season_player["role"]) if season_player.get("role") else None,
                price=float(price) if price is not None else None,
                is_starter=bool(entry.get("isStarting")),
                is_captain=bool(entry.get("isCaptain")),
                is_vice_captain=bool(entry.get("isViceCaptain")),
                substitute_priority=(
                    int(entry["substitutePriority"])
                    if entry.get("substitutePriority") is not None
                    else None
                ),
                club_name=str(team.get("name") or "") or None,
            )
        )
    money = tour_info if isinstance(tour_info, dict) else {}
    return RemoteSquad(
        squad_id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        slug=slug,
        league_name=str(tournament.get("name") or slug),
        season_id=str(season.get("id") or ""),
        tour=tour,
        players=tuple(players),
        total_price=_optional_float(money.get("totalPrice")),
        current_balance=_optional_float(money.get("currentBalance")),
        transfers_left=_optional_int(money.get("transfersLeft")),
        transfers_done=_optional_int(money.get("transfersDone")),
    )


def _optional_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _extract_league_name(payload: dict[str, Any]) -> str | None:
    league = ((payload.get("data") or {}).get("fantasyQueries") or {}).get("league")
    if isinstance(league, dict) and league.get("id"):
        return str(league.get("name") or league["id"])
    return None


def _league_mismatch(
    *,
    expected_slug: str,
    found_slug: str,
    found_name: str | None = None,
) -> SquadImportError:
    expected = canonical_slug(expected_slug)
    found = canonical_slug(found_slug)
    label = found_name or found
    return SquadImportError(
        f"Лига в ссылке ({label}) не совпадает с выбранной лигой ({expected}).",
        status=409,
        type_="league_mismatch",
        details={
            "expected_slug": expected,
            "found_slug": found,
            "found_name": found_name,
        },
    )


def import_squad_from_url(
    *,
    url: str,
    expected_slug: str,
    fetch_squad: FetchPayload,
    resolve_players: ResolvePlayers,
    fetch_league: FetchPayload | None = None,
) -> dict[str, Any]:
    """Parse the link, fetch the live roster and resolve it against the snapshot.

    ``fetch_squad`` / ``fetch_league`` are injected so tests never need the
    network. ``resolve_players`` is the local snapshot lookup (fantasy id →
    player card for the tour the UI is currently editing).
    """
    parsed = parse_squad_url(url)
    if parsed.slug and not slugs_match(parsed.slug, expected_slug):
        raise _league_mismatch(expected_slug=expected_slug, found_slug=parsed.slug)

    try:
        payload = fetch_squad(parsed.squad_id)
    except GraphQLRequestError as error:
        raise SquadImportError(
            "Не удалось получить состав с Sports.ru.",
            status=502,
            type_="upstream_error",
            details=str(error),
        ) from error

    remote = extract_remote_squad(payload)
    if remote is None:
        if fetch_league is not None:
            try:
                league_payload = fetch_league(parsed.squad_id)
            except GraphQLRequestError:
                league_payload = {}
            league_name = _extract_league_name(league_payload)
            if league_name:
                raise SquadImportError(
                    "Это ссылка на лигу, а не на команду. "
                    "Откройте страницу своей команды на Sports.ru и скопируйте её адрес.",
                    status=400,
                    type_="not_a_squad",
                    details={"league_name": league_name},
                )
        raise SquadImportError(
            "Команда не найдена на Sports.ru.",
            status=404,
            type_="not_found",
        )

    if not slugs_match(remote.slug, expected_slug):
        raise _league_mismatch(
            expected_slug=expected_slug,
            found_slug=remote.slug,
            found_name=remote.league_name,
        )
    if not remote.players:
        raise SquadImportError(
            "У команды нет состава на текущий тур.",
            status=422,
            type_="empty_squad",
        )

    resolved = resolve_players([player.fantasy_player_id for player in remote.players])
    by_id = {
        item["fantasy_player_id"]: item
        for item in resolved
        if item.get("fantasy_player_id")
    }
    players: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for remote_player in remote.players:
        local = by_id.get(remote_player.fantasy_player_id)
        if local is None:
            missing.append(
                {
                    "fantasy_player_id": remote_player.fantasy_player_id,
                    "player_name": remote_player.name or None,
                    "role": remote_player.role,
                }
            )
            continue
        players.append(local)

    return {
        "squad_id": remote.squad_id,
        "squad_name": remote.name,
        "competition_slug": remote.slug,
        "competition_name": remote.league_name,
        "remote_season_id": remote.season_id,
        "remote_tour": remote.tour,
        "players": players,
        "missing": missing,
        # Money and transfers as the game sees them, so the builder can plan
        # from the manager's real budget (team value + bank) and the transfers
        # actually left rather than the season's opening budget and the tour's
        # full allowance.
        "total_price": remote.total_price,
        "current_balance": remote.current_balance,
        "budget": remote.budget,
        "transfers_left": remote.transfers_left,
        "transfers_done": remote.transfers_done,
    }


def graphql_fetch_squad(client: Any, squad_id: str) -> dict[str, Any]:
    """Execute ``SQUAD_QUERY`` through a Sports.ru GraphQL client."""
    return client.execute(SQUAD_QUERY, {"squadID": squad_id})


def graphql_fetch_league(client: Any, league_id: str) -> dict[str, Any]:
    """Execute ``LEAGUE_PROBE_QUERY`` through a Sports.ru GraphQL client."""
    return client.execute(LEAGUE_PROBE_QUERY, {"id": league_id})


__all__ = [
    "ParsedSquadLink",
    "RemoteSquad",
    "RemoteSquadPlayer",
    "SquadImportError",
    "canonical_slug",
    "extract_remote_squad",
    "graphql_fetch_league",
    "graphql_fetch_squad",
    "import_squad_from_url",
    "parse_squad_url",
    "slugs_match",
]
