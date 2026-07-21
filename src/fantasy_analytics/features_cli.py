"""Command-line entry point for the analytical feature dataset (step 6).

It reads the *active* snapshot published by the quality gate and builds a
reproducible, leakage-free feature dataset for a target tour. The full dataset
(rows plus metadata and the feature dictionary) is written to disk, while a
compact summary is printed to stdout so the command composes in a pipeline.

The database is never mutated and the Sports.ru API is never called.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .db import create_db_engine, create_session_factory
from .features import FeaturesError, build_feature_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-features",
        description=(
            "Build a reproducible, leakage-free analytical feature dataset for "
            "a target tour from the active snapshot."
        ),
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for this command",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        help="Ingestion run to build from (default: the active snapshot)",
    )
    parser.add_argument(
        "--season",
        dest="season_ref",
        help="Season fantasy id, stat id or name to select the active snapshot",
    )
    parser.add_argument(
        "--tour",
        dest="tour_ref",
        help=(
            "Target tour fantasy id or name (default: the next non-finished "
            "tour). Required when the season is fully finished."
        ),
    )
    parser.add_argument(
        "--output",
        default="data/features",
        help="Directory for the dataset artifacts (default: data/features)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable summary on stderr",
    )
    return parser


def _write_outputs(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = report["rows"]

    dataset_path = output_dir / "features.json"
    dataset_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    csv_path = output_dir / "features.csv"
    if rows:
        fieldnames = list(rows[0].keys())
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")

    dictionary_path = output_dir / "feature-dictionary.json"
    dictionary_path.write_text(
        json.dumps(report["feature_dictionary"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "dataset": str(dataset_path),
        "csv": str(csv_path),
        "dictionary": str(dictionary_path),
    }


def _summarize(report: dict[str, Any], paths: dict[str, str], stream) -> None:
    tour = report["tour"]
    counts = report["counts"]
    print(
        f"Features v{report['feature_version']} for season "
        f"{report['season']['name']} tour {tour['fantasy_tour_id']} "
        f"({tour['name']}, status={tour['status']}) from run {report['run_id']}",
        file=stream,
    )
    print(
        f"  cutoff={report['cutoff']}; rows={counts['rows']}, "
        f"fixtures={counts['fixtures']}, "
        f"clubs_with_history={counts['clubs_with_history']}, "
        f"players_without_fixture={counts['players_without_fixture']}",
        file=stream,
    )
    print(f"  dataset written to {paths['dataset']}", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)

    try:
        report = build_feature_dataset(
            session_factory,
            run_id=args.run_id,
            season_ref=args.season_ref,
            tour_ref=args.tour_ref,
        )
    except FeaturesError as error:
        print(f"Feature build failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Feature build error: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    paths = _write_outputs(report, Path(args.output))

    if not args.quiet:
        _summarize(report, paths, sys.stderr)

    # Print metadata only (without the potentially large row payload) so the
    # command stays pipeline-friendly; the full dataset lives in --output.
    summary = {key: value for key, value in report.items() if key != "rows"}
    summary["artifacts"] = paths
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
