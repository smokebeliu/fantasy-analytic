"""Command-line interface for applying database migrations."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import migration


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-migrate",
        description="Apply or inspect Fantasy Analytics database migrations.",
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for this command",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    upgrade = subcommands.add_parser("upgrade", help="Upgrade to a revision")
    upgrade.add_argument("revision", nargs="?", default="head")

    downgrade = subcommands.add_parser("downgrade", help="Downgrade to a revision")
    downgrade.add_argument("revision", nargs="?", default="base")

    subcommands.add_parser("current", help="Show the current revision")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "upgrade":
            migration.upgrade(args.database_url, args.revision)
        elif args.command == "downgrade":
            migration.downgrade(args.database_url, args.revision)
        elif args.command == "current":
            migration.current(args.database_url)
        else:  # pragma: no cover - argparse enforces valid commands
            parser.error(f"Unknown command: {args.command}")
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Migration command failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
