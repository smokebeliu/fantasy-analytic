"""Command-line entry point for the discovery prototype."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .client import ClientConfig, DEFAULT_ENDPOINT, SportsGraphQLClient
from .competitions import DEFAULT_TOURNAMENT_SLUG
from .discovery import DiscoveryOptions, run_discovery


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-discover",
        description=(
            "Fetch Sports.ru fantasy data for one league and create "
            "model-discovery artifacts."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/discovery"),
        help="Artifact directory (default: data/discovery)",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"GraphQL endpoint (default: {DEFAULT_ENDPOINT})",
    )
    parser.add_argument(
        "--tournament",
        default=DEFAULT_TOURNAMENT_SLUG,
        help=f"Fantasy tournament slug (default: {DEFAULT_TOURNAMENT_SLUG})",
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
        help="Discover the active season instead of the latest completed season",
    )
    parser.add_argument(
        "--player-page-size",
        type=_positive_integer,
        default=100,
        help="Players requested per GraphQL page (default: 100)",
    )
    parser.add_argument(
        "--history-samples-per-role",
        type=int,
        default=1,
        help="Top player histories sampled for each role; use 0 to disable",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds (default: 30)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.history_samples_per_role < 0:
        parser.error("--history-samples-per-role cannot be negative")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    client = SportsGraphQLClient(
        ClientConfig(
            endpoint=args.endpoint,
            timeout_seconds=args.timeout,
        )
    )
    options = DiscoveryOptions(
        output_dir=args.output,
        tournament_slug=args.tournament,
        season_id=args.season_id,
        season_name=args.season_name,
        use_current_season=args.current,
        player_page_size=args.player_page_size,
        history_samples_per_role=args.history_samples_per_role,
    )

    try:
        report = run_discovery(client, options)
    except Exception as error:
        print(f"Discovery failed: {error}", file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"\nArtifacts written to {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
