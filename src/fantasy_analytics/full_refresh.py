"""One button for the whole dataset: every imported league, both seasons, odds.

The admin screen imports one season of one league at a time, and a nightly
sweep only re-imports current seasons. Keeping the analytics fresh after a
model change therefore meant clicking through every league twice and then
the odds button — the *full refresh* does that sequence in one go:

    for each imported league, in catalogue order:
        1. import the latest completed season (the forecast's prior);
        2. import the current season, when the league has one;
        3. fetch the 1x2 line and rebuild the next-tour forecast.

The steps run strictly one after another: the imports are heavy (a full
season is ~40–90 s and a burst of GraphQL calls) and the odds refresh needs
the current season to be in place. Each import is a regular
``ingestion_jobs`` row driven by the usual worker process, so it shows up in
the per-league panel, survives an API restart and keeps the per-league lock.
The run itself is orchestrated in-process by :class:`FullRefreshManager` as
an asyncio task and lives in memory: the jobs it produced are durable, the
list of what came next is not, so a restart mid-run ends the run and the
admin starts it again (a league already imported today is simply
re-imported).

A failing step never stops the run — a broken league must not leave the
others stale — it is recorded on the step, the run carries on and finishes
``failed`` so the screen says something went wrong and where.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy.orm import sessionmaker

from .db import IngestionJobRepository, session_scope
from .db.job_repository import ACTIVE_STATUSES
from .ingestion_worker import safe_error_message
from .read_repository import ReadRepository

logger = logging.getLogger(__name__)

STEP_LATEST_COMPLETED = "latest_completed"
STEP_CURRENT_SEASON = "current_season"
STEP_ODDS = "odds"

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

# ``trigger_type`` stamped on the jobs a full refresh enqueues, so the job
# history tells a one-button run from a manual click or the nightly sweep.
FULL_REFRESH_TRIGGER = "full_refresh"

# How often the orchestrator re-reads a job it is waiting for, and how long it
# waits before giving the step up. A season import is a minute or two; an
# hour means something is wrong with the worker rather than the data.
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_JOB_TIMEOUT = 3600.0

SpawnWorker = Callable[[int], None]
RefreshOdds = Callable[..., dict[str, Any]]


class FullRefreshError(RuntimeError):
    """A full refresh could not be started."""


class FullRefreshBusy(FullRefreshError):
    """A full refresh is already running."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class FullRefreshStep:
    """One unit of the run: an import of one season or the odds of one league."""

    tournament_slug: str
    competition_name: str | None
    kind: str
    status: str = STATUS_PENDING
    job_id: int | None = None
    detail: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tournament_slug": self.tournament_slug,
            "competition_name": self.competition_name,
            "kind": self.kind,
            "status": self.status,
            "job_id": self.job_id,
            "detail": self.detail,
            "error": self.error,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }


@dataclass
class FullRefreshRun:
    """The state of one full refresh, as the admin screen polls it."""

    id: int
    status: str
    started_at: datetime
    steps: list[FullRefreshStep] = field(default_factory=list)
    finished_at: datetime | None = None

    @property
    def is_running(self) -> bool:
        return self.status == STATUS_RUNNING

    def as_dict(self) -> dict[str, Any]:
        done = sum(
            1
            for step in self.steps
            if step.status in (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_SKIPPED)
        )
        current = next(
            (step for step in self.steps if step.status == STATUS_RUNNING), None
        )
        return {
            "id": self.id,
            "status": self.status,
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "total_steps": len(self.steps),
            "completed_steps": done,
            "failed_steps": sum(1 for s in self.steps if s.status == STATUS_FAILED),
            "current_step": current.as_dict() if current else None,
            "steps": [step.as_dict() for step in self.steps],
        }


