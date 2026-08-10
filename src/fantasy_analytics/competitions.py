"""The catalogue of fantasy competitions Sports.ru offers (step 22).

Until now the pipeline was addressed at exactly one league: every entry point
defaulted to the ``russia`` tournament slug and the admin API hard-coded it into
its paths. The data model was always competition-aware (``competitions`` +
``seasons.competition_id``), so what was missing was the *catalogue*: a list of
which leagues exist, which seasons each one exposes and therefore what an
operator is allowed to import.

``fantasyQueries.tournamentsList`` is the only operation that answers this — the
single-tournament query has to be addressed by a slug that is already known. The
catalogue is fetched once, stored on the ``competitions`` rows and then read from
PostgreSQL, so no read path ever calls Sports.ru.

Two shapes of the same season list need distinguishing:

* ``seasons`` — every season the league exposes, whether imported or not. It
  drives the admin season picker and lets the UI tell that, say, Serie A has no
  active season this year, so "refresh the active season" would fail.
* the imported ``seasons`` rows — what the read API serves. Those only appear
  once an import has actually run.

A season's stat name is not unique inside a competition: the Champions League and
Europa League each publish *two* fantasy seasons per year (the league phase and
the knockout stage), both named e.g. ``2025/2026`` and both pointing at the same
stat season. :func:`season_labels` disambiguates them by appending the fantasy
season id, which is the only identifier that differs.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .client import SportsGraphQLClient
from .db import session_scope
from .db.import_repository import DomainImportRepository
from .db.models import Competition
from .queries import TOURNAMENTS_QUERY

# The tournament the project started with; still the default for every CLI and
# the target of the legacy ``/admin/ingestion/rpl/*`` paths.
DEFAULT_TOURNAMENT_SLUG = "russia"


class CompetitionCatalogueError(RuntimeError):
    """Raised when the tournament catalogue cannot be read or resolved."""


@dataclass(frozen=True)
class SeasonRef:
    """One season a competition exposes, imported or not."""

    fantasy_season_id: str
    stat_season_id: str | None
    name: str
    label: str
    is_active: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "fantasy_season_id": self.fantasy_season_id,
            "stat_season_id": self.stat_season_id,
            "name": self.name,
            "label": self.label,
            "is_active": self.is_active,
        }


@dataclass(frozen=True)
class CompetitionRef:
    """One fantasy competition (league or tournament) and its season list."""

    fantasy_tournament_id: str
    slug: str
    name: str
    sort_order: int
    seasons: tuple[SeasonRef, ...]
    current_season_id: str | None

    @property
    def has_active_season(self) -> bool:
        return any(season.is_active for season in self.seasons)

    @property
    def latest_season(self) -> SeasonRef | None:
        """The season a default import would target.

        Mirrors :func:`fantasy_analytics.discovery._select_season`: the newest
        *completed* season, because a season in progress has partial statistics.
        Falls back to the newest season of any kind for a league that has never
        finished one.
        """
        completed = [season for season in self.seasons if not season.is_active]
        pool = completed or list(self.seasons)
        return pool[-1] if pool else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fantasy_tournament_id": self.fantasy_tournament_id,
            "slug": self.slug,
            "name": self.name,
            "sort_order": self.sort_order,
            "current_season_id": self.current_season_id,
            "seasons": [season.as_dict() for season in self.seasons],
        }


def season_labels(seasons: list[dict[str, Any]]) -> list[str]:
    """Return a display label per season, unique within the competition.

    Season names repeat inside a competition whenever a tournament is split into
    phases (Champions/Europa League), so a bare ``2025/2026`` would name two
    different things in a picker. Only the ambiguous ones get the fantasy season
    id appended, keeping the common case clean.
    """
    names = [str((season.get("statObject") or {}).get("name") or "") for season in seasons]
    duplicates = {name for name, count in Counter(names).items() if count > 1}
    labels: list[str] = []
    for season, name in zip(seasons, names, strict=True):
        fantasy_id = str(season.get("id") or "")
        display = name or f"сезон {fantasy_id}"
        labels.append(f"{display} (#{fantasy_id})" if name in duplicates else display)
    return labels


def _season_sort_key(season: dict[str, Any]) -> tuple[str, int]:
    """Order seasons oldest-first by stat name, then by fantasy id.

    The stat season name (``2025/2026``, ``2026``) sorts chronologically as a
    string, and the fantasy id breaks ties between phases of one tournament in
    the order Sports.ru created them (league phase before knockout).
    """
    name = str((season.get("statObject") or {}).get("name") or "")
    try:
        fantasy_id = int(str(season.get("id") or 0))
    except ValueError:
        fantasy_id = 0
    return (name, fantasy_id)


def catalogue_seasons(tournament: dict[str, Any]) -> list[SeasonRef]:
    """Normalize a tournament payload's ``seasons`` list, oldest season first.

    Both the catalogue sync and a season import see this same block (the single
    tournament query returns it too), so an import keeps its own league's
    catalogue current without a separate sync.
    """
    seasons = [
        season
        for season in (tournament.get("seasons") or [])
        if isinstance(season, dict) and season.get("id")
    ]
    seasons.sort(key=_season_sort_key)
    return [
        SeasonRef(
            fantasy_season_id=str(season["id"]),
            stat_season_id=(
                str((season.get("statObject") or {}).get("id"))
                if (season.get("statObject") or {}).get("id")
                else None
            ),
            name=str((season.get("statObject") or {}).get("name") or ""),
            label=label,
            is_active=bool(season.get("isActive")),
        )
        for season, label in zip(seasons, season_labels(seasons), strict=True)
    ]


def normalize_catalogue(payload: dict[str, Any]) -> list[CompetitionRef]:
    """Turn a ``tournamentsList`` response into ordered competition refs."""
    tournaments = (
        (payload.get("data") or {}).get("fantasyQueries") or {}
    ).get("tournamentsList")
    if not isinstance(tournaments, list) or not tournaments:
        raise CompetitionCatalogueError(
            "Sports.ru returned no fantasy tournaments"
        )

    refs: list[CompetitionRef] = []
    for sort_order, tournament in enumerate(tournaments):
        if not isinstance(tournament, dict):
            continue
        fantasy_tournament_id = str(tournament.get("id") or "")
        slug = str(tournament.get("webName") or fantasy_tournament_id)
        if not fantasy_tournament_id or not slug:
            continue
        refs.append(
            CompetitionRef(
                fantasy_tournament_id=fantasy_tournament_id,
                slug=slug,
                name=str(tournament.get("name") or slug),
                sort_order=sort_order,
                seasons=tuple(catalogue_seasons(tournament)),
                current_season_id=(
                    str((tournament.get("currentSeason") or {}).get("id"))
                    if (tournament.get("currentSeason") or {}).get("id")
                    else None
                ),
            )
        )
    if not refs:
        raise CompetitionCatalogueError(
            "Sports.ru returned no usable fantasy tournaments"
        )
    return refs


def fetch_catalogue(client: SportsGraphQLClient) -> list[CompetitionRef]:
    """Download the full list of fantasy competitions from Sports.ru."""
    return normalize_catalogue(client.execute(TOURNAMENTS_QUERY))


def persist_catalogue(
    session: Session,
    refs: list[CompetitionRef],
    *,
    synced_at: datetime | None = None,
) -> dict[str, Any]:
    """Upsert the catalogue onto the ``competitions`` rows.

    Only catalogue metadata is written: the seasons an import actually produced
    live in ``seasons`` and are never touched here, so syncing the catalogue can
    never disturb a published snapshot.
    """
    repo = DomainImportRepository(session)
    stamp = synced_at or datetime.now(UTC)
    for ref in refs:
        repo.upsert_competition(
            fantasy_tournament_id=ref.fantasy_tournament_id,
            slug=ref.slug,
            name=ref.name,
            sort_order=ref.sort_order,
            available_seasons=[season.as_dict() for season in ref.seasons],
            catalogue_synced_at=stamp,
        )
    return {
        "synced_at": stamp.isoformat(),
        "competitions": len(refs),
        "seasons": sum(len(ref.seasons) for ref in refs),
        "slugs": [ref.slug for ref in refs],
    }


def sync_catalogue(
    client: SportsGraphQLClient,
    session_factory: sessionmaker,
    *,
    synced_at: datetime | None = None,
) -> dict[str, Any]:
    """Fetch the catalogue from Sports.ru and store it, returning a report."""
    refs = fetch_catalogue(client)
    with session_scope(session_factory) as session:
        return persist_catalogue(session, refs, synced_at=synced_at)


def known_slugs(session: Session) -> list[str]:
    """Every competition slug the stored catalogue knows about."""
    return list(
        session.execute(
            select(Competition.slug).order_by(
                Competition.sort_order, Competition.slug
            )
        ).scalars()
    )


def resolve_slug(session: Session, slug: str) -> Competition | None:
    """Look a competition up by its slug (the ``webName`` Sports.ru exposes)."""
    return session.execute(
        select(Competition).where(Competition.slug == slug)
    ).scalar_one_or_none()


__all__ = [
    "CompetitionCatalogueError",
    "CompetitionRef",
    "DEFAULT_TOURNAMENT_SLUG",
    "SeasonRef",
    "catalogue_seasons",
    "fetch_catalogue",
    "known_slugs",
    "normalize_catalogue",
    "persist_catalogue",
    "resolve_slug",
    "season_labels",
    "sync_catalogue",
]
