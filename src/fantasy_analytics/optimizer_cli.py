"""Command-line entry point for the squad optimizer (development-plan step 8).

It rebuilds the expected-points forecast for a target tour from the active
snapshot, then solves the fantasy squad-selection integer program: the full
roster, the starting eleven, the captain, the vice-captain and the ordered
bench. The full result is written to disk and a compact, human-readable summary
is printed so the command composes in a pipeline.

Pass ``--current-squad`` (a comma-separated list of fantasy player ids) to run
in limited-transfers mode instead of building a fresh squad. ``--locked``,
``--locked-starters`` and ``--formation`` pin the user's own picks and shape, and
the optimizer fills the rest. ``--fixture-conflict-weight`` tunes how hard
starters that meet each other in the tour are penalised. The database is never
mutated and the Sports.ru API is never called.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .db import create_db_engine, create_session_factory
from .forecast import MODEL_EVENT, MODEL_MEAN, MODEL_RECENT
from .optimizer import (
    DEFAULT_FIXTURE_CONFLICT_WEIGHT,
    OptimizerError,
    build_squad_optimization,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-optimize",
        description=(
            "Select the optimal fantasy squad, starting eleven, captain and "
            "bench for a target tour from the active snapshot."
        ),
    )
    parser.add_argument("--database-url", help="Override DATABASE_URL for this command")
    parser.add_argument(
        "--run-id",
        type=int,
        help="Ingestion run to optimize from (default: the active snapshot)",
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
        "--model",
        default=MODEL_EVENT,
        choices=[MODEL_EVENT, MODEL_MEAN, MODEL_RECENT],
        help=f"Forecast model to optimize on (default: {MODEL_EVENT})",
    )
    parser.add_argument(
        "--current-squad",
        help=(
            "Comma-separated fantasy player ids of the current squad; enables "
            "limited-transfers mode"
        ),
    )
    parser.add_argument(
        "--max-transfers",
        type=int,
        help="Override the tour's transfer limit (limited-transfers mode only)",
    )
    parser.add_argument(
        "--locked",
        help=(
            "Comma-separated player ids that must be in the squad; the "
            "remaining slots are filled optimally"
        ),
    )
    parser.add_argument(
        "--locked-starters",
        help="Comma-separated player ids that must be in the starting eleven",
    )
    parser.add_argument(
        "--formation",
        help=(
            "Starting formation as defenders-midfielders-forwards, e.g. 4-4-2 "
            "(default: whatever the solver finds best)"
        ),
    )
    parser.add_argument(
        "--fixture-conflict-weight",
        type=float,
        help=(
            "How hard starters that meet each other in the tour are penalised "
            f"(default: {DEFAULT_FIXTURE_CONFLICT_WEIGHT}); 0 ignores the "
            "schedule but still reports the clashes"
        ),
    )
    parser.add_argument(
        "--output",
        default="data/optimizer",
        help="Directory for the optimizer artifacts (default: data/optimizer)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable summary on stderr",
    )
    return parser


def _parse_ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    ids = [part.strip() for part in raw.split(",") if part.strip()]
    return ids or None


def _write_outputs(report: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "optimizer.json"
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"report": str(path)}


def _summarize(report: dict[str, Any], paths: dict[str, str], stream) -> None:
    tour = report["tour"]
    solution = report["solution"]
    print(
        f"Optimizer v{report['optimizer_version']} ({report['mode']} mode, "
        f"model {report['model']}) for season {report['season']['name']} "
        f"tour {tour['fantasy_tour_id']} ({tour['name']}) from run {report['run_id']}",
        file=stream,
    )
    print(
        f"  status={solution['status']}; formation {solution['formation']}; "
        f"expected points (with captain) = {solution['objective_expected_points']}; "
        f"spent {solution['total_price']} / {report['rules']['total_budget']} "
        f"(unused {solution['unused_budget']})",
        file=stream,
    )
    fixtures = solution.get("fixtures") or {}
    print(
        f"  fixtures: {len(fixtures.get('head_to_head') or [])} head-to-head "
        f"fixture(s) in the eleven, {len(fixtures.get('clashes') or [])} clashing "
        f"pair(s); penalty {solution.get('fixture_penalty')} at weight "
        f"{fixtures.get('conflict_weight')} -> objective score "
        f"{solution.get('objective_score')}",
        file=stream,
    )
    for clash in fixtures.get("clashes") or []:
        print(
            f"    -{clash['penalty']:<6} {clash['role'][:3]} "
            f"{clash['player_name']} ({clash['club_name']}) vs "
            f"{clash['opponent_role'][:3]} {clash['opponent_player_name']} "
            f"({clash['opponent_club_name']})",
            file=stream,
        )
    transfers = solution.get("transfers")
    if transfers is not None:
        print(
            f"  transfers: {transfers['made']}/{transfers['allowed']} used, "
            f"kept {transfers['kept']}",
            file=stream,
        )
    constraints = solution.get("constraints") or {}
    if constraints.get("locked") or constraints.get("formation"):
        print(
            f"  pinned: {len(constraints.get('locked') or [])} player(s), "
            f"{len(constraints.get('locked_starters') or [])} as starters; "
            f"requested formation {constraints.get('formation') or 'any'}",
            file=stream,
        )
    print("  starting eleven ('*' = locked by the user):", file=stream)
    for player in solution["starting"]:
        tag = (
            " (C)"
            if player["is_captain"]
            else " (V)"
            if player["is_vice_captain"]
            else ""
        )
        pin = "*" if player.get("is_locked") else " "
        print(
            f"   {pin}{player['expected_points']:>6}  {player['role'][:3]:<3} "
            f"{player['player_name']}{tag} ({player['club_name']}, "
            f"{player['price']})",
            file=stream,
        )
    print("  bench:", file=stream)
    for player in solution["bench"]:
        pin = "*" if player.get("is_locked") else " "
        print(
            f"   {pin}#{player['bench_order']}  {player['role'][:3]:<3} "
            f"{player['player_name']} ({player['club_name']}, {player['price']})",
            file=stream,
        )
    print(f"  report written to {paths['report']}", file=stream)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)

    try:
        report = build_squad_optimization(
            session_factory,
            run_id=args.run_id,
            season_ref=args.season_ref,
            tour_ref=args.tour_ref,
            model=args.model,
            current_squad=_parse_ids(args.current_squad),
            max_transfers=args.max_transfers,
            locked=_parse_ids(args.locked),
            locked_starters=_parse_ids(args.locked_starters),
            formation=args.formation,
            fixture_conflict_weight=args.fixture_conflict_weight,
        )
    except OptimizerError as error:
        print(f"Optimization failed: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # noqa: BLE001 - surface a clean CLI error
        print(f"Optimizer error: {error}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()

    paths = _write_outputs(report, Path(args.output))

    if not args.quiet:
        _summarize(report, paths, sys.stderr)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
