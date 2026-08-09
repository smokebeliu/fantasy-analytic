"""Command-line entry point for walk-forward backtesting (step 19).

One command replays a finished season from an imported snapshot, compares the
event model against its two baselines on both accuracy and realised squad points
and prints a decision. The full result plus the parameters it was produced with
are written to disk so a run is reproducible and reviewable:

``backtest.json``     the complete report (params, audit, per-tour metrics, squads)
``tour-metrics.csv``  one row per (tour, model) for spreadsheets/plots
``report.md``         the human-readable summary: metrics, errors, verdict

The command exits non-zero when the leakage audit finds a violation, so a broken
cutoff can never be mistaken for a good backtest.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .backtest import (
    ACCEPT_MODEL,
    DEFAULT_MODELS,
    KEEP_BASELINE,
    PRIMARY_MODEL,
    REVISE_MODEL,
    BacktestError,
    run_backtest,
)
from .db import create_db_engine, create_session_factory
from .optimizer import ROLES

_DECISION_TEXT = {
    ACCEPT_MODEL: "принять модель",
    REVISE_MODEL: "доработать модель",
    KEEP_BASELINE: "оставить baseline",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-backtest",
        description=(
            "Backtest the points forecast and the squad optimizer on the tours "
            "of an imported season and compare them against the baselines."
        ),
    )
    parser.add_argument("--database-url", help="Override DATABASE_URL for this command")
    parser.add_argument(
        "--run-id",
        type=int,
        help="Ingestion run to backtest (default: the active snapshot)",
    )
    parser.add_argument(
        "--season",
        dest="season_ref",
        help="Season fantasy id, stat id or name to select the active snapshot",
    )
    parser.add_argument(
        "--tour",
        dest="tours",
        action="append",
        help=(
            "Restrict the backtest to this tour (fantasy id or name); repeatable. "
            "Default: every tour with imported player statistics."
        ),
    )
    parser.add_argument(
        "--model",
        dest="models",
        action="append",
        choices=list(DEFAULT_MODELS),
        help=f"Model to evaluate; repeatable (default: {', '.join(DEFAULT_MODELS)})",
    )
    parser.add_argument(
        "--no-optimize",
        action="store_true",
        help="Only measure forecast accuracy; skip the squad simulation",
    )
    parser.add_argument(
        "--fixture-conflict-weight",
        type=float,
        help="Head-to-head penalty used while simulating squads (default: 0.25)",
    )
    parser.add_argument(
        "--top-errors",
        type=int,
        default=20,
        help="How many largest over/under-predictions to report (default: 20)",
    )
    parser.add_argument(
        "--top-unstable",
        type=int,
        default=10,
        help="How many least stable features to report (default: 10)",
    )
    parser.add_argument(
        "--output",
        default="data/backtest",
        help="Directory for the backtest artifacts (default: data/backtest)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable summary on stderr",
    )
    return parser


def _metric_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten per-(tour, model) metrics and squad results for the CSV export."""
    rows: list[dict[str, Any]] = []
    for tour in report["tours"]:
        for model, entry in tour["models"].items():
            metrics = entry["metrics"]
            played = entry.get("metrics_played") or {}
            squad = entry.get("squad") or {}
            row: dict[str, Any] = {
                "fantasy_tour_id": tour["fantasy_tour_id"],
                "tour": tour["name"],
                "cutoff": tour["cutoff"],
                "model": model,
                "players": metrics["n"],
                "mae": metrics["mae"],
                "rmse": metrics["rmse"],
                "bias": metrics["bias"],
                "mean_predicted": metrics["mean_predicted"],
                "mean_actual": metrics["mean_actual"],
                "players_played": played.get("n"),
                "mae_played": played.get("mae"),
                "rmse_played": played.get("rmse"),
                "bias_played": played.get("bias"),
                "squad_projected_points": squad.get("projected_points"),
                "squad_actual_points": squad.get("actual_points"),
                "squad_best_eleven_points": squad.get("best_eleven_actual_points"),
                "squad_bench_points": squad.get("bench_actual_points"),
                "captain_actual_points": (squad.get("captain") or {}).get(
                    "actual_points"
                ),
                "captain_was_best": (squad.get("captain") or {}).get(
                    "was_best_starter"
                ),
                "hindsight_actual_points": (tour.get("hindsight") or {}).get(
                    "actual_points"
                ),
            }
            for role in ROLES:
                role_metrics = entry["by_role"].get(role) or {}
                row[f"mae_{role.lower()}"] = role_metrics.get("mae")
            rows.append(row)
    return rows


