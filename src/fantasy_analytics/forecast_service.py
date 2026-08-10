"""Materialise the forecasts a published snapshot is expected to have.

The forecast pipeline (step 7) writes ``player_forecasts`` rows, but until now
nothing called it automatically. A fresh import therefore published a snapshot
whose players had a price and a season score but *no* projection, and the
«Прогноз» column of the player table stayed empty until somebody remembered to
run ``fantasy-forecast`` by hand. The optimizer hid the problem because it
rebuilds the forecast in process instead of reading the table.

This module closes the gap from both ends:

* the ingestion worker calls :func:`ensure_tour_forecasts` right after the
  quality gate publishes, so a fresh snapshot arrives with projections stored;
* the read API calls it before answering a player query, so a snapshot imported
  by an older build heals itself the first time somebody looks at that tour.

Building one tour is cheap — a full RPL tour is three models over ~460 players
and takes well under a second — but it is still a write on a read path, so it
runs under a session-level advisory lock keyed by ``(run, tour)``. Two
concurrent readers can never build the same tour twice; the loser skips the
build and reads whatever the winner stored.

Failure is never fatal. A tour that cannot be forecast (no fixtures yet, an
incomplete snapshot) leaves the table as it was and the caller keeps serving
null projections, exactly as it did before.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Iterator

from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from .db import ForecastRepository, session_scope
from .db.models import FantasyTour, IngestionRun

ProgressCallback = Callable[[str], None]


def _noop(_message: str) -> None:
    return None


def forecast_lock_key(run_id: int, tour_id: int) -> int:
    """Return a stable signed 64-bit advisory-lock key for a run/tour pair.

    ``pg_advisory_lock`` takes a ``bigint``. Hashing the pair keeps the key
    deterministic across processes, so the API worker that serves a read and the
    ingestion worker that just published contend for the same lock instead of
    building the same tour twice.
    """
    payload = f"fantasy-forecast:{run_id}:{tour_id}".encode()
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big", signed=True)


@contextmanager
def _advisory_lock(session_factory: sessionmaker, key: int) -> Iterator[bool]:
    """Hold a session-level advisory lock, yielding whether it was acquired.

    The lock lives on its own connection so the forecast build underneath can
    open (and close) as many sessions as it likes. A session-level lock outlives
    the transaction, so it is always released explicitly before the connection
    goes back to the pool.
    """
    session = session_factory()
    acquired = False
    try:
        acquired = bool(
            session.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            ).scalar_one()
        )
        yield acquired
    finally:
        try:
            if acquired:
                session.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
                session.commit()
        finally:
            session.close()


def _resolve_tour_ref(session_factory: sessionmaker, run_id: int, tour_id: int) -> str | None:
    """Return the fantasy id of a tour that belongs to the run's season.

    ``run_forecast`` selects its target tour by fantasy id or name, while the
    API and the frontend speak internal ids. A tour from a different season than
    the run's is rejected here rather than silently forecasting the wrong tour.
    """
    with session_scope(session_factory) as session:
        run = session.get(IngestionRun, run_id)
        if run is None or run.season_id is None:
            return None
        tour = session.execute(
            select(FantasyTour).where(
                FantasyTour.id == tour_id,
                FantasyTour.season_id == run.season_id,
            )
        ).scalar_one_or_none()
        return tour.fantasy_tour_id if tour is not None else None


def has_tour_forecasts(session_factory: sessionmaker, *, run_id: int, tour_id: int) -> bool:
    """Whether any forecast row is already stored for the run and tour."""
    with session_scope(session_factory) as session:
        return ForecastRepository(session).has_forecasts(run_id=run_id, tour_id=tour_id)


def ensure_tour_forecasts(
    session_factory: sessionmaker,
    *,
    run_id: int | None,
    tour_id: int | None,
    now: datetime | None = None,
    on_progress: ProgressCallback = _noop,
) -> int:
    """Store the forecasts for one run/tour pair unless they already exist.

    Returns the number of rows written: ``0`` when the forecasts were already
    there, when the arguments do not identify a forecastable tour, or when the
    build failed. Never raises — a missing projection is a degraded read, not an
    error, and the caller keeps working with whatever the table holds.
    """
    if run_id is None or tour_id is None:
        return 0
    if has_tour_forecasts(session_factory, run_id=run_id, tour_id=tour_id):
        return 0

    tour_ref = _resolve_tour_ref(session_factory, run_id, tour_id)
    if tour_ref is None:
        return 0

    # Imported lazily: the forecast pipeline pulls in the whole feature builder,
    # which the read path should not pay for when nothing needs building.
    from .forecast import run_forecast

    with _advisory_lock(session_factory, forecast_lock_key(run_id, tour_id)) as acquired:
        if not acquired:
            # Another process is building the very same tour; its rows will be
            # visible to the next read.
            return 0
        if has_tour_forecasts(session_factory, run_id=run_id, tour_id=tour_id):
            return 0
        try:
            report = run_forecast(
                session_factory,
                run_id=run_id,
                tour_ref=tour_ref,
                now=now,
                persist=True,
            )
        except Exception as error:  # noqa: BLE001 - a failed build must not break the read
            on_progress(f"Forecast for tour {tour_ref} could not be built: {error}")
            return 0

    persisted = int(report.get("persisted") or 0)
    on_progress(f"Stored {persisted} forecast rows for tour {tour_ref}")
    return persisted


def ensure_next_tour_forecasts(
    session_factory: sessionmaker,
    *,
    run_id: int | None,
    now: datetime | None = None,
    on_progress: ProgressCallback = _noop,
) -> int:
    """Materialise forecasts for the run's next unfinished tour.

    This is the ingestion worker's entry point: right after publishing there is
    exactly one tour anybody is going to ask about — the next one to be played.
    A season whose tours are all finished has nothing to forecast and is skipped.
    """
    if run_id is None:
        return 0
    with session_scope(session_factory) as session:
        run = session.get(IngestionRun, run_id)
        if run is None or run.season_id is None:
            return 0
        tour = session.execute(
            select(FantasyTour)
            .where(
                FantasyTour.season_id == run.season_id,
                func.upper(func.coalesce(FantasyTour.status, "")) != "FINISHED",
            )
            .order_by(
                FantasyTour.starts_at.is_(None), FantasyTour.starts_at, FantasyTour.id
            )
            .limit(1)
        ).scalar_one_or_none()
        tour_id = tour.id if tour is not None else None
    return ensure_tour_forecasts(
        session_factory,
        run_id=run_id,
        tour_id=tour_id,
        now=now,
        on_progress=on_progress,
    )


__all__ = [
    "ensure_next_tour_forecasts",
    "ensure_tour_forecasts",
    "forecast_lock_key",
    "has_tour_forecasts",
]