def plan_full_refresh(competitions: list[dict[str, Any]]) -> list[FullRefreshStep]:
    """Lay out the steps for every imported league, in catalogue order.

    Only leagues with at least one imported season are touched: the point is
    to refresh what the app already serves, not to pull in every league
    Sports.ru offers. A league without an active season in the catalogue gets
    its current-season step marked skipped up front, so the screen shows why
    nothing happened there.
    """
    steps: list[FullRefreshStep] = []
    for competition in competitions:
        if not competition.get("is_imported"):
            continue
        slug = str(competition["slug"])
        name = competition.get("name")
        steps.append(FullRefreshStep(slug, name, STEP_LATEST_COMPLETED))
        current = FullRefreshStep(slug, name, STEP_CURRENT_SEASON)
        if not competition.get("has_active_season"):
            current.status = STATUS_SKIPPED
            current.detail = "У лиги нет активного сезона в каталоге"
        steps.append(current)
        steps.append(FullRefreshStep(slug, name, STEP_ODDS))
    return steps


class FullRefreshManager:
    """Run and report the one-button refresh (one run at a time per process)."""

    def __init__(
        self,
        session_factory: sessionmaker,
        spawn_worker: SpawnWorker,
        refresh_odds: RefreshOdds,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        job_timeout: float = DEFAULT_JOB_TIMEOUT,
    ) -> None:
        self._session_factory = session_factory
        self._spawn_worker = spawn_worker
        self._refresh_odds = refresh_odds
        self._poll_interval = poll_interval
        self._job_timeout = job_timeout
        self._run: FullRefreshRun | None = None
        self._task: asyncio.Task[None] | None = None
        self._counter = 0

    # ------------------------------------------------------------------
    # Public surface.
    # ------------------------------------------------------------------
    @property
    def run(self) -> FullRefreshRun | None:
        return self._run

    def status(self) -> dict[str, Any]:
        """What the admin screen shows: the current or last run, or nothing."""
        return {
            "is_running": self._run is not None and self._run.is_running,
            "run": self._run.as_dict() if self._run is not None else None,
        }

    async def start(self) -> dict[str, Any]:
        """Plan a run from the catalogue and start executing it in the background.

        Raises :class:`FullRefreshBusy` while a run is in flight and
        :class:`FullRefreshError` when there is nothing imported to refresh.
        """
        if self._run is not None and self._run.is_running:
            raise FullRefreshBusy("A full refresh is already running")
        competitions = await asyncio.to_thread(self._list_competitions)
        steps = plan_full_refresh(competitions)
        if not steps:
            raise FullRefreshError(
                "Нет импортированных лиг: сначала импортируйте хотя бы одну лигу"
            )
        self._counter += 1
        self._run = FullRefreshRun(
            id=self._counter,
            status=STATUS_RUNNING,
            started_at=datetime.now(UTC),
            steps=steps,
        )
        self._task = asyncio.create_task(self._execute(self._run))
        return self.status()

    async def wait(self) -> None:
        """Block until the current run finishes (tests and shutdown)."""
        if self._task is not None:
            await self._task

    # ------------------------------------------------------------------
    # Execution.
    # ------------------------------------------------------------------
    def _list_competitions(self) -> list[dict[str, Any]]:
        with session_scope(self._session_factory) as session:
            return ReadRepository(session).list_competitions(imported_only=True)

    async def _execute(self, run: FullRefreshRun) -> None:
        logger.info("Full refresh %s started: %s steps", run.id, len(run.steps))
        for step in run.steps:
            if step.status == STATUS_SKIPPED:
                continue
            step.status = STATUS_RUNNING
            step.started_at = datetime.now(UTC)
            try:
                if step.kind == STEP_ODDS:
                    await self._run_odds(step)
                else:
                    await self._run_import(step)
                step.status = STATUS_SUCCEEDED
            except asyncio.CancelledError:
                step.status = STATUS_FAILED
                step.error = "Обновление прервано"
                step.finished_at = datetime.now(UTC)
                run.status = STATUS_FAILED
                run.finished_at = datetime.now(UTC)
                raise
            except Exception as error:  # noqa: BLE001 - every failure is reported
                step.status = STATUS_FAILED
                step.error = safe_error_message(error)
                logger.warning(
                    "Full refresh %s: %s/%s failed: %s",
                    run.id,
                    step.tournament_slug,
                    step.kind,
                    step.error,
                )
            finally:
                step.finished_at = datetime.now(UTC)
        failed = any(step.status == STATUS_FAILED for step in run.steps)
        run.status = STATUS_FAILED if failed else STATUS_SUCCEEDED
        run.finished_at = datetime.now(UTC)
        logger.info("Full refresh %s finished: %s", run.id, run.status)

    def _enqueue(self, step: FullRefreshStep) -> tuple[int, bool]:
        with session_scope(self._session_factory) as session:
            job, created = IngestionJobRepository(session).enqueue(
                tournament_slug=step.tournament_slug,
                use_current_season=step.kind == STEP_CURRENT_SEASON,
                trigger_type=FULL_REFRESH_TRIGGER,
            )
            return int(job.id), created

    def _job_state(self, job_id: int) -> tuple[str, str | None, dict[str, Any] | None]:
        with session_scope(self._session_factory) as session:
            job = IngestionJobRepository(session).get(job_id)
            if job is None:
                return STATUS_FAILED, "Задание исчезло из базы", None
            return str(job.status), job.error_message, job.result

    async def _run_import(self, step: FullRefreshStep) -> None:
        job_id, created = await asyncio.to_thread(self._enqueue, step)
        step.job_id = job_id
        if created:
            await asyncio.to_thread(self._spawn_worker, job_id)
        else:
            # Someone clicked the per-league button first: that job does the
            # same import, so wait for it instead of failing on the lock.
            step.detail = "Дождались уже запущенного обновления"
        deadline = asyncio.get_running_loop().time() + self._job_timeout
        while True:
            status, error, result = await asyncio.to_thread(self._job_state, job_id)
            if status not in ACTIVE_STATUSES:
                break
            if asyncio.get_running_loop().time() > deadline:
                raise FullRefreshError(
                    f"Задание #{job_id} не завершилось за {int(self._job_timeout)} с"
                )
            await asyncio.sleep(self._poll_interval)
        if status != STATUS_SUCCEEDED:
            raise FullRefreshError(error or f"Задание #{job_id} завершилось со статусом {status}")
        outcome = _import_detail(result)
        step.detail = ", ".join(part for part in (step.detail, outcome) if part) or None

    async def _run_odds(self, step: FullRefreshStep) -> None:
        report = await asyncio.to_thread(
            self._refresh_odds,
            self._session_factory,
            tournament_slug=step.tournament_slug,
        )
        step.detail = _odds_detail(report)


