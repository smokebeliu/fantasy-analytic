"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { formatDateTime, relativeFreshness } from "@/lib/format";
import {
  currentJob,
  formatDuration,
  importHighlights,
  isJobActive,
  jobOutcome,
  jobPercent,
  jobStatusLabel,
  shouldPoll,
  stageLabel,
} from "@/lib/ingestion";
import type {
  CompetitionModel,
  IngestionStatusResponse,
  RefreshRequestBody,
} from "@/lib/types";
import { ErrorState } from "./StateBlocks";

// How often the panel asks the backend for the job state. A full season import
// takes ~40-90 s depending on the league's size, so a few seconds is responsive
// without being chatty — and polling stops entirely once the job is terminal.
const POLL_INTERVAL_MS = 2500;

// The two dynamic season selectors resolve at import time (which season is the
// latest completed one changes as a season ends); anything else is an explicit
// fantasy season id from the league's catalogue.
const LATEST_COMPLETED = "latest";
const ACTIVE_SEASON = "current";

/** Translate the season selector into the refresh request body. */
export function refreshBodyFor(choice: string): RefreshRequestBody {
  if (choice === ACTIVE_SEASON) return { current: true };
  if (choice === LATEST_COMPLETED) return {};
  return { season_id: choice };
}

export function RefreshPanel({
  initialStatus,
  initialError,
  competitions = [],
  initialSlug,
}: {
  initialStatus: IngestionStatusResponse | null;
  initialError?: string | null;
  /** The whole catalogue: a league can be imported before it has any data. */
  competitions?: CompetitionModel[];
  initialSlug?: string;
}) {
  const router = useRouter();
  const [status, setStatus] = useState<IngestionStatusResponse | null>(
    initialStatus,
  );
  const [loadError, setLoadError] = useState<string | null>(
    initialError ?? null,
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [refreshingOdds, setRefreshingOdds] = useState(false);
  const [slug, setSlug] = useState<string>(
    initialSlug ?? initialStatus?.tournament_slug ?? competitions[0]?.slug ?? "",
  );
  const [season, setSeason] = useState<string>(LATEST_COMPLETED);
  const [now, setNow] = useState(() => Date.now());

  const competition =
    competitions.find((item) => item.slug === slug) ??
    status?.competition ??
    null;

  // Remembering the job the panel last saw lets it react exactly once to a
  // refresh finishing, instead of re-rendering the server tree on every poll.
  const seenJob = useRef(currentJob(initialStatus));

  const applyStatus = useCallback(
    (next: IngestionStatusResponse) => {
      setStatus(next);
      setLoadError(null);
      const job = currentJob(next);
      const finished =
        isJobActive(seenJob.current) && job !== null && !isJobActive(job);
      seenJob.current = job;
      // A refresh that just finished changes the published snapshot, so the
      // server-rendered header (data freshness) has to be re-fetched.
      if (finished) router.refresh();
    },
    [router],
  );

  const reload = useCallback(async () => {
    if (!slug) return;
    try {
      applyStatus(await api.getIngestionStatus(slug));
    } catch (error: unknown) {
      setLoadError(
        error instanceof ApiError
          ? error.message
          : "Не удалось получить состояние обновления.",
      );
    }
  }, [applyStatus, slug]);

  // The timer must not be torn down and rebuilt whenever an unrelated render
  // produces a new `reload` identity, so it is reached through a ref.
  const reloadRef = useRef(reload);
  useEffect(() => {
    reloadRef.current = reload;
  }, [reload]);

  // The server render is a snapshot of the moment the page was requested, and a
  // client-side navigation can reuse a prefetched payload, so the state of an
  // in-flight refresh is revalidated once on mount before any polling starts.
  // Switching leagues re-runs this: each league has its own job history.
  useEffect(() => {
    void reloadRef.current();
  }, [slug]);

  // A season chosen for one league means nothing for another, so the selector
  // returns to the default whenever the league changes.
  useEffect(() => {
    setSeason(LATEST_COMPLETED);
  }, [slug]);

  const polling = shouldPoll(status);

  // Poll only while a job is in flight (criterion: no polling once finished).
  useEffect(() => {
    if (!polling) return;
    const timer = setInterval(() => {
      setNow(Date.now());
      void reloadRef.current();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [polling]);

  const refreshing = polling || starting;

  const syncCatalogue = useCallback(async () => {
    setSyncing(true);
    setActionError(null);
    try {
      await api.syncCompetitions();
      // The league list is server-rendered, so it needs a fresh server tree.
      router.refresh();
      await reload();
    } catch (error: unknown) {
      setActionError(
        error instanceof ApiError
          ? error.message
          : "Не удалось обновить список лиг.",
      );
    } finally {
      setSyncing(false);
    }
  }, [reload, router]);

  const refreshOdds = useCallback(async () => {
    if (!slug) return;
    setRefreshingOdds(true);
    setActionError(null);
    try {
      await api.refreshOdds(slug);
      await reload();
    } catch (error: unknown) {
      setActionError(
        error instanceof ApiError
          ? error.message
          : "Не удалось обновить котировки.",
      );
    } finally {
      setRefreshingOdds(false);
    }
  }, [reload, slug]);

  const start = useCallback(async () => {
    setStarting(true);
    setActionError(null);
    try {
      const job = await api.refreshIngestion(slug, refreshBodyFor(season));
      // Show the queued job immediately; the poller takes over from here.
      setStatus((prev) =>
        prev
          ? { ...prev, is_refreshing: true, active_job: job, latest_job: job }
          : prev,
      );
      seenJob.current = job;
      setNow(Date.now());
      await reload();
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 409) {
        setActionError(
          "Обновление уже выполняется — дождитесь его завершения.",
        );
        await reload();
      } else {
        setActionError(
          error instanceof ApiError
            ? error.message
            : "Не удалось запустить обновление.",
        );
      }
    } finally {
      setStarting(false);
    }
  }, [season, slug, reload]);

  if (loadError && !status) {
    return <ErrorState message={loadError} onRetry={() => void reload()} />;
  }

  const job = currentJob(status);
  const successful = status?.latest_successful_job ?? null;
  const outcome = jobOutcome(job);
  const percent = jobPercent(job);
  const highlights = importHighlights(successful);
  const stages = status?.stages ?? [];
  const activeStage = job?.progress?.stage ?? (refreshing ? "queued" : null);

  return (
    <div className="refresh-grid" data-testid="refresh-panel">
      <section className="panel panel--pad refresh-action">
        <div className="refresh-action__head">
          <h2>Обновить данные</h2>
          <span
            className={`chip chip--${outcome.kind}`}
            data-testid="refresh-state"
          >
            {refreshing ? "Выполняется" : jobStatusLabel(job?.status)}
          </span>
        </div>

        <div className="field">
          <label htmlFor="refresh-league">Лига</label>
          <select
            id="refresh-league"
            data-testid="refresh-league"
            value={slug}
            disabled={refreshing || competitions.length === 0}
            onChange={(event) => setSlug(event.target.value)}
          >
            {competitions.length === 0 && <option value={slug}>{slug}</option>}
            {competitions.map((item) => (
              <option key={item.slug} value={item.slug}>
                {item.name}
                {item.is_imported ? "" : " — ещё не импортирована"}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label htmlFor="refresh-season">Что обновлять</label>
          <select
            id="refresh-season"
            data-testid="refresh-season"
            value={season}
            disabled={refreshing}
            onChange={(event) => setSeason(event.target.value)}
          >
            <option value={LATEST_COMPLETED}>
              Последний завершённый сезон
            </option>
            <option
              value={ACTIVE_SEASON}
              disabled={competition ? !competition.has_active_season : false}
            >
              Активный сезон
              {competition && !competition.has_active_season
                ? " — нет в этой лиге"
                : ""}
            </option>
            {(competition?.available_seasons ?? [])
              .slice()
              .reverse()
              .map((item) => (
                <option
                  key={item.fantasy_season_id}
                  value={item.fantasy_season_id}
                >
                  Сезон {item.label}
                  {item.is_active ? " (идёт)" : ""}
                </option>
              ))}
          </select>
        </div>
        <p className="inline-note" data-testid="refresh-season-hint">
          {season === LATEST_COMPLETED
            ? "Обновляет исторические данные (по умолчанию)."
            : season === ACTIVE_SEASON
              ? "Обновляет текущий турнир: составы, цены и календарь."
              : "Импортирует выбранный сезон этой лиги."}
        </p>

        <button
          className="btn btn--primary"
          data-testid="refresh-button"
          disabled={refreshing || !slug}
          onClick={() => void start()}
        >
          {refreshing ? "Обновление идёт…" : "Запустить обновление"}
        </button>
        <p className="inline-note">
          Импорт и контроль качества выполняются в отдельном процессе. Снапшот
          публикуется только после успешной проверки, поэтому неудачное
          обновление не портит текущие данные. Блокировка действует на одну лигу,
          поэтому разные лиги можно обновлять параллельно.
        </p>

        <button
          className="btn btn--sm"
          data-testid="sync-competitions"
          disabled={syncing}
          onClick={() => void syncCatalogue()}
        >
          {syncing ? "Обновляем список лиг…" : "Обновить список лиг"}
        </button>
        <p className="inline-note">
          Перечитывает у Sports.ru список доступных лиг и их сезонов
          {competition?.catalogue_synced_at
            ? `. Последняя синхронизация: ${formatDateTime(
                competition.catalogue_synced_at,
              )}`
            : ""}
          .
        </p>

        <button
          className="btn btn--sm"
          data-testid="refresh-odds"
          disabled={refreshingOdds || !slug}
          onClick={() => void refreshOdds()}
        >
          {refreshingOdds ? "Загружаем котировки…" : "Обновить котировки"}
        </button>
        <p className="inline-note" data-testid="refresh-odds-hint">
          Загружает 1x2 по ближайшим матчам этой лиги и пересчитывает ожидаемые
          очки: фаворит против слабой обороны даёт апсайд атаки, крепкая оборона
          против слабой атаки — апсайд «сухаря»
          {status?.odds?.synced_at
            ? `. Последнее обновление: ${formatDateTime(status.odds.synced_at)}${
                status.odds.matches
                  ? ` (${status.odds.matches} матч.)`
                  : ""
              }`
            : ""}
          .
        </p>

        {actionError && (
          <div className="error-inline" role="alert" data-testid="refresh-action-error">
            {actionError}
            <button
              className="btn btn--sm"
              style={{ marginLeft: 10 }}
              disabled={refreshing}
              onClick={() => void start()}
            >
              Повторить
            </button>
          </div>
        )}
        {loadError && (
          <div className="inline-note" data-testid="refresh-load-error">
            {loadError}
          </div>
        )}
      </section>

      <section className="panel panel--pad refresh-progress" data-testid="refresh-progress">
        <h2>Текущее задание</h2>
        {!job && (
          <p className="inline-note">Обновление ещё не запускалось.</p>
        )}
        {job && (
          <>
            <div className="refresh-meta">
              <div className="stat-box">
                <span className="k">Задание</span>
                <span className="v">#{job.id}</span>
              </div>
              <div className="stat-box">
                <span className="k">Статус</span>
                <span className="v" data-testid="refresh-job-status">
                  {jobStatusLabel(job.status)}
                </span>
              </div>
              <div className="stat-box">
                <span className="k">Запущено</span>
                <span className="v">
                  {formatDateTime(job.started_at ?? job.created_at)}
                </span>
              </div>
              <div className="stat-box">
                <span className="k">Длительность</span>
                <span className="v" data-testid="refresh-duration">
                  {formatDuration(
                    job.started_at ?? job.created_at,
                    job.finished_at,
                    now,
                  )}
                </span>
              </div>
            </div>

            <div
              className="progress"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
              aria-label="Прогресс обновления"
            >
              <div
                className={`progress__bar${
                  job.status === "failed" ? " progress__bar--failed" : ""
                }`}
                style={{ width: `${percent}%` }}
              />
            </div>
            <p className="refresh-stage" data-testid="refresh-stage">
              <strong>{stageLabel(activeStage)}</strong> · {percent}%
              {job.progress?.message && (
                <span className="refresh-stage__note"> {job.progress.message}</span>
              )}
            </p>

            {stages.length > 0 && (
              <ol className="stage-list" data-testid="refresh-stages">
                {stages.map((entry) => {
                  const reached = percent >= entry.percent;
                  const current = entry.stage === activeStage;
                  return (
                    <li
                      key={entry.stage}
                      className={`stage-list__item${reached ? " is-done" : ""}${
                        current ? " is-current" : ""
                      }`}
                    >
                      {stageLabel(entry.stage)}
                    </li>
                  );
                })}
              </ol>
            )}

            <div
              className={
                outcome.kind === "failed" ? "error-inline" : "inline-note"
              }
              data-testid="refresh-outcome"
              role={outcome.kind === "failed" ? "alert" : undefined}
            >
              {outcome.message}
            </div>
            {outcome.kind === "failed" && (
              <button
                className="btn btn--sm"
                data-testid="refresh-retry"
                disabled={refreshing}
                onClick={() => void start()}
              >
                Повторить обновление
              </button>
            )}
          </>
        )}
      </section>

      <section className="panel panel--pad refresh-snapshot" data-testid="refresh-snapshot">
        <h2>Опубликованный снапшот</h2>
        {!status?.snapshot && (
          <p className="inline-note">
            Активного снапшота нет. Запустите обновление, чтобы импортировать
            сезон и опубликовать данные.
          </p>
        )}
        {status?.snapshot && (
          <div className="refresh-meta">
            <div className="stat-box">
              <span className="k">Снапшот</span>
              <span className="v">#{status.snapshot.run_id}</span>
            </div>
            <div className="stat-box">
              <span className="k">Лига</span>
              <span className="v" data-testid="refresh-snapshot-league">
                {status.competition?.name ?? status.tournament_slug}
              </span>
            </div>
            <div className="stat-box">
              <span className="k">Сезон</span>
              <span className="v">
                {status.season?.label ?? status.season?.name ?? "—"}
              </span>
            </div>
            <div className="stat-box">
              <span className="k">Данные от</span>
              <span className="v" data-testid="refresh-freshness">
                {formatDateTime(status.snapshot.data_freshness)}
              </span>
            </div>
            <div className="stat-box">
              <span className="k">Актуальность</span>
              <span className="v">
                {relativeFreshness(status.snapshot.data_freshness)}
              </span>
            </div>
          </div>
        )}

        <h3 className="section-title">Целевой тур</h3>
        {status?.target_tour ? (
          <div className="refresh-meta" data-testid="refresh-target-tour">
            <div className="stat-box">
              <span className="k">Тур</span>
              <span className="v">{status.target_tour.name}</span>
            </div>
            <div className="stat-box">
              <span className="k">Статус</span>
              <span className="v">{status.target_tour.status}</span>
            </div>
            <div className="stat-box">
              <span className="k">Дедлайн трансферов</span>
              <span className="v">
                {formatDateTime(status.target_tour.transfers_deadline_at)}
              </span>
            </div>
          </div>
        ) : (
          <p className="inline-note">Туры ещё не импортированы.</p>
        )}

        {highlights.length > 0 && (
          <>
            <h3 className="section-title">Последний успешный импорт</h3>
            <div className="refresh-meta" data-testid="refresh-highlights">
              {highlights.map((item) => (
                <div className="stat-box" key={item.label}>
                  <span className="k">{item.label}</span>
                  <span className="v">{item.value}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </section>
    </div>
  );
}
