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
        dest="run_ids",
        type=int,
        action="append",
        help=(
            "Ingestion run to backtest; repeatable. With several runs each one "
            "is written to its own sub-directory and a cross-league summary is "
            "added (default: the active snapshot)"
        ),
    )
    parser.add_argument(
        "--all-active",
        action="store_true",
        help="Backtest every published snapshot that has at least one played tour",
    )
    parser.add_argument(
        "--season",
        dest="season_ref",
        help="Season fantasy id, stat id or name to select the active snapshot",
    )
    parser.add_argument(
        "--carry-squad",
        action="store_true",
        help=(
            "Keep one squad from tour to tour and spend only the tour's transfer "
            "allowance, as in the game, instead of building a fresh squad per tour"
        ),
    )
    parser.add_argument(
        "--max-transfers",
        type=int,
        help="Transfers allowed per tour in --carry-squad mode (default: the tour's limit)",
    )
    parser.add_argument(
        "--min-transfer-gain",
        type=float,
        help="Least expected-points gain a transfer must bring (default: 0.5)",
    )
    parser.add_argument(
        "--transfer-gain-sigma",
        type=float,
        help="Extra transfer margin in standard deviations of both forecasts (default: 0)",
    )
    parser.add_argument(
        "--captain-risk-weight",
        type=float,
        help="Standard deviations added to a player's captain score (default: 0)",
    )
    parser.add_argument(
        "--horizon-tours",
        type=int,
        default=1,
        help="Tours the roster is chosen on in --carry-squad mode (default: 1)",
    )
    parser.add_argument(
        "--horizon-decay",
        type=float,
        help="Discount per tour ahead for --horizon-tours (default: 0.7)",
    )
    parser.add_argument(
        "--no-parallel",
        action="store_true",
        help=(
            "Forecast a European cup from its own matches only, without the "
            "national leagues its clubs play in (step 24) — the baseline the "
            "parallel sourcing is measured against"
        ),
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

    lines += [
        "",
        "## Ranking and the squad's own accounting",
        "",
        "`Rank ρ*` is the Spearman correlation between forecast and fact among "
        f"players who played. `Top-{report.get('top_n', 25)}` compares the tour's "
        "highest forecasts with what they scored (and with the real top scorers, "
        "the ceiling). `Squad proj/act` is the optimizer's own projection against "
        "the points its squads realised; `auto-subs` applies the game's automatic "
        "substitutions and vice-captain rule.",
        "",
        "| Model | Rank ρ* | Top-N pred | Top-N act | Top-N ceiling | Top-N hits "
        "| Squad proj | Squad act | Gap | Squad act (auto-subs) | Transfers |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for model, summary in report["models"].items():
        ranking = summary.get("ranking") or {}
        top = ranking.get("top") or {}
        squad = summary.get("squad") or {}
        gap = squad.get("projection_gap_share")
        lines.append(
            f"| `{model}` | {ranking.get('rank_corr_played', '—')} "
            f"| {top.get('predicted_points', '—')} | {top.get('actual_points', '—')} "
            f"| {top.get('ceiling_points', '—')} | {top.get('hit_rate', '—')} "
            f"| {squad.get('projected_points_total', '—')} "
            f"| {squad.get('actual_points_total', '—')} "
            f"| {'—' if gap is None else f'{gap:+.1%}'} "
            f"| {squad.get('actual_points_autosub_total', '—')} "
            f"| {squad.get('transfers_made_total', '—')} |"
        )
    params = report.get("params") or {}
    if params.get("carry_squad"):
        lines += [
            "",
            "Squads were carried over from tour to tour "
            f"(transfers per tour: {params.get('max_transfers') or 'the tour limit'}, "
            f"minimum gain {params.get('min_transfer_gain')}, "
            f"sigma {params.get('transfer_gain_sigma')}, "
            f"horizon {params.get('horizon_tours')} tour(s), "
            f"decay {params.get('horizon_decay')}).",
        ]

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
        ranking = summary.get("ranking") or {}
        top = ranking.get("top") or {}
        squad = summary.get("squad") or {}
        squad_note = (
            f", squad {squad['actual_points_total']} pts "
            f"(auto-subs {squad['actual_points_autosub_total']}, "
            f"proj {squad['projected_points_total']}, "
            f"captain hit {squad['captain_hit_rate']}, "
            f"lineup eff {squad['lineup_efficiency_mean']}, "
            f"transfers {squad['transfers_made_total']})"
            if squad
            else ""
        )
        print(
            f"  {model:<14} MAE={metrics['mae']} RMSE={metrics['rmse']} "
            f"bias={metrics['bias']} "
            f"(played only: MAE={played.get('mae')} n={played.get('n')}) "
            f"rank={ranking.get('rank_corr_played')} "
            f"top{top.get('n')}={top.get('predicted_points')}/{top.get('actual_points')}"
            f"/{top.get('ceiling_points')}"
            f"{squad_note}",
            file=stream,
        )
    hindsight = (report.get("hindsight") or {}).get("actual_points_total")
    if hindsight is not None:
        print(f"  hindsight optimum: {hindsight} pts", file=stream)
    verdict = report["verdict"]
    print(f"  verdict: {verdict['decision']} — {verdict['reason']}", file=stream)
    print(f"  report written to {paths['report']}", file=stream)


def _league_label(report: dict[str, Any]) -> str:
    season = report.get("season") or {}
    competition = report.get("competition") or {}
    name = competition.get("name") or competition.get("slug") or ""
    return f"{name} {season.get('name') or ''}".strip() or f"run {report['run_id']}"


def _summary_markdown(reports: Sequence[dict[str, Any]]) -> str:
    """One table across leagues, so a change is judged on every season at once."""
    lines = [
        "# Backtest summary",
        "",
        f"{len(reports)} run(s); the three verdict criteria per league plus the "
        "ranking metrics. A change is only worth keeping if it does not lose on "
        "MAE, MAE* or squad points in *any* league and narrows the top-N gap.",
        "",
        "| League | Run | Tours | Model | MAE | MAE* | Rank ρ* | Top-N pred/act/ceiling "
        "| Squad pts | Auto-subs | Hindsight | Proj gap | Verdict |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for report in reports:
        hindsight = (report.get("hindsight") or {}).get("actual_points_total")
        verdict = (report.get("verdict") or {}).get("decision", "")
        for model, summary in report["models"].items():
            metrics = summary["metrics"]
            played = summary.get("metrics_played") or {}
            ranking = summary.get("ranking") or {}
            top = ranking.get("top") or {}
            squad = summary.get("squad") or {}
            gap = squad.get("projection_gap_share")
            lines.append(
                f"| {_league_label(report)} | {report['run_id']} "
                f"| {report['counts']['tours_evaluated']} | `{model}` "
                f"| {metrics['mae']} | {played.get('mae', '—')} "
                f"| {ranking.get('rank_corr_played', '—')} "
                f"| {top.get('predicted_points', '—')} / {top.get('actual_points', '—')} "
                f"/ {top.get('ceiling_points', '—')} "
                f"| {squad.get('actual_points_total', '—')} "
                f"| {squad.get('actual_points_autosub_total', '—')} "
                f"| {hindsight if hindsight is not None else '—'} "
                f"| {'—' if gap is None else f'{gap:+.1%}'} "
                f"| {verdict if model == PRIMARY_MODEL else ''} |"
            )
    return "\n".join(lines) + "\n"


def _active_run_ids(session_factory) -> list[int]:
    """Every published snapshot with at least one tour of player statistics."""
    from sqlalchemy import select

    from .db import session_scope
    from .db.models import IngestionRun, PlayerMatchStats

    with session_scope(session_factory) as session:
        runs = list(
            session.execute(
                select(IngestionRun.id)
                .where(IngestionRun.is_active.is_(True))
                .order_by(IngestionRun.id)
            ).scalars()
        )
        with_stats = set(
            session.execute(
                select(PlayerMatchStats.ingestion_run_id)
                .where(PlayerMatchStats.ingestion_run_id.in_(runs))
                .distinct()
            ).scalars()
        )
    return [run_id for run_id in runs if run_id in with_stats]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)

    common = dict(
        season_ref=args.season_ref,
        tour_refs=args.tours,
        models=tuple(args.models) if args.models else DEFAULT_MODELS,
        optimize=not args.no_optimize,
        fixture_conflict_weight=args.fixture_conflict_weight,
        top_errors=args.top_errors,
        top_unstable=args.top_unstable,
        carry_squad=args.carry_squad,
        max_transfers=args.max_transfers,
        min_transfer_gain=args.min_transfer_gain,
        transfer_gain_sigma=args.transfer_gain_sigma,
        captain_risk_weight=args.captain_risk_weight,
        horizon_tours=args.horizon_tours,
        horizon_decay=args.horizon_decay,
        parallel_sources=not args.no_parallel,
    )

    reports: list[dict[str, Any]] = []
    try:
        run_ids: list[int | None]
        if args.all_active:
            run_ids = list(_active_run_ids(session_factory))
            if not run_ids:
                print("No published snapshot has played tours", file=sys.stderr)
                return 2
        else:
            run_ids = list(args.run_ids) if args.run_ids else [None]
        for run_id in run_ids:
            reports.append(run_backtest(session_factory, run_id=run_id, **common))
    except BacktestError as error:
        print(f"Backtest failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Backtest error: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    output_dir = Path(args.output)
    multi = len(reports) > 1
    summaries: list[dict[str, Any]] = []
    passed = True
    for report in reports:
        target = output_dir / f"run-{report['run_id']}" if multi else output_dir
        paths = _write_outputs(report, target)
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
        summaries.append(summary)
        passed = passed and bool(report["cutoff_audit"]["passed"])

    if multi:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "summary.md").write_text(_summary_markdown(reports), encoding="utf-8")
        (output_dir / "summary.json").write_text(
            json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if not args.quiet:
            print(f"  summary written to {output_dir / 'summary.md'}", file=sys.stderr)
        print(json.dumps(summaries, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(summaries[0], ensure_ascii=False, indent=2))

    # A leakage violation invalidates the whole comparison, so it fails the run.
    return 0 if passed else 3


if __name__ == "__main__":
    raise SystemExit(main())
