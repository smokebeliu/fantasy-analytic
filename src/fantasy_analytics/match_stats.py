"""Extended match-statistics discovery spike (development plan step 3).

This module answers a single question: which football metrics does the
Sports.ru ``statMatch`` API expose reliably for finished RPL matches?

It takes a reproducible sample of matches (spread across the season and
covering every club), fetches the extended statistics for each one, stores the
raw payloads and produces a field-coverage report. The report tells later steps
which GraphQL paths are safe to adopt, which are only partially populated and
which must be excluded.

The step is a read-only investigation: it proposes model changes but does not
build any forecast or write extended stats into the domain schema.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, Sequence

from .client import SportsGraphQLClient
from .queries import MATCH_STATS_QUERY

ProgressCallback = Callable[[str], None]

# Fill-rate thresholds that turn a raw coverage number into a modelling
# decision. They are intentionally strict: a metric must be almost always
# present before a later analytics step is allowed to depend on it.
ADOPT_THRESHOLD = 0.90
CONDITIONAL_THRESHOLD = 0.50
SPARSE_THRESHOLD = 0.0


class MatchStatsError(RuntimeError):
    """Raised when the live response does not satisfy the expected contract."""


@dataclass(frozen=True)
class MatchRef:
    """A finished match to sample, described by catalog attributes."""

    stat_match_id: str
    tour_name: str
    tour_order: int
    home_club: str
    away_club: str
    scheduled_at: datetime | None
    home_score: int | None = None
    away_score: int | None = None

    @property
    def clubs(self) -> tuple[str, str]:
        return (self.home_club, self.away_club)


@dataclass(frozen=True)
class MatchStatsOptions:
    output_dir: Path
    sample_size: int = 40
    source: str | None = None


def _sort_key(match: MatchRef) -> tuple[Any, ...]:
    stamp = match.scheduled_at
    ordinal = stamp.timestamp() if isinstance(stamp, datetime) else 0.0
    return (ordinal, match.tour_order, match.stat_match_id)


def select_match_sample(
    matches: Sequence[MatchRef],
    sample_size: int = 40,
) -> list[MatchRef]:
    """Pick a deterministic, well-spread sample of matches.

    The selection guarantees at least ``max(sample_size, 30)`` matches when
    enough are available, spreads them evenly across the season timeline and
    then tops up so that every club appears at least once. The result is
    reproducible: the same catalog always yields the same sample.
    """
    finished = [
        match
        for match in matches
        if match.home_score is not None and match.away_score is not None
    ]
    ordered = sorted(finished, key=_sort_key)
    total = len(ordered)
    target = max(sample_size, 30)
    if total <= target:
        return ordered

    # Evenly spaced indices across the whole season keep the sample
    # representative of early, mid and late tours.
    indices = sorted({round(i * (total - 1) / (target - 1)) for i in range(target)})
    selected = [ordered[index] for index in indices]
    selected_ids = {match.stat_match_id for match in selected}

    covered: set[str] = set()
    for match in selected:
        covered.update(match.clubs)
    all_clubs = {club for match in ordered for club in match.clubs}

    for club in sorted(all_clubs - covered):
        for match in ordered:
            if match.stat_match_id in selected_ids:
                continue
            if club in match.clubs:
                selected.append(match)
                selected_ids.add(match.stat_match_id)
                covered.update(match.clubs)
                break

    return sorted(selected, key=_sort_key)


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    return "object"


def iter_leaf_paths(value: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
    """Yield ``(normalized_path, leaf_value)`` for every scalar in a payload.

    List indices collapse to ``[]`` and the ``home``/``away`` roots collapse to
    ``side`` so both teams and all lineup entries aggregate into one coverage
    number per logical field.
    """
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = "side" if key in ("home", "away") and not prefix else key
            child_prefix = normalized if not prefix else f"{prefix}.{normalized}"
            yield from iter_leaf_paths(child, child_prefix)
    elif isinstance(value, list):
        child_prefix = f"{prefix}[]"
        for item in value:
            if isinstance(item, (dict, list)):
                yield from iter_leaf_paths(item, child_prefix)
            else:
                yield (child_prefix, item)
    else:
        yield (prefix, value)


@dataclass
class _FieldCoverage:
    path: str
    observed: int = 0
    non_null: int = 0
    types: set[str] = field(default_factory=set)

    def record(self, value: Any) -> None:
        self.observed += 1
        if value is None:
            self.types.add("null")
            return
        self.non_null += 1
        self.types.add(_json_type(value))

    @property
    def fill_rate(self) -> float:
        if self.observed == 0:
            return 0.0
        return self.non_null / self.observed

    def decision(self) -> str:
        if self.observed == 0:
            return "ABSENT"
        rate = self.fill_rate
        if rate >= ADOPT_THRESHOLD:
            return "ADOPT"
        if rate >= CONDITIONAL_THRESHOLD:
            return "CONDITIONAL"
        if rate > SPARSE_THRESHOLD:
            return "SPARSE"
        return "EXCLUDE"

    def as_dict(self) -> dict[str, Any]:
        non_null_types = sorted(self.types - {"null"}) or ["null"]
        return {
            "path": self.path,
            "type": "|".join(non_null_types),
            "observed": self.observed,
            "non_null": self.non_null,
            "fill_rate": round(self.fill_rate, 4),
            "decision": self.decision(),
        }


class CoverageAccumulator:
    """Aggregate field-level fill rates across every sampled match."""

    def __init__(self) -> None:
        self._fields: "OrderedDict[str, _FieldCoverage]" = OrderedDict()

    def add_match(self, match_payload: dict[str, Any]) -> None:
        for path, value in iter_leaf_paths(match_payload):
            coverage = self._fields.get(path)
            if coverage is None:
                coverage = _FieldCoverage(path=path)
                self._fields[path] = coverage
            coverage.record(value)

    def table(self) -> list[dict[str, Any]]:
        return [coverage.as_dict() for coverage in sorted(self._fields.values(), key=lambda item: item.path)]

    def decision_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for coverage in self._fields.values():
            decision = coverage.decision()
            counts[decision] = counts.get(decision, 0) + 1
        return counts


def _match_consistency(match: MatchRef, payload: dict[str, Any]) -> dict[str, Any]:
    """Cross-check a match payload against the imported catalog."""
    home = payload.get("home") or {}
    away = payload.get("away") or {}

    def _starters(side: dict[str, Any]) -> int:
        return sum(
            1
            for line in (side.get("lineup") or [])
            if line.get("lineupStarting")
        )

    home_score = home.get("score")
    away_score = away.get("score")
    return {
        "stat_match_id": match.stat_match_id,
        "score_matches_catalog": (
            home_score == match.home_score and away_score == match.away_score
        ),
        "eleven_home_starters": _starters(home) == 11,
        "eleven_away_starters": _starters(away) == 11,
        "has_detail_stat": bool(payload.get("hasDetailStat")),
        "has_lineups": bool(payload.get("hasLineups")),
        "has_events": bool(payload.get("hasEvents")),
        "has_person_stat": bool(payload.get("hasPersonStat")),
        "has_xg": bool(payload.get("hasXG")),
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def fetch_match_stats(
    client: SportsGraphQLClient,
    stat_match_id: str,
    source: str | None = None,
) -> dict[str, Any]:
    """Fetch and validate the extended statistics for a single match."""
    payload = client.execute(
        MATCH_STATS_QUERY,
        {"id": str(stat_match_id), "source": source},
    )
    match = (
        payload.get("data", {})
        .get("statQueries", {})
        .get("football", {})
        .get("match")
    )
    if not isinstance(match, dict):
        raise MatchStatsError(
            f"Match {stat_match_id} did not return a statMatch object"
        )
    return payload


def _availability_summary(consistency: list[dict[str, Any]]) -> dict[str, int]:
    keys = (
        "has_detail_stat",
        "has_lineups",
        "has_events",
        "has_person_stat",
        "has_xg",
        "score_matches_catalog",
        "eleven_home_starters",
        "eleven_away_starters",
    )
    return {key: sum(1 for row in consistency if row.get(key)) for key in keys}


def run_match_stats_discovery(
    client: SportsGraphQLClient,
    matches: Sequence[MatchRef],
    options: MatchStatsOptions,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Sample matches, fetch extended stats and build a coverage report."""

    def _progress(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    sample = select_match_sample(matches, options.sample_size)
    if not sample:
        raise MatchStatsError(
            "No finished matches available to sample; run the import first"
        )

    raw_dir = options.output_dir / "raw"
    accumulator = CoverageAccumulator()
    consistency: list[dict[str, Any]] = []
    fetched: list[dict[str, Any]] = []

    started = perf_counter()
    for index, match in enumerate(sample, start=1):
        _progress(
            f"[{index}/{len(sample)}] fetching match {match.stat_match_id} "
            f"({match.home_club} vs {match.away_club}, {match.tour_name})"
        )
        payload = fetch_match_stats(client, match.stat_match_id, options.source)
        _write_json(raw_dir / f"match-{match.stat_match_id}.json", payload)
        match_stat = payload["data"]["statQueries"]["football"]["match"]
        accumulator.add_match(match_stat)
        consistency.append(_match_consistency(match, match_stat))
        fetched.append(
            {
                "stat_match_id": match.stat_match_id,
                "tour_name": match.tour_name,
                "tour_order": match.tour_order,
                "home_club": match.home_club,
                "away_club": match.away_club,
                "scheduled_at": (
                    match.scheduled_at.isoformat()
                    if isinstance(match.scheduled_at, datetime)
                    else None
                ),
            }
        )
    duration = perf_counter() - started

    coverage_table = accumulator.table()
    sampled_clubs = sorted({club for match in sample for club in match.clubs})
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "operation": "statQueries.football.match",
        "source": options.source,
        "sample": {
            "requested_size": options.sample_size,
            "matches": len(sample),
            "clubs_covered": len(sampled_clubs),
            "tours_covered": len({match.tour_order for match in sample}),
            "clubs": sampled_clubs,
        },
        "duration_seconds": round(duration, 3),
        "availability": _availability_summary(consistency),
        "decision_counts": accumulator.decision_counts(),
        "coverage": coverage_table,
        "model_findings": _model_findings(coverage_table, consistency),
    }

    _write_json(options.output_dir / "sampled-matches.json", fetched)
    _write_json(options.output_dir / "match-consistency.json", consistency)
    _write_json(options.output_dir / "field-coverage.json", coverage_table)
    _write_json(options.output_dir / "report.json", report)
    (options.output_dir / "field-coverage.md").write_text(
        render_coverage_markdown(report),
        encoding="utf-8",
    )
    return report


