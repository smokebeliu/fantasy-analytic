"""Nightly refresh of every league that already has its active season imported.

Manual refresh (development-plan step 5) stays on demand. This module is the
other half of that contract: once an operator has imported a league's *current*
season, the API keeps that season fresh without anyone pressing the button.

Eligibility is read from PostgreSQL, not from Sports.ru:

* a row in ``seasons`` with ``is_active`` is the loaded current season;
* catalogued-but-never-imported leagues are ignored;
* a league whose only import is a finished historical season is ignored.

Each eligible league is enqueued as a regular :class:`IngestionJob` with
``trigger_type='scheduled'`` and ``use_current_season=True``, so the existing
worker, quality gate and per-tournament lock apply unchanged. Different leagues
still refresh in parallel; a league that already has a pending/running job, or
that already received a scheduled job today, is skipped.

The API process runs :func:`run_nightly_loop` from its lifespan: it sleeps until
the configured local hour (default 03:00 Europe/Moscow) and then sweeps. A
short catch-up window on startup covers a deploy that lands just after the
hour; after a sweep the loop always waits for tomorrow so it cannot spin.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .db import IngestionJobRepository, create_db_engine, create_session_factory, session_scope
from .db.models import Competition, IngestionJob, Season

logger = logging.getLogger(__name__)

SCHEDULED_TRIGGER = "scheduled"

# Russia no longer observes DST, so this is a safe fallback when the image has
# no tzdata and :class:`ZoneInfo` cannot load Europe/Moscow.
_MOSCOW_OFFSET = timezone(timedelta(hours=3))

DEFAULT_HOUR = 3
DEFAULT_MINUTE = 0
DEFAULT_TIMEZONE = "Europe/Moscow"
DEFAULT_CATCHUP_HOURS = 3

SKIP_ALREADY_RUNNING = "already_running"
SKIP_ALREADY_RAN_TODAY = "already_ran_today"

SpawnWorker = Callable[[int], None]
SleepFn = Callable[[float], Awaitable[None]]
NowFn = Callable[[], datetime]


def resolve_timezone(name: str) -> tzinfo:
    """Return an IANA zone, falling back to a fixed offset when tzdata is missing."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        if name in {"Europe/Moscow", "Europe/Kirov", "Europe/Volgograd"}:
            return _MOSCOW_OFFSET
        return timezone.utc


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int, *, minimum: int, maximum: int) -> int:
    if value is None or value.strip() == "":
        return default
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"Expected an integer in [{minimum}, {maximum}], got {parsed}")
    return parsed


@dataclass(frozen=True)
class NightlyRefreshSettings:
    """When and whether the in-process scheduler should fire."""

    enabled: bool = True
    hour: int = DEFAULT_HOUR
    minute: int = DEFAULT_MINUTE
    timezone_name: str = DEFAULT_TIMEZONE
    catchup_hours: int = DEFAULT_CATCHUP_HOURS

    @property
    def tz(self) -> tzinfo:
        return resolve_timezone(self.timezone_name)

    @classmethod
    def from_env(cls) -> "NightlyRefreshSettings":
        """Read scheduler settings from ``NIGHTLY_REFRESH_*`` environment variables."""
        return cls(
            enabled=_as_bool(os.environ.get("NIGHTLY_REFRESH_ENABLED"), True),
            hour=_as_int(
                os.environ.get("NIGHTLY_REFRESH_HOUR"),
                DEFAULT_HOUR,
                minimum=0,
                maximum=23,
            ),
            minute=_as_int(
                os.environ.get("NIGHTLY_REFRESH_MINUTE"),
                DEFAULT_MINUTE,
                minimum=0,
                maximum=59,
            ),
            timezone_name=(
                os.environ.get("NIGHTLY_REFRESH_TZ") or DEFAULT_TIMEZONE
            ).strip()
            or DEFAULT_TIMEZONE,
            catchup_hours=_as_int(
                os.environ.get("NIGHTLY_REFRESH_CATCHUP_HOURS"),
                DEFAULT_CATCHUP_HOURS,
                minimum=0,
                maximum=23,
            ),
        )