def _import_detail(result: dict[str, Any] | None) -> str | None:
    if not isinstance(result, dict):
        return None
    freshness = result.get("data_freshness")
    quality = result.get("quality") or {}
    passed = quality.get("passed") if isinstance(quality, dict) else None
    bits: list[str] = []
    if passed is True:
        bits.append("снапшот опубликован")
    elif passed is False:
        bits.append("снапшот не опубликован: контроль качества не пройден")
    if freshness:
        bits.append(f"данные от {freshness}")
    return ", ".join(bits) or None


def _odds_detail(report: dict[str, Any] | None) -> str | None:
    if not isinstance(report, dict):
        return None
    return (
        f"линий {report.get('fetched', 0)}, привязано {report.get('linked', 0)}, "
        f"прогнозов пересчитано {report.get('forecast_rows', 0)}"
    )


__all__ = [
    "DEFAULT_JOB_TIMEOUT",
    "DEFAULT_POLL_INTERVAL",
    "FULL_REFRESH_TRIGGER",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_RUNNING",
    "STATUS_SKIPPED",
    "STATUS_SUCCEEDED",
    "STEP_CURRENT_SEASON",
    "STEP_LATEST_COMPLETED",
    "STEP_ODDS",
    "FullRefreshBusy",
    "FullRefreshError",
    "FullRefreshManager",
    "FullRefreshRun",
    "FullRefreshStep",
    "plan_full_refresh",
]
