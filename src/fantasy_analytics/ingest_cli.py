"""Command-line entry point for the full historical import."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .client import ClientConfig, DEFAULT_ENDPOINT, SportsGraphQLClient
from .competitions import DEFAULT_TOURNAMENT_SLUG
from .db import create_db_engine, create_session_factory
from .ingestion import IngestionOptions, run_ingestion


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-ingest",
        description=(
            "Import a full Sports.ru fantasy season into PostgreSQL "
            "(season, tours, matches, clubs and every player). Pick the league "
            "with --tournament; 'fantasy-competitions list' shows the slugs."
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
        "--tournament",
        default=DEFAULT_TOURNAMENT_SLUG,
        help=(
            "Fantasy tournament slug, e.g. russia, spain, italy, england, "
            f"champions-league (default: {DEFAULT_TOURNAMENT_SLUG})"
        ),
    )
    season_group = parser.add_mutually_exclusive_group()
    season_group.add_argument("--season-id", help="Exact fantasy season ID")
    season_group.add_argument(
        "--season-name",
        help="Exact stat season name, for example 2025/2026",
    )
    season_group.add_argument(
        "--current",
        action="store_true",
        help="Import the active season instead of the latest completed season",
    )
    parser.add_argument(
        "--player-page-size",
        type=_positive_integer,
        default=100,
        help="Players requested per GraphQL page (default: 100)",
    )
    parser.add_argument(
        "--history-page-size",
        type=_positive_integer,
        default=100,
        help="Match-history rows requested per page (default: 100)",
    )
    parser.add_argument(
        "--history-workers",
        type=_positive_integer,
        default=8,
        help="Concurrent player-history requests (default: 8)",
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
    options = IngestionOptions(
        tournament_slug=args.tournament,
        season_id=args.season_id,
        season_name=args.season_name,
        use_current_season=args.current,
        player_page_size=args.player_page_size,
        history_page_size=args.history_page_size,
        history_workers=args.history_workers,
    )

    def on_progress(message: str) -> None:
        if not args.quiet:
            print(message, file=sys.stderr)

    try:
        report = run_ingestion(client, session_factory, options, on_progress)
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Ingestion failed: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
