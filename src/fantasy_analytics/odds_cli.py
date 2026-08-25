"""Command-line entry point for refreshing match betting odds.

Downloads the Sports.ru 1x2 calendar line for a league (or for every imported
active league whose next tour starts tomorrow), stores it, and rebuilds the
event forecast so expected points pick up attacking / clean-sheet upside.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Sequence

from .client import ClientConfig, DEFAULT_ENDPOINT, SportsGraphQLClient
from .competitions import DEFAULT_TOURNAMENT_SLUG
from .db import create_db_engine, create_session_factory
from .nightly_refresh import NightlyRefreshSettings
from .odds_refresh import (
    OddsRefreshError,
    refresh_due_odds,
    refresh_league_odds,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-odds",
        description=(
            "Fetch Sports.ru 1x2 odds for upcoming fixtures and rebuild the "
            "next-tour forecast from them."
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
        default=None,
        help=(
            "Fantasy tournament slug to refresh (default: every league whose "
            f"next tour starts tomorrow; omit with --force for all, or pass "
            f"{DEFAULT_TOURNAMENT_SLUG} for the RPL)"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the day-before-tour window (refresh even if kickoff is later)",
    )
    parser.add_argument(
        "--no-forecast",
        action="store_true",
        help="Store the line without rebuilding expected points",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)
    client = SportsGraphQLClient(ClientConfig(endpoint=args.endpoint))

    try:
        if args.tournament:
            report = refresh_league_odds(
                client,
                session_factory,
                tournament_slug=args.tournament,
                rebuild_forecasts=not args.no_forecast,
            )
        else:
            report = refresh_due_odds(
                client,
                session_factory,
                settings=NightlyRefreshSettings.from_env(),
                force=args.force,
                rebuild_forecasts=not args.no_forecast,
            )
    except OddsRefreshError as error:
        print(f"Odds refresh failed: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