def _model_findings(
    coverage: list[dict[str, Any]],
    consistency: list[dict[str, Any]],
) -> list[str]:
    matches = len(consistency)
    with_xg = sum(1 for row in consistency if row.get("has_xg"))
    findings: list[str] = []

    findings.append(
        "statQueries.football.match(id) returns a statMatch by stat_match_id "
        "without needing a source argument."
    )
    if with_xg == 0:
        findings.append(
            "xG is unavailable for the sampled RPL matches (hasXG is false and "
            "team/player xG fields are null); exclude xG-derived features for now."
        )
    elif with_xg < matches:
        findings.append(
            f"xG is only partially available ({with_xg}/{matches} matches expose "
            "hasXG); treat team/player xG as optional, never required."
        )
    adopt = [row["path"] for row in coverage if row["decision"] == "ADOPT"]
    excluded = [
        row["path"]
        for row in coverage
        if row["decision"] in ("EXCLUDE", "SPARSE", "ABSENT")
    ]
    findings.append(
        f"{len(adopt)} of {len(coverage)} measured fields are reliably populated "
        f"(>= {int(ADOPT_THRESHOLD * 100)}% fill) and safe to adopt."
    )
    findings.append(
        f"{len(excluded)} measured fields are missing or too sparse to depend on."
    )
    findings.append(
        "Identifier linkage: side.team.id is the stat team slug (clubs.stat_team_id) "
        "and side.lineup[].player.id is the stat player slug (players.stat_player_id); "
        "both join the extended stats back to the imported catalog."
    )
    findings.append(
        "Reliable team metrics: shotsTotal, shotsOnTarget, shotsOffTarget, "
        "ballPossession, cornerKicks, fouls and substitutions. Reliable player "
        "metrics are limited to goalsScored, cards, chancesCreated and ownGoals; "
        "passing, duels and xG breakdowns are too sparse."
    )
    return findings