def _markdown(report: dict[str, Any]) -> str:
    """Render the reviewable summary of a backtest run."""
    season = report["season"]
    lines: list[str] = [
        f"# Backtest {season['name']} (run {report['run_id']})",
        "",
        f"- Backtest version: `{report['versions']['backtest']}`",
        f"- Model `{PRIMARY_MODEL}` v{report['versions']['model']}, "
        f"features v{report['versions']['feature']}, "
        f"scoring `{report['versions']['scoring']}`, "
        f"optimizer v{report['versions']['optimizer']}",
        f"- Tours evaluated: {report['counts']['tours_evaluated']} of "
        f"{report['counts']['tours_total']} "
        f"(skipped {report['counts']['tours_skipped']})",
        f"- Cutoff audit: "
        f"{'passed' if report['cutoff_audit']['passed'] else 'FAILED'} "
        f"({report['cutoff_audit']['rows_checked']} rows re-derived from the raw "
        f"appearance table, "
        f"{len(report['cutoff_audit']['warnings'])} tour(s) with warnings)",
        "",
        "## Model comparison",
        "",
        "`MAE*`/`RMSE*` are restricted to players who actually took the field; the "
        "unstarred columns cover every selectable player, where a correct zero for "
        "a player who never appeared dominates the average.",
        "",
        "| Model | Rows | MAE | RMSE | Bias | MAE* | RMSE* | Squad points "
        "| Captain hits | Lineup eff. |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for model, summary in report["models"].items():
        metrics = summary["metrics"]
        played = summary.get("metrics_played") or {}
        squad = summary.get("squad") or {}
        lines.append(
            f"| `{model}` | {metrics['n']} | {metrics['mae']} | {metrics['rmse']} "
            f"| {metrics['bias']} | {played.get('mae', '—')} "
            f"| {played.get('rmse', '—')} | {squad.get('actual_points_total', '—')} "
            f"| {squad.get('captain_hit_rate', '—')} "
            f"| {squad.get('lineup_efficiency_mean', '—')} |"
        )

    hindsight = (report.get("hindsight") or {}).get("actual_points_total")
    if hindsight is not None:
        lines += ["", f"Hindsight optimum over the same tours: **{hindsight}** points."]

    lines += ["", "## Accuracy by position", "", "| Model | " + " | ".join(ROLES) + " |",
              "| --- | " + " | ".join("---" for _ in ROLES) + " |"]
    for model, summary in report["models"].items():
        cells = [
            str((summary["by_role"].get(role) or {}).get("mae", "—")) for role in ROLES
        ]
        lines.append(f"| `{model}` | " + " | ".join(cells) + " |")

    lines += ["", "## Per-tour MAE", "", "| Tour | " + " | ".join(
        f"`{model}`" for model in report["models"]
    ) + " |", "| --- | " + " | ".join("---" for _ in report["models"]) + " |"]
    by_tour = {
        model: {entry["fantasy_tour_id"]: entry["metrics"] for entry in summary["by_tour"]}
        for model, summary in report["models"].items()
    }
    for tour in report["tours"]:
        cells = [
            str(by_tour[model].get(tour["fantasy_tour_id"], {}).get("mae", "—"))
            for model in report["models"]
        ]
        lines.append(f"| {tour['name']} | " + " | ".join(cells) + " |")

    lines += ["", f"## Largest errors (`{PRIMARY_MODEL}`)", "",
              "Over-predicted:", ""]
    for item in report["errors"]["over_predicted"][:10]:
        lines.append(
            f"- {item['player_name']} ({item['role']}, {item['club_name']}), "
            f"{item['tour']}: прогноз {item['predicted']} против {item['actual']} "
            f"(ошибка {item['error']})"
        )
    lines += ["", "Under-predicted:", ""]
    for item in report["errors"]["under_predicted"][:10]:
        lines.append(
            f"- {item['player_name']} ({item['role']}, {item['club_name']}), "
            f"{item['tour']}: прогноз {item['predicted']} против {item['actual']} "
            f"(ошибка {item['error']})"
        )

    lines += ["", "## Least stable features", "",
              "| Feature | Mean | Std | Mean |Δ| tour to tour | Volatility |",
              "| --- | --- | --- | --- | --- |"]
    for item in report["feature_instability"]:
        lines.append(
            f"| `{item['feature']}` | {item['mean']} | {item['std']} "
            f"| {item['mean_abs_change']} | {item['volatility']} |"
        )

    verdict = report["verdict"]
    lines += [
        "",
        "## Verdict",
        "",
        f"**{verdict['decision']}** ({_DECISION_TEXT.get(verdict['decision'], '')})",
        "",
        verdict["reason"] + ".",
        "",
    ]
    if verdict.get("criteria"):
        lines += [
            "| Criterion | Better | " + f"`{PRIMARY_MODEL}`" + " | Best baseline | Won |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in verdict["criteria"]:
            lines.append(
                f"| {item['label']} | {item['better']} | {item['primary']} "
                f"| {item['best_baseline']} (`{item['best_baseline_model']}`) "
                f"| {'yes' if item['won'] else 'no'} |"
            )
        lines.append("")
    return "\n".join(lines)


def _write_outputs(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset_path = output_dir / "backtest.json"
    dataset_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    csv_path = output_dir / "tour-metrics.csv"
    rows = _metric_rows(report)
    if rows:
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")

    report_path = output_dir / "report.md"
    report_path.write_text(_markdown(report), encoding="utf-8")

    return {
        "dataset": str(dataset_path),
        "csv": str(csv_path),
        "report": str(report_path),
    }


def _summarize(report: dict[str, Any], paths: dict[str, str], stream) -> None:
    season = report["season"]
    counts = report["counts"]
    print(
        f"Backtest v{report['backtest_version']} of season {season['name']} "
        f"from run {report['run_id']}: {counts['tours_evaluated']} tours, "
        f"{counts['predictions']} predictions per model",
        file=stream,
    )
    audit = report["cutoff_audit"]
    print(
        f"  cutoff audit: {'passed' if audit['passed'] else 'FAILED'} "
        f"({audit['rows_checked']} rows re-derived, "
        f"{len(audit['violations'])} tour(s) with violations, "
        f"{len(audit['warnings'])} with warnings)",
        file=stream,
    )
    for model, summary in report["models"].items():
        metrics = summary["metrics"]
        played = summary.get("metrics_played") or {}
        squad = summary.get("squad") or {}
        squad_note = (
            f", squad {squad['actual_points_total']} pts "
            f"(captain hit {squad['captain_hit_rate']}, "
            f"lineup eff {squad['lineup_efficiency_mean']})"
            if squad
            else ""
        )
        print(
            f"  {model:<14} MAE={metrics['mae']} RMSE={metrics['rmse']} "
            f"bias={metrics['bias']} "
            f"(played only: MAE={played.get('mae')} n={played.get('n')})"
            f"{squad_note}",
            file=stream,
        )
    hindsight = (report.get("hindsight") or {}).get("actual_points_total")
    if hindsight is not None:
        print(f"  hindsight optimum: {hindsight} pts", file=stream)
    verdict = report["verdict"]
    print(f"  verdict: {verdict['decision']} — {verdict['reason']}", file=stream)
    print(f"  report written to {paths['report']}", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)

    try:
        report = run_backtest(
            session_factory,
            run_id=args.run_id,
            season_ref=args.season_ref,
            tour_refs=args.tours,
            models=tuple(args.models) if args.models else DEFAULT_MODELS,
            optimize=not args.no_optimize,
            fixture_conflict_weight=args.fixture_conflict_weight,
            top_errors=args.top_errors,
            top_unstable=args.top_unstable,
        )
    except BacktestError as error:
        print(f"Backtest failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Backtest error: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    paths = _write_outputs(report, Path(args.output))

    if not args.quiet:
        _summarize(report, paths, sys.stderr)

    summary = {
        key: value
        for key, value in report.items()
        if key not in ("tours", "errors", "cutoff_audit")
    }
    summary["cutoff_audit"] = {
        key: value
        for key, value in report["cutoff_audit"].items()
        if key != "tours"
    }
    summary["artifacts"] = paths
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # A leakage violation invalidates the whole comparison, so it fails the run.
    return 0 if report["cutoff_audit"]["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
