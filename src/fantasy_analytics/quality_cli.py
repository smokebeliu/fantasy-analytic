"""Command-line entry point for the data-quality and reconciliation gate.

It evaluates the snapshot of an ingestion run (the latest successful run by
default), records violations in ``data_quality_issues`` and publishes the
snapshot only when no blocking issue is found. The process exits non-zero when
a blocking issue is present so it can gate a pipeline.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .db import create_db_engine, create_session_factory
from .quality import QualityError, run_quality_checks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-quality",
        description=(
            "Run data-quality and reconciliation checks against an imported "
            "snapshot and publish it only when no blocking issue is found."
        ),
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for this command",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        help="Ingestion run to evaluate (default: the latest successful run)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable summary on stderr",
    )
    return parser


def _summarize(report: dict, stream) -> None:
    verdict = "PASSED" if report["passed"] else "BLOCKED"
    print(
        f"Quality gate {verdict} for run {report['run_id']} "
        f"(season {report['season_id']}): "
        f"{report['counts']['blocking']} blocking, "
        f"{report['counts']['warnings']} warning(s); "
        f"active={report['is_active']}",
        file=stream,
    )
    for check in report["checks"]:
        print(
            f"  - {check['name']}: {check['status']} "
            f"({check['blocking']} blocking, {check['warnings']} warning)",
            file=stream,
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)

    try:
        report = run_quality_checks(session_factory, run_id=args.run_id)
    except QualityError as error:
        print(f"Quality gate failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Quality gate error: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    if not args.quiet:
        _summarize(report, sys.stderr)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