@dataclass(frozen=True)
class EligibleLeague:
    """One imported active season the nightly sweep would refresh."""

    tournament_slug: str
    season_id: int
    fantasy_season_id: str
    season_name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "tournament_slug": self.tournament_slug,
            "season_id": self.season_id,
            "fantasy_season_id": self.fantasy_season_id,
            "season_name": self.season_name,
        }


def start_of_local_day(now: datetime, tz: tzinfo) -> datetime:
    """Return midnight of ``now``'s local day, as an aware UTC timestamp."""
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def scheduled_time_on(local_day: datetime, settings: NightlyRefreshSettings) -> datetime:
    """The configured clock time on the local calendar day of ``local_day``."""
    local = local_day.astimezone(settings.tz)
    return local.replace(
        hour=settings.hour,
        minute=settings.minute,
        second=0,
        microsecond=0,
    )


def seconds_until_next_run(
    now: datetime,
    settings: NightlyRefreshSettings,
    *,
    allow_catchup: bool = False,
) -> float:
    """Seconds to sleep before the next nightly sweep.

    When ``allow_catchup`` is true and ``now`` is still inside the catch-up
    window after tonight's hour, the result is ``0`` so a process that started
    just after 03:00 still runs. After a sweep the caller must pass
    ``allow_catchup=False`` so the loop waits for tomorrow rather than spinning.
    """
    local = now.astimezone(settings.tz)
    today_run = scheduled_time_on(local, settings)
    if local < today_run:
        target = today_run
    elif allow_catchup and (local - today_run) <= timedelta(hours=settings.catchup_hours):
        return 0.0
    else:
        target = today_run + timedelta(days=1)
    return max(0.0, (target - local).total_seconds())


def next_run_at(
    now: datetime,
    settings: NightlyRefreshSettings,
    *,
    allow_catchup: bool = False,
) -> datetime:
    """Absolute timestamp of the next sweep (UTC)."""
    delay = seconds_until_next_run(now, settings, allow_catchup=allow_catchup)
    return now.astimezone(UTC) + timedelta(seconds=delay)


def list_imported_active_leagues(session: Session) -> list[EligibleLeague]:
    """Leagues whose current (``is_active``) season has already been imported.

    One row per competition: if a tournament somehow has two active fantasy
    seasons imported (Champions League phases), the newest one is reported but
    the sweep still enqueues a single ``use_current_season`` job for the slug.
    """
    rows = session.execute(
        select(Competition, Season)
        .join(Season, Season.competition_id == Competition.id)
        .where(Season.is_active.is_(True))
        .order_by(
            Competition.sort_order,
            Competition.slug,
            Season.starts_at.is_(None),
            Season.starts_at.desc(),
            Season.id.desc(),
        )
    ).all()
    seen: set[str] = set()
    eligible: list[EligibleLeague] = []
    for competition, season in rows:
        if competition.slug in seen:
            continue
        seen.add(competition.slug)
        eligible.append(
            EligibleLeague(
                tournament_slug=competition.slug,
                season_id=season.id,
                fantasy_season_id=season.fantasy_season_id,
                season_name=season.name,
            )
        )
    return eligible


def scheduled_jobs_since(
    session: Session,
    *,
    created_after: datetime,
    tournament_slug: str | None = None,
) -> list[IngestionJob]:
    """Scheduled jobs created at or after ``created_after``, newest first."""
    conditions = [
        IngestionJob.trigger_type == SCHEDULED_TRIGGER,
        IngestionJob.created_at >= created_after,
    ]
    if tournament_slug is not None:
        conditions.append(IngestionJob.tournament_slug == tournament_slug)
    return list(
        session.execute(
            select(IngestionJob)
            .where(*conditions)
            .order_by(IngestionJob.id.desc())
        ).scalars()
    )


