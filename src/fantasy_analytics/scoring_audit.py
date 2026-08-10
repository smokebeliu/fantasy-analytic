"""Check that the scoring table describes a league's real points (step 22).

The event model turns per-90 rates into expected points through :data:`
fantasy_analytics.forecast.SCORING`, a table that Sports.ru only publishes as an
image and that was therefore reconstructed empirically from one RPL season. As
soon as the pipeline serves other leagues that reconstruction stops being a
detail: if La Liga paid, say, 5 points for a defender's goal, every La Liga
projection and every squad the optimizer returned for it would be quietly wrong.

The audit replays :func:`fantasy_analytics.forecast.reconstruct_points` over every
imported player-match row and compares it against ``points``, the authoritative
score Sports.ru assigned. The absolute accuracy is not the point — the table
deliberately omits events the imported columns do not carry (red cards, own goals,
conceded penalties, the indirect "fantasy assist"), so a perfect match is not
expected. What matters is the *comparison between leagues*: a league scored by the
same rules as the RPL lands at the same accuracy, and one scored differently
collapses. That makes this a cheap, repeatable check to run after importing a new
league.

Only matches the player actually played are scored. A row with no minutes is worth
zero points and is reconstructed exactly, so including them would inflate every
league's accuracy by the same uninformative amount.

The audit is also how the table gets corrected. Pooled across roles it reported a
healthy 78% exact on La Liga while the goalkeeper row was wrong by more than two
points a match, because keepers are 6% of the rows: read the per-role breakdown,
not only the total.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from typing import Any, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db import create_db_engine, create_session_factory, session_scope
from .db.models import (
    Competition,
    IngestionRun,
    PlayerMatchStats,
    PlayerSeason,
    Season,
)
from .forecast import SCORING_VERSION, reconstruct_points

# Difference bands the report summarises, in points.
TOLERANCES = (0, 1, 2)


def audit_run(session: Session, run_id: int) -> dict[str, Any]:
    """Score every played match of one snapshot and summarise the residuals."""
    rows = session.execute(
        select(
            PlayerSeason.role,
            PlayerMatchStats.field_minutes,
            PlayerMatchStats.points,
            PlayerMatchStats.goals,
            PlayerMatchStats.assists,
            PlayerMatchStats.saves,
            PlayerMatchStats.ball_recoveries,
            PlayerMatchStats.yellow_cards,
            PlayerMatchStats.goals_conceded,
        )
        .join(PlayerSeason, PlayerMatchStats.player_season_id == PlayerSeason.id)
        .where(
            PlayerMatchStats.ingestion_run_id == run_id,
            PlayerMatchStats.field_minutes > 0,
        )
    ).all()

    residuals: Counter[int] = Counter()
    by_role: dict[str, Counter[int]] = {}
    for row in rows:
        predicted = reconstruct_points(
            role=row.role,
            minutes=row.field_minutes,
            goals=row.goals,
            assists=row.assists,
            saves=row.saves,
            ball_recoveries=row.ball_recoveries,
            yellow_cards=row.yellow_cards,
            goals_conceded=row.goals_conceded,
        )
        residual = predicted - row.points
        residuals[residual] += 1
        by_role.setdefault(row.role, Counter())[residual] += 1

    def shares(counter: Counter[int], total: int) -> dict[str, float]:
        if total == 0:
            return {
                "mean_residual": 0.0,
                **{f"within_{t}": 0.0 for t in TOLERANCES},
            }
        return {
            "mean_residual": round(
                sum(diff * count for diff, count in counter.items()) / total, 4
            ),
            **{
                f"within_{t}": round(
                    sum(count for diff, count in counter.items() if abs(diff) <= t)
                    / total,
                    4,
                )
                for t in TOLERANCES
            },
        }

    total = len(rows)
    return {
        "run_id": run_id,
        "scoring_version": SCORING_VERSION,
        "appearances": total,
        **shares(residuals, total),
        "by_role": {
            role: {"appearances": sum(counter.values()), **shares(counter, sum(counter.values()))}
            for role, counter in sorted(by_role.items())
        },
        "top_residuals": [
            {"difference": diff, "appearances": count}
            for diff, count in sorted(
                residuals.items(), key=lambda item: -item[1]
            )[:8]
        ],
    }


def audit_active_snapshots(session_factory: sessionmaker) -> list[dict[str, Any]]:
    """Audit the published snapshot of every imported league."""
    with session_scope(session_factory) as session:
        runs = session.execute(
            select(
                IngestionRun.id,
                Competition.slug,
                Competition.name,
                Season.name.label("season_name"),
                Competition.sort_order,
            )
            .join(Season, IngestionRun.season_id == Season.id)
            .join(Competition, Season.competition_id == Competition.id)
            .where(IngestionRun.is_active.is_(True))
            .order_by(Competition.sort_order, IngestionRun.id)
        ).all()
        return [
            {
                "slug": run.slug,
                "competition": run.name,
                "season": run.season_name,
                **audit_run(session, run.id),
            }
            for run in runs
        ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-scoring-audit",
        description=(
            "Reconstruct imported fantasy points from the scoring table and "
            "report how well it fits each league."
        ),
    )
    parser.add_argument(
        "--database-url", help="Override DATABASE_URL for this command"
    )
    parser.add_argument(
        "--run-id",
        type=int,
        help="Audit one ingestion run instead of every published snapshot",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the full report as JSON instead of a table",
    )
    return parser


def _print_table(reports: list[dict[str, Any]]) -> None:
    header = (
        f"{'slug':<18} {'season':<12} {'appearances':>11} "
        f"{'exact':>7} {'±1':>7} {'±2':>7} {'mean':>7}"
    )
    def _row(label: str, season: str, stats: dict[str, Any]) -> str:
        return (
            f"{label:<18} {season:<12} {stats['appearances']:>11} "
            f"{stats['within_0'] * 100:>6.1f}% {stats['within_1'] * 100:>6.1f}% "
            f"{stats['within_2'] * 100:>6.1f}% {stats['mean_residual']:>7.3f}"
        )

    print(f"scoring table: {SCORING_VERSION}")
    print(header)
    print("-" * len(header))
    for report in reports:
        print(_row(report.get("slug", "?"), report.get("season", "?"), report))
        # A role scored by different rules disappears into the pooled figure —
        # goalkeepers are 6% of the rows — so every role is reported separately.
        for role, stats in report.get("by_role", {}).items():
            print(_row(f"  {role.lower()}", "", stats))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)
    try:
        if args.run_id is not None:
            with session_scope(session_factory) as session:
                reports = [audit_run(session, args.run_id)]
        else:
            reports = audit_active_snapshots(session_factory)
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Scoring audit failed: {error}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    if not reports:
        print(
            "No published snapshot to audit; import a season first.",
            file=sys.stderr,
        )
        return 1
    if args.as_json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
    else:
        _print_table(reports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
