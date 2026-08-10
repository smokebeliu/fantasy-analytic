"""Command-line entry point for the competition catalogue (step 22).

``sync`` refreshes the stored catalogue from Sports.ru (one GraphQL call) so the
admin API and UI know which leagues and seasons exist. ``list`` prints the stored
catalogue, and is the quickest way to look up the ``--tournament`` slug that
``fantasy-ingest`` expects.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .client import ClientConfig, DEFAULT_ENDPOINT, SportsGraphQLClient
from .competitions import sync_catalogue
from .db import create_db_engine, create_session_factory, session_scope
from .read_repository import ReadRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-competitions",
        description="Inspect and refresh the catalogue of fantasy leagues.",
    )
    parser.add_argument(
        "--database-url", help="Override DATABASE_URL for this command"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync = subparsers.add_parser(
        "sync", help="Fetch the league catalogue from Sports.ru and store it"
    )
    sync.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"GraphQL endpoint (default: {DEFAULT_ENDPOINT})",
    )
    sync.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )

    listing = subparsers.add_parser(
        "list", help="Print the stored catalogue (never calls Sports.ru)"
    )
    listing.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit JSON instead of a table",
    )
    listing.add_argument(
        "--imported-only",
        action="store_true",
        help="Only leagues that already have an imported season",
    )
    return parser


def _print_table(competitions: list[dict]) -> None:
    header = f"{'slug':<22} {'name':<26} {'seasons':>7} {'imported':>8}  latest snapshot"
    print(header)
    print("-" * len(header))
    for item in competitions:
        latest = item.get("latest_season") or {}
        snapshot = (latest.get("snapshot") or {}) if latest else {}
        published = (
            f"{latest.get('label') or latest.get('name')} (run #{snapshot['run_id']})"
            if snapshot
            else "—"
        )
        print(
            f"{item['slug']:<22} {item['name']:<26} "
            f"{len(item.get('available_seasons') or []):>7} "
            f"{len(item.get('seasons') or []):>8}  {published}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)
    try:
        if args.command == "sync":
            if args.timeout <= 0:
                parser.error("--timeout must be greater than zero")
            client = SportsGraphQLClient(
                ClientConfig(endpoint=args.endpoint, timeout_seconds=args.timeout)
            )
            report = sync_catalogue(client, session_factory)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0

        with session_scope(session_factory) as session:
            competitions = ReadRepository(session).list_competitions(
                imported_only=args.imported_only
            )
        if args.as_json:
            print(json.dumps(competitions, ensure_ascii=False, indent=2))
        elif not competitions:
            print(
                "The catalogue is empty; run 'fantasy-competitions sync' first.",
                file=sys.stderr,
            )
            return 1
        else:
            _print_table(competitions)
        return 0
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Competition catalogue command failed: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