def render_coverage_markdown(report: dict[str, Any]) -> str:
    """Render the coverage report as a Markdown document."""
    sample = report["sample"]
    lines: list[str] = []
    lines.append("# Extended match-statistics coverage")
    lines.append("")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append("")
    lines.append(
        f"Operation: `{report['operation']}` "
        f"(source: `{report['source']}`)."
    )
    lines.append("")
    lines.append(
        f"Sample: {sample['matches']} matches, {sample['clubs_covered']} clubs, "
        f"{sample['tours_covered']} tours."
    )
    lines.append("")
    availability = report["availability"]
    lines.append("## Match-level availability")
    lines.append("")
    lines.append("| Flag | Matches |")
    lines.append("| --- | --- |")
    for key, value in availability.items():
        lines.append(f"| `{key}` | {value}/{sample['matches']} |")
    lines.append("")
    lines.append("## Field coverage")
    lines.append("")
    lines.append("| GraphQL path | Type | Fill | Non-null/observed | Decision |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in report["coverage"]:
        lines.append(
            f"| `{row['path']}` | {row['type']} | {row['fill_rate']:.0%} | "
            f"{row['non_null']}/{row['observed']} | {row['decision']} |"
        )
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    for finding in report["model_findings"]:
        lines.append(f"- {finding}")
    lines.append("")
    return "\n".join(lines)
