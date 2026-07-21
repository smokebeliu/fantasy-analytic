"""Command-line entry point for the extended match-statistics spike.

It reads the match catalog imported by ``fantasy-ingest`` from PostgreSQL,
samples a reproducible subset of finished matches and probes the Sports.ru
``statMatch`` API to measure field coverage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, aliased

from .client import ClientConfig, DEFAULT_ENDPOINT, SportsGraphQLClient
from .db import create_db_engine, create_session_factory
from .db.models import Club, FantasyTour, Match, Season
from .match_stats import (
    MatchRef,
    MatchStatsOptions,
    run_match_stats_discovery,
)


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def load_matches(session: Session, season_name: str | None) -> list[MatchRef]:
    """Load finished matches with club and tour context from the catalog."""
    home_club = aliased(Club)
    away_club = aliased(Club)
    query = (
        select(
            Match.stat_match_id,
            Match.scheduled_at,
            Match.home_score,
            Match.away_score,
            FantasyTour.name.label("tour_name"),
            FantasyTour.fantasy_tour_id.label("tour_id"),
            home_club.canonical_name.label("home_club"),
            away_club.canonical_name.label("away_club"),
        )
        .join(home_club, Match.home_club_id == home_club.id)
        .join(away_club, Match.away_club_id == away_club.id)
        .join(Season, Match.season_id == Season.id)
        .outerjoin(FantasyTour, Match.tour_id == FantasyTour.id)
        .order_by(Match.scheduled_at)
    )
    if season_name:
        query = query.where(Season.name == season_name)

    matches: list[MatchRef] = []
    for row in session.execute(query):
        try:
            tour_order = int(row.tour_id) if row.tour_id is not None else 0
        except (TypeError, ValueError):
            tour_order = 0
        matches.append(
            MatchRef(
                stat_match_id=row.stat_match_id,
                tour_name=row.tour_name or "",
                tour_order=tour_order,
                home_club=row.home_club,
                away_club=row.away_club,
                scheduled_at=row.scheduled_at,
                home_score=row.home_score,
                away_score=row.away_score,
            )
        )
    return matches


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-match-stats",
        description=(
            "Sample finished RPL matches from the catalog and measure the "
            "field coverage of the Sports.ru extended match statistics."
        ),
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for this command",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"GraphQL endpoint (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--season-name",
        help="Restrict sampling to a stat season name, for example 2025/2026",
    )
    parser.add_argument(
        "--sample-size",
        type=_positive_integer,
        default=40,
        help="Matches to sample (minimum 30 is enforced; default: 40)",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Optional statSourceList value; the default resolver is used when omitted",
    )
    parser.add_argument(
        "--output",
        default="data/match-stats",
        help="Directory for raw fixtures and the coverage report",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress incremental progress output",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    client = SportsGraphQLClient(
        ClientConfig(endpoint=args.endpoint, timeout_seconds=args.timeout)
    )
    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)

    try:
        with session_factory() as session:
            matches = load_matches(session, args.season_name)
    finally:
        engine.dispose()

    if not matches:
        print(
            "No matches found in the catalog. Run 'fantasy-ingest' first "
            "(the cloud database starts empty on every new agent).",
            file=sys.stderr,
        )
        return 1

    options = MatchStatsOptions(
        output_dir=Path(args.output),
        sample_size=args.sample_size,
        source=args.source,
    )

    def on_progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    try:
        report = run_match_stats_discovery(client, matches, options, on_progress)
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Match-stats discovery failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
