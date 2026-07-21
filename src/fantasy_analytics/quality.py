"""Data-quality and reconciliation gate (development-plan step 4).

The gate inspects the snapshot produced by an ingestion run and decides whether
it is trustworthy enough to become the *active* snapshot that later analytics
steps read. It runs a fixed set of checks, each split into ``blocking`` and
``warning`` severities:

* **blocking** — structural problems that make the snapshot unusable
  (empty catalog, unresolved references, duplicated fixtures, a player with
  minutes but no match history). A run with any blocking issue never becomes
  active.
* **warning** — numeric discrepancies that the 72-hour Sports.ru statistics
  adjustment window (or provider gaps) can still explain. They are recorded but
  do not block publication.

Every issue records the expected and actual value behind the comparison, and
all issues are persisted to ``data_quality_issues`` scoped to the run. The gate
is read-only with respect to the domain data: it only writes issues and toggles
``ingestion_runs.is_active``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .db import session_scope
from .db.models import (
    ClubMatchStats,
    ClubSeasonStats,
    FantasyTour,
    Match,
    PlayerMatchStats,
    PlayerSeason,
    PlayerSeasonStats,
    SeasonClub,
)
from .db.quality_repository import QualityRepository

BLOCKING = "blocking"
WARNING = "warning"

# Sports.ru freezes fantasy statistics 72 hours after the final match of a tour,
# so results and aggregates for matches inside this window may still change.
# Reconciliation mismatches that involve such matches are downgraded to warnings.
STAT_ADJUSTMENT_WINDOW = timedelta(hours=72)

# Cap the number of per-entity issues persisted for a single check so a
# systemic discrepancy cannot flood the table; the true total is kept in the
# check summary.
MAX_ISSUES_PER_CHECK = 200


class QualityError(RuntimeError):
    """Raised when the gate cannot resolve a run or its season to evaluate."""


@dataclass(frozen=True)
class QualityIssue:
    check_name: str
    severity: str
    message: str
    entity_type: str | None = None
    entity_ref: str | None = None
    expected: str | None = None
    actual: str | None = None
    details: dict[str, Any] | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_ref": self.entity_ref,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class CheckResult:
    name: str
    expected: Any
    actual: Any
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(issue.severity == BLOCKING for issue in self.issues):
            return BLOCKING
        if any(issue.severity == WARNING for issue in self.issues):
            return WARNING
        return "ok"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "blocking": sum(1 for i in self.issues if i.severity == BLOCKING),
            "warnings": sum(1 for i in self.issues if i.severity == WARNING),
            "issues": [issue.as_row() for issue in self.issues],
        }


@dataclass
class QualityContext:
    session: Session
    run_id: int
    season_id: int
    now: datetime


CheckFn = Callable[[QualityContext], CheckResult]


def _cap(issues: list[QualityIssue]) -> list[QualityIssue]:
    return issues[:MAX_ISSUES_PER_CHECK]


def check_catalog_completeness(ctx: QualityContext) -> CheckResult:
    """The season must expose clubs, tours, matches and players."""
    s = ctx.session
    counts = {
        "clubs": s.execute(
            select(func.count(func.distinct(SeasonClub.club_id))).where(
                SeasonClub.season_id == ctx.season_id
            )
        ).scalar_one(),
        "season_clubs": s.execute(
            select(func.count())
            .select_from(SeasonClub)
            .where(SeasonClub.season_id == ctx.season_id)
        ).scalar_one(),
        "tours": s.execute(
            select(func.count())
            .select_from(FantasyTour)
            .where(FantasyTour.season_id == ctx.season_id)
        ).scalar_one(),
        "matches": s.execute(
            select(func.count())
            .select_from(Match)
            .where(Match.season_id == ctx.season_id)
        ).scalar_one(),
        "players": s.execute(
            select(func.count())
            .select_from(PlayerSeason)
            .where(PlayerSeason.season_id == ctx.season_id)
        ).scalar_one(),
    }
    issues: list[QualityIssue] = []
    for entity in ("season_clubs", "tours", "matches", "players"):
        if counts[entity] == 0:
            issues.append(
                QualityIssue(
                    check_name="catalog_completeness",
                    severity=BLOCKING,
                    entity_type=entity,
                    message=f"Season has no {entity}; the snapshot is empty",
                    expected="> 0",
                    actual="0",
                )
            )
    return CheckResult(
        name="catalog_completeness",
        expected={entity: "> 0" for entity in counts},
        actual=counts,
        issues=issues,
    )


def check_reference_integrity(ctx: QualityContext) -> CheckResult:
    """Every match and player must reference clubs registered in the season."""
    s = ctx.session
    club_ids = set(
        s.execute(
            select(SeasonClub.club_id).where(SeasonClub.season_id == ctx.season_id)
        ).scalars()
    )
    season_club_ids = set(
        s.execute(
            select(SeasonClub.id).where(SeasonClub.season_id == ctx.season_id)
        ).scalars()
    )

    issues: list[QualityIssue] = []
    violations = 0

    matches = s.execute(
        select(
            Match.stat_match_id,
            Match.home_club_id,
            Match.away_club_id,
        ).where(Match.season_id == ctx.season_id)
    ).all()
    for stat_match_id, home_club_id, away_club_id in matches:
        problems = []
        if home_club_id not in club_ids:
            problems.append(f"home club {home_club_id} not in season")
        if away_club_id not in club_ids:
            problems.append(f"away club {away_club_id} not in season")
        if home_club_id == away_club_id:
            problems.append("home and away clubs are identical")
        if problems:
            violations += 1
            issues.append(
                QualityIssue(
                    check_name="reference_integrity",
                    severity=BLOCKING,
                    entity_type="match",
                    entity_ref=str(stat_match_id),
                    message="; ".join(problems),
                    expected="both clubs registered and distinct",
                    actual=f"home={home_club_id}, away={away_club_id}",
                )
            )

    orphan_players = s.execute(
        select(func.count())
        .select_from(PlayerSeason)
        .where(
            PlayerSeason.season_id == ctx.season_id,
            PlayerSeason.current_season_club_id.isnot(None),
            PlayerSeason.current_season_club_id.notin_(season_club_ids or {-1}),
        )
    ).scalar_one()
    if orphan_players:
        violations += orphan_players
        issues.append(
            QualityIssue(
                check_name="reference_integrity",
                severity=BLOCKING,
                entity_type="player_season",
                message=(
                    f"{orphan_players} player(s) reference a club outside the season"
                ),
                expected="current club within season",
                actual=f"{orphan_players} orphaned",
            )
        )

    return CheckResult(
        name="reference_integrity",
        expected="all match/player club references resolve within the season",
        actual={"violations": violations},
        issues=_cap(issues),
    )


def check_duplicate_fixtures(ctx: QualityContext) -> CheckResult:
    """A (tour, home club, away club) fixture must appear at most once."""
    s = ctx.session
    rows = s.execute(
        select(
            Match.tour_id,
            Match.home_club_id,
            Match.away_club_id,
            func.count().label("n"),
            func.array_agg(Match.stat_match_id).label("ids"),
        )
        .where(Match.season_id == ctx.season_id)
        .group_by(Match.tour_id, Match.home_club_id, Match.away_club_id)
        .having(func.count() > 1)
    ).all()

    issues: list[QualityIssue] = []
    for tour_id, home_club_id, away_club_id, n, ids in rows:
        issues.append(
            QualityIssue(
                check_name="duplicate_fixtures",
                severity=BLOCKING,
                entity_type="match",
                entity_ref=",".join(str(i) for i in ids),
                message=(
                    f"Fixture (tour {tour_id}, {home_club_id} vs {away_club_id}) "
                    f"appears {n} times"
                ),
                expected="1",
                actual=str(n),
                details={"stat_match_ids": [str(i) for i in ids]},
            )
        )
    return CheckResult(
        name="duplicate_fixtures",
        expected="each fixture appears once",
        actual={"duplicate_groups": len(rows)},
        issues=_cap(issues),
    )


def check_match_score_completeness(ctx: QualityContext) -> CheckResult:
    """Matches older than the adjustment window should carry a final score."""
    s = ctx.session
    cutoff = ctx.now - STAT_ADJUSTMENT_WINDOW
    rows = s.execute(
        select(Match.stat_match_id, Match.scheduled_at)
        .where(
            Match.season_id == ctx.season_id,
            Match.scheduled_at < cutoff,
            (Match.home_score.is_(None)) | (Match.away_score.is_(None)),
        )
        .order_by(Match.scheduled_at)
    ).all()
    issues = [
        QualityIssue(
            check_name="match_score_completeness",
            severity=WARNING,
            entity_type="match",
            entity_ref=str(stat_match_id),
            message=(
                "Match older than the 72h adjustment window has no final score"
            ),
            expected="home and away score present",
            actual="missing score",
            details={
                "scheduled_at": scheduled_at.isoformat() if scheduled_at else None
            },
        )
        for stat_match_id, scheduled_at in rows
    ]
    return CheckResult(
        name="match_score_completeness",
        expected="past matches have final scores",
        actual={"missing_scores": len(rows)},
        issues=_cap(issues),
    )


def _clubs_within_window(ctx: QualityContext) -> set[int]:
    """Club ids whose latest finished match is inside the adjustment window."""
    cutoff = ctx.now - STAT_ADJUSTMENT_WINDOW
    rows = ctx.session.execute(
        select(func.distinct(ClubMatchStats.club_id))
        .join(Match, ClubMatchStats.match_id == Match.id)
        .where(
            Match.season_id == ctx.season_id,
            Match.scheduled_at >= cutoff,
            ClubMatchStats.goals_scored.isnot(None),
        )
    ).scalars()
    return set(rows)


def check_club_result_reconciliation(ctx: QualityContext) -> CheckResult:
    """Club season aggregates must match results derived from their matches."""
    s = ctx.session
    recent_clubs = _clubs_within_window(ctx)

    # Derive played/goals/results per club from the finished match rows.
    derived: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "matches_played": 0,
            "goals_scored": 0,
            "goals_conceded": 0,
            "matches_won": 0,
            "matches_drawn": 0,
            "matches_lost": 0,
        }
    )
    match_rows = s.execute(
        select(
            ClubMatchStats.club_id,
            ClubMatchStats.goals_scored,
            ClubMatchStats.goals_conceded,
        )
        .join(Match, ClubMatchStats.match_id == Match.id)
        .where(
            Match.season_id == ctx.season_id,
            ClubMatchStats.goals_scored.isnot(None),
            ClubMatchStats.goals_conceded.isnot(None),
        )
    ).all()
    for club_id, gf, ga in match_rows:
        agg = derived[club_id]
        agg["matches_played"] += 1
        agg["goals_scored"] += gf
        agg["goals_conceded"] += ga
        if gf > ga:
            agg["matches_won"] += 1
        elif gf == ga:
            agg["matches_drawn"] += 1
        else:
            agg["matches_lost"] += 1

    # Stored aggregate for this run, keyed by club id via the season club.
    stored_rows = s.execute(
        select(
            SeasonClub.club_id,
            SeasonClub.display_name,
            ClubSeasonStats.matches_played,
            ClubSeasonStats.goals_scored,
            ClubSeasonStats.goals_conceded,
            ClubSeasonStats.matches_won,
            ClubSeasonStats.matches_drawn,
            ClubSeasonStats.matches_lost,
        )
        .join(ClubSeasonStats, ClubSeasonStats.season_club_id == SeasonClub.id)
        .where(
            SeasonClub.season_id == ctx.season_id,
            ClubSeasonStats.ingestion_run_id == ctx.run_id,
        )
    ).all()

    metrics = (
        "matches_played",
        "goals_scored",
        "goals_conceded",
        "matches_won",
        "matches_drawn",
        "matches_lost",
    )
    issues: list[QualityIssue] = []
    mismatched = 0
    for row in stored_rows:
        club_id = row.club_id
        derived_agg = derived.get(club_id)
        if derived_agg is None:
            continue
        stored = {metric: getattr(row, metric) for metric in metrics}
        diffs = {
            metric: (derived_agg[metric], stored[metric])
            for metric in metrics
            if derived_agg[metric] != stored[metric]
        }
        if not diffs:
            continue
        mismatched += 1
        severity = WARNING if club_id in recent_clubs else BLOCKING
        detail_str = ", ".join(
            f"{metric}: matches={d}/aggregate={a}"
            for metric, (d, a) in diffs.items()
        )
        issues.append(
            QualityIssue(
                check_name="club_result_reconciliation",
                severity=severity,
                entity_type="club",
                entity_ref=row.display_name,
                message=f"Club aggregate disagrees with match results ({detail_str})",
                expected=str({m: derived_agg[m] for m in diffs}),
                actual=str({m: stored[m] for m in diffs}),
                details={
                    "within_adjustment_window": club_id in recent_clubs,
                    "diffs": {m: {"matches": d, "aggregate": a} for m, (d, a) in diffs.items()},
                },
            )
        )
    return CheckResult(
        name="club_result_reconciliation",
        expected="club season aggregates equal results derived from matches",
        actual={"clubs_compared": len(stored_rows), "mismatched": mismatched},
        issues=_cap(issues),
    )


def check_player_points_reconciliation(ctx: QualityContext) -> CheckResult:
    """Season fantasy aggregates must match the sum of per-match player stats."""
    s = ctx.session

    aggregate_rows = s.execute(
        select(
            PlayerSeason.id,
            PlayerSeason.fantasy_player_id,
            PlayerSeasonStats.points,
            PlayerSeasonStats.goals,
            PlayerSeasonStats.assists,
            PlayerSeasonStats.field_minutes,
        )
        .join(
            PlayerSeasonStats,
            PlayerSeasonStats.player_season_id == PlayerSeason.id,
        )
        .where(
            PlayerSeason.season_id == ctx.season_id,
            PlayerSeasonStats.ingestion_run_id == ctx.run_id,
        )
    ).all()

    match_sums = {
        row.player_season_id: row
        for row in s.execute(
            select(
                PlayerMatchStats.player_season_id,
                func.count().label("matches"),
                func.coalesce(func.sum(PlayerMatchStats.points), 0).label("points"),
                func.coalesce(func.sum(PlayerMatchStats.goals), 0).label("goals"),
                func.coalesce(func.sum(PlayerMatchStats.assists), 0).label("assists"),
                func.coalesce(
                    func.sum(PlayerMatchStats.field_minutes), 0
                ).label("field_minutes"),
            )
            .join(
                PlayerSeason,
                PlayerMatchStats.player_season_id == PlayerSeason.id,
            )
            .where(
                PlayerSeason.season_id == ctx.season_id,
                PlayerMatchStats.ingestion_run_id == ctx.run_id,
            )
            .group_by(PlayerMatchStats.player_season_id)
        ).all()
    }

    metrics = ("points", "goals", "assists", "field_minutes")
    issues: list[QualityIssue] = []
    missing_history = 0
    mismatched = 0
    for row in aggregate_rows:
        summed = match_sums.get(row.id)
        match_count = summed.matches if summed else 0
        if match_count == 0:
            # A player who accumulated minutes must have match history; its
            # absence signals a dropped page during ingestion.
            if row.field_minutes and row.field_minutes > 0:
                missing_history += 1
                issues.append(
                    QualityIssue(
                        check_name="player_points_reconciliation",
                        severity=BLOCKING,
                        entity_type="player_season",
                        entity_ref=str(row.fantasy_player_id),
                        message=(
                            "Player has season minutes but no match history "
                            "(likely a missing ingestion page)"
                        ),
                        expected=f"match history for {row.field_minutes} minutes",
                        actual="0 matches",
                    )
                )
            continue
        diffs = {
            metric: (getattr(row, metric), getattr(summed, metric))
            for metric in metrics
            if getattr(row, metric) != getattr(summed, metric)
        }
        if not diffs:
            continue
        mismatched += 1
        detail_str = ", ".join(
            f"{metric}: aggregate={a}/matches={m}" for metric, (a, m) in diffs.items()
        )
        issues.append(
            QualityIssue(
                check_name="player_points_reconciliation",
                severity=WARNING,
                entity_type="player_season",
                entity_ref=str(row.fantasy_player_id),
                message=(
                    "Season aggregate disagrees with summed match stats "
                    f"({detail_str}); may reflect the 72h adjustment window"
                ),
                expected=str({m: getattr(row, m) for m in diffs}),
                actual=str({m: getattr(summed, m) for m in diffs}),
                details={"diffs": {m: {"aggregate": a, "matches": v} for m, (a, v) in diffs.items()}},
            )
        )
    return CheckResult(
        name="player_points_reconciliation",
        expected="season fantasy aggregates equal the sum of match stats",
        actual={
            "players_compared": len(aggregate_rows),
            "missing_history": missing_history,
            "mismatched": mismatched,
        },
        issues=_cap(issues),
    )


CHECKS: tuple[CheckFn, ...] = (
    check_catalog_completeness,
    check_reference_integrity,
    check_duplicate_fixtures,
    check_match_score_completeness,
    check_club_result_reconciliation,
    check_player_points_reconciliation,
)


def evaluate_run(ctx: QualityContext) -> list[CheckResult]:
    """Run every check against the context and return their results."""
    return [check(ctx) for check in CHECKS]


def run_quality_checks(
    session_factory: sessionmaker,
    *,
    run_id: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate a run's snapshot, persist issues and publish if it passes.

    When ``run_id`` is omitted the latest successful run is evaluated. The
    snapshot is marked active only when no blocking issue is found, so an
    invalid snapshot never supersedes the last valid one.
    """
    evaluated_at = now or datetime.now(UTC)
    with session_scope(session_factory) as session:
        repo = QualityRepository(session)
        run = repo.get_run(run_id) if run_id is not None else repo.latest_succeeded_run()
        if run is None:
            raise QualityError(
                "No ingestion run to evaluate; run 'fantasy-ingest' first"
            )
        season_id = repo.resolve_season_id(run)
        if season_id is None:
            raise QualityError(
                f"Could not resolve the season for ingestion run {run.id}; "
                "its snapshot is empty or missing aggregates"
            )

        ctx = QualityContext(
            session=session,
            run_id=run.id,
            season_id=season_id,
            now=evaluated_at,
        )
        results = evaluate_run(ctx)
        all_issues = [issue for result in results for issue in result.issues]
        blocking = sum(1 for issue in all_issues if issue.severity == BLOCKING)
        warnings = sum(1 for issue in all_issues if issue.severity == WARNING)
        passed = blocking == 0

        repo.replace_issues(
            run, season_id, [issue.as_row() for issue in all_issues]
        )
        repo.publish(run, season_id, active=passed, checked_at=evaluated_at)

        report = {
            "run_id": run.id,
            "season_id": season_id,
            "generated_at": evaluated_at.isoformat(),
            "passed": passed,
            "is_active": run.is_active,
            "counts": {"blocking": blocking, "warnings": warnings},
            "checks": [result.as_dict() for result in results],
        }
        return report


__all__ = [
    "BLOCKING",
    "WARNING",
    "STAT_ADJUSTMENT_WINDOW",
    "QualityError",
    "QualityIssue",
    "CheckResult",
    "QualityContext",
    "CHECKS",
    "evaluate_run",
    "run_quality_checks",
]
