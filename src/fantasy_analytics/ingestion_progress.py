"""Coarse progress stages for a manual ingestion job (development-plan step 17).

The importer and the quality gate report their progress as free-form English
messages through a ``ProgressCallback``. A user watching the refresh button in
the frontend needs something coarser and stable instead: *which stage* the job
is in and roughly how far it has come. This module is the single place that maps
one onto the other.

The mapping is intentionally one-way and lossy:

* :data:`STAGES` is the ordered, machine-readable stage vocabulary the API and
  the frontend agree on, each with a completion percentage.
* :func:`classify` recognises the messages :mod:`fantasy_analytics.ingestion`
  and :mod:`fantasy_analytics.quality` actually emit and returns the stage they
  belong to, or ``None`` for anything it does not know.
* :class:`ProgressTracker` turns the stream of messages into a monotonic
  (never-going-backwards) stage plus the latest message, so a late log line from
  an earlier stage cannot make the progress bar jump back.

Keeping the vocabulary here rather than in the worker means the frontend never
has to parse a log line, and an unrecognised message degrades into "same stage,
new message" instead of a wrong stage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# Ordered stages of a refresh job. ``percent`` is the completion *reached* when
# the stage starts, so a client can render a progress bar without knowing the
# stage semantics. The values are calibrated on a real RPL season import, where
# fetching the per-player match history dominates the runtime.
STAGES: tuple[tuple[str, int], ...] = (
    ("queued", 0),
    ("starting", 5),
    ("fetch_season", 10),
    ("fetch_players", 20),
    ("fetch_history", 45),
    ("persist", 75),
    ("quality_gate", 90),
    ("forecast", 95),
    ("finished", 100),
)

STAGE_ORDER: dict[str, int] = {name: index for index, (name, _) in enumerate(STAGES)}
STAGE_PERCENT: dict[str, int] = {name: percent for name, percent in STAGES}

# The first and last stage names, used by the worker and the API.
STAGE_QUEUED = STAGES[0][0]
STAGE_FINISHED = STAGES[-1][0]

# Longest message kept on the job row; a progress note is a hint, not a log.
MAX_MESSAGE_LENGTH = 200

# Message patterns emitted by the import/quality pipeline, most specific first.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^Created ingestion run\b", re.I), "starting"),
    (re.compile(r"^Job \d+ started\b", re.I), "starting"),
    (re.compile(r"^Selected season\b", re.I), "fetch_season"),
    (re.compile(r"^Fetched player page\b", re.I), "fetch_players"),
    (re.compile(r"^Fetching match history\b", re.I), "fetch_history"),
    (re.compile(r"^Fetched history \d+/\d+", re.I), "fetch_history"),
    (re.compile(r"^Fetch stage finished\b", re.I), "persist"),
    (re.compile(r"^Saved \d+ raw responses\b", re.I), "persist"),
    (re.compile(r"^Upserted\b", re.I), "persist"),
    (re.compile(r"^Ingestion run marked succeeded\b", re.I), "quality_gate"),
    (re.compile(r"^Running quality checks\b", re.I), "quality_gate"),
    (re.compile(r"^Forecasting the next tour\b", re.I), "forecast"),
    (re.compile(r"^Stored \d+ forecast rows\b", re.I), "forecast"),
    (re.compile(r"^Forecast for tour\b", re.I), "forecast"),
    (re.compile(r"^Job \d+ succeeded\b", re.I), "finished"),
)


def classify(message: str) -> str | None:
    """Return the stage a progress message belongs to, or ``None`` if unknown."""
    text = (message or "").strip()
    for pattern, stage in _PATTERNS:
        if pattern.search(text):
            return stage
    return None


def percent_for(stage: str) -> int:
    """Completion percentage of a stage; unknown stages report zero."""
    return STAGE_PERCENT.get(stage, 0)


def truncate(message: str) -> str:
    """Bound a progress message so the job row stays small."""
    text = (message or "").strip()
    if len(text) > MAX_MESSAGE_LENGTH:
        return text[: MAX_MESSAGE_LENGTH - 1] + "…"
    return text


@dataclass
class ProgressTracker:
    """Monotonic stage tracker over a stream of progress messages."""

    stage: str = STAGE_QUEUED
    message: str = ""

    def observe(self, message: str) -> tuple[str, int, str]:
        """Fold one message in and return ``(stage, percent, message)``.

        An unknown message keeps the current stage; a message belonging to an
        earlier stage never rewinds the progress the user already saw.
        """
        stage = classify(message)
        if stage is not None and STAGE_ORDER[stage] > STAGE_ORDER[self.stage]:
            self.stage = stage
        self.message = truncate(message)
        return self.stage, percent_for(self.stage), self.message

    def advance_to(self, stage: str) -> tuple[str, int]:
        """Force the tracker to a stage (used at lifecycle boundaries)."""
        if stage not in STAGE_ORDER:
            raise ValueError(f"Unknown ingestion stage: {stage!r}")
        if STAGE_ORDER[stage] > STAGE_ORDER[self.stage]:
            self.stage = stage
        return self.stage, percent_for(self.stage)


def stage_names() -> Iterable[str]:
    return (name for name, _ in STAGES)


__all__ = [
    "MAX_MESSAGE_LENGTH",
    "STAGES",
    "STAGE_FINISHED",
    "STAGE_ORDER",
    "STAGE_PERCENT",
    "STAGE_QUEUED",
    "ProgressTracker",
    "classify",
    "percent_for",
    "stage_names",
    "truncate",
]