def _job_payload(job: IngestionJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "tournament_slug": job.tournament_slug,
        "trigger_type": job.trigger_type,
        "use_current_season": job.use_current_season,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


def enqueue_nightly_jobs(
    session_factory: sessionmaker,
    spawn_worker: SpawnWorker,
    *,
    settings: NightlyRefreshSettings | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Enqueue a current-season refresh for every eligible league.

    ``force`` bypasses the once-a-day guard (used by the admin "run now"
    endpoint) but never starts a second concurrent job for the same league.
    """
    settings = settings or NightlyRefreshSettings()
    moment = now or datetime.now(UTC)
    day_start = start_of_local_day(moment, settings.tz)

    with session_scope(session_factory) as session:
        eligible = list_imported_active_leagues(session)

    enqueued: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for league in eligible:
        with session_scope(session_factory) as session:
            repo = IngestionJobRepository(session)
            if not force:
                already = scheduled_jobs_since(
                    session,
                    created_after=day_start,
                    tournament_slug=league.tournament_slug,
                )
                if already:
                    skipped.append(
                        {
                            "tournament_slug": league.tournament_slug,
                            "reason": SKIP_ALREADY_RAN_TODAY,
                        }
                    )
                    continue
            job, created = repo.enqueue(
                tournament_slug=league.tournament_slug,
                use_current_season=True,
                trigger_type=SCHEDULED_TRIGGER,
            )
            payload = _job_payload(job)

        if created:
            spawn_worker(payload["id"])
            enqueued.append(payload)
        else:
            skipped.append(
                {
                    "tournament_slug": league.tournament_slug,
                    "reason": SKIP_ALREADY_RUNNING,
                }
            )

    report = {
        "ran_at": moment.astimezone(UTC).isoformat(),
        "force": force,
        "eligible": [league.as_dict() for league in eligible],
        "enqueued": enqueued,
        "skipped": skipped,
    }
    logger.info(
        "Nightly refresh sweep: %s eligible, %s enqueued, %s skipped",
        len(eligible),
        len(enqueued),
        len(skipped),
    )
    return report


def scheduler_status(
    session_factory: sessionmaker,
    settings: NightlyRefreshSettings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Snapshot the scheduler can show without triggering a sweep."""
    moment = now or datetime.now(UTC)
    day_start = start_of_local_day(moment, settings.tz)
    with session_scope(session_factory) as session:
        eligible = [league.as_dict() for league in list_imported_active_leagues(session)]
        today = [_job_payload(job) for job in scheduled_jobs_since(session, created_after=day_start)]
    return {
        "enabled": settings.enabled,
        "timezone": settings.timezone_name,
        "hour": settings.hour,
        "minute": settings.minute,
        "catchup_hours": settings.catchup_hours,
        "now": moment.astimezone(UTC).isoformat(),
        "next_run_at": next_run_at(moment, settings, allow_catchup=True).isoformat(),
        "eligible": eligible,
        "scheduled_today": today,
    }


async def run_nightly_loop(
    enqueue_fn: Callable[[], Any],
    settings: NightlyRefreshSettings,
    *,
    sleep: SleepFn = asyncio.sleep,
    now_fn: NowFn | None = None,
) -> None:
    """Sleep until the configured hour, sweep, repeat.

    The first iteration may catch up if the process started shortly after the
    scheduled hour; every later iteration waits for the next calendar run.
    Errors from ``enqueue_fn`` are logged and do not stop the loop.
    """
    clock = now_fn or (lambda: datetime.now(UTC))
    first = True
    while True:
        delay = seconds_until_next_run(clock(), settings, allow_catchup=first)
        first = False
        if delay > 0:
            logger.info("Nightly refresh sleeping for %.0f seconds", delay)
            await sleep(delay)
        try:
            enqueue_fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Nightly refresh sweep failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fantasy-nightly-refresh",
        description=(
            "Enqueue a current-season refresh for every league that already "
            "has its active season imported."
        ),
    )
    parser.add_argument(
        "--database-url", help="Override DATABASE_URL for this process"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-queue even if a scheduled job already ran today",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List eligible leagues without enqueueing jobs",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """One-shot CLI so a cron job can drive the same sweep as the API loop."""
    parser = build_parser()
    args = parser.parse_args(argv)

    engine = create_db_engine(args.database_url)
    session_factory = create_session_factory(engine)
    settings = NightlyRefreshSettings.from_env()

    def spawn(job_id: int) -> None:
        from .api import default_spawn_worker

        default_spawn_worker(job_id, database_url=args.database_url)

    try:
        if args.dry_run:
            with session_scope(session_factory) as session:
                eligible = list_imported_active_leagues(session)
            print(
                {
                    "eligible": [league.as_dict() for league in eligible],
                }
            )
            return 0
        report = enqueue_nightly_jobs(
            session_factory,
            spawn,
            settings=settings,
            force=args.force,
        )
        print(report)
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    raise SystemExit(main())
