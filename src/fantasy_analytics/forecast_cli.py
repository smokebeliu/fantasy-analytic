"""Command-line entry point for the baseline points forecast (step 7).

It builds an interpretable, event-based expected-points forecast (plus a season
mean and recent-form baseline) for a target tour from the active snapshot, then
persists the rows to ``player_forecasts`` and writes the full dataset to disk. A
compact summary is printed to stdout so the command composes in a pipeline.

Use ``--no-persist`` to only compute and write artifacts without touching the
database.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .db import create_db_engine, create_session_factory
from .forecast import MODEL_EVENT, ForecastError, run_forecast


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-forecast",
        description=(
            "Forecast expected fantasy points for a target tour from the active "
            "snapshot and persist them to player_forecasts."
        ),
    )
    parser.add_argument(
        "--database-url",
        help="Override DATABASE_URL for this command",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        help="Ingestion run to forecast from (default: the active snapshot)",
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
        default="data/forecast",
        help="Directory for the forecast artifacts (default: data/forecast)",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        help="Do not write forecasts to the database; only produce artifacts",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable summary on stderr",
    )
    return parser


def _flatten(row: dict[str, Any]) -> dict[str, Any]:
    """Flatten nested JSON fields for the CSV export."""
    flat = {key: value for key, value in row.items() if key not in ("components", "params")}
    for key, value in (row.get("components") or {}).items():
        flat[f"component_{key}"] = value
    return flat


def _write_outputs(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = report["rows"]

    dataset_path = output_dir / "forecast.json"
    dataset_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    csv_path = output_dir / "forecast.csv"
    flat_rows = [_flatten(row) for row in rows]
    if flat_rows:
        fieldnames: list[str] = []
        for row in flat_rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(flat_rows)
    else:
        csv_path.write_text("", encoding="utf-8")

    return {"dataset": str(dataset_path), "csv": str(csv_path)}


def _summarize(report: dict[str, Any], paths: dict[str, str], stream) -> None:
    tour = report["tour"]
    counts = report["counts"]
    print(
        f"Forecast model v{report['model_version']} (scoring "
        f"{report['scoring_version']}) for season {report['season']['name']} "
        f"tour {tour['fantasy_tour_id']} ({tour['name']}, status={tour['status']}) "
        f"from run {report['run_id']}",
        file=stream,
    )
    print(
        f"  cutoff={report['cutoff']}; players={counts['players']}, "
        f"available={counts['available_players']}, rows={counts['rows']}, "
        f"persisted={report.get('persisted', 0)}",
        file=stream,
    )
    top = [
        row
        for row in report["rows"]
        if row["model_name"] == MODEL_EVENT
    ][:5]
    if top:
        print("  top expected points (event model):", file=stream)
        for row in top:
            print(
                f"    {row['expected_points']:>6} +/- {row['uncertainty']}  "
                f"{row['player_name']} ({row['role']}, {row['club_name']})",
                file=stream,
            )
    print(f"  dataset written to {paths['dataset']}", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)

    try:
        report = run_forecast(
            session_factory,
            run_id=args.run_id,
            season_ref=args.season_ref,
            tour_ref=args.tour_ref,
            persist=not args.no_persist,
        )
    except ForecastError as error:
        print(f"Forecast failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Forecast error: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    paths = _write_outputs(report, Path(args.output))

    if not args.quiet:
        _summarize(report, paths, sys.stderr)

    summary = {key: value for key, value in report.items() if key != "rows"}
    summary["artifacts"] = paths
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
