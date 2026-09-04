"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { api, ApiError } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import type {
  FullRefreshStatus,
  FullRefreshStep,
  FullRefreshStepKind,
  FullRefreshStepStatus,
} from "@/lib/types";

// The run is a chain of season imports (a minute or two each) and odds
// fetches, so a few seconds between polls is responsive enough; polling stops
// as soon as the run is over.
const POLL_INTERVAL_MS = 2500;

export const STEP_KIND_LABELS: Record<FullRefreshStepKind, string> = {
  latest_completed: "Последний завершённый сезон",
  current_season: "Текущий сезон",
  odds: "Котировки",
};

export const STEP_STATUS_LABELS: Record<FullRefreshStepStatus, string> = {
  pending: "В очереди",
  running: "Выполняется",
  succeeded: "Готово",
  failed: "Ошибка",
  skipped: "Пропущено",
};

/** One line per step: league, what, state, and the reason when there is one. */
export function stepSummary(step: FullRefreshStep): string {
  const note = step.error ?? step.detail;
  return `${step.competition_name ?? step.tournament_slug} — ${
    STEP_KIND_LABELS[step.kind] ?? step.kind
  }: ${STEP_STATUS_LABELS[step.status] ?? step.status}${note ? ` (${note})` : ""}`;
}

export function FullRefreshPanel({
  initialStatus = null,
}: {
  initialStatus?: FullRefreshStatus | null;
}) {
  const router = useRouter();
  const [status, setStatus] = useState<FullRefreshStatus | null>(initialStatus);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // React exactly once to the run finishing: the snapshots changed, so the
  // server-rendered parts of the page (freshness, per-league panel) reload.
  const wasRunning = useRef(Boolean(initialStatus?.is_running));

  const applyStatus = useCallback(
    (next: FullRefreshStatus) => {
      setStatus(next);
      if (wasRunning.current && !next.is_running) router.refresh();
      wasRunning.current = next.is_running;
    },
    [router],
  );

  const reload = useCallback(async () => {
    try {
      applyStatus(await api.getFullRefresh());
    } catch (err: unknown) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Не удалось получить состояние полного обновления.",
      );
    }
  }, [applyStatus]);

  const reloadRef = useRef(reload);
  useEffect(() => {
    reloadRef.current = reload;
  }, [reload]);

  // A page opened while a run is in flight (or a prefetched render) has to
  // catch up before it starts polling.
  useEffect(() => {
    void reloadRef.current();
  }, []);

  const running = Boolean(status?.is_running);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void reloadRef.current(), POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [running]);

  const start = useCallback(async () => {
    setStarting(true);
    setError(null);
    try {
      const next = await api.startFullRefresh();
      wasRunning.current = next.is_running;
      setStatus(next);
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 409) {
        setError("Полное обновление уже выполняется.");
        await reload();
      } else {
        setError(
          err instanceof ApiError
            ? err.message
            : "Не удалось запустить полное обновление.",
        );
      }
    } finally {
      setStarting(false);
    }
  }, [reload]);

  const run = status?.run ?? null;
  const current = run?.current_step ?? null;
  const percent =
    run && run.total_steps > 0
      ? Math.round((run.completed_steps / run.total_steps) * 100)
      : 0;

  return (
    <section
      className="panel panel--pad full-refresh"
      data-testid="full-refresh-panel"
    >
      <div className="refresh-action__head">
        <h2>Обновить всё</h2>
        {run && (
          <span
            className={`chip chip--${
              run.status === "failed"
                ? "failed"
                : run.status === "succeeded"
                  ? "succeeded"
                  : "running"
            }`}
            data-testid="full-refresh-state"
          >
            {run.status === "running"
              ? "Выполняется"
              : run.status === "succeeded"
                ? "Готово"
                : "Завершено с ошибками"}
          </span>
        )}
      </div>
      <p className="inline-note">
        Для каждой импортированной лиги по очереди: последний завершённый
        сезон, затем текущий, затем котировки с пересчётом прогноза. Один клик
        вместо обхода всех лиг вручную; задания видны и в панели лиги ниже.
      </p>
      <button
        className="btn btn--primary"
        data-testid="full-refresh-button"
        disabled={running || starting}
        onClick={() => void start()}
      >
        {running ? "Обновление идёт…" : "Обновить все лиги и котировки"}
      </button>
      {error && (
        <div className="error-inline" role="alert" data-testid="full-refresh-error">
          {error}
        </div>
      )}

      {run && (
        <div data-testid="full-refresh-run">
          <div
            className="progress"
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            aria-label="Прогресс полного обновления"
          >
            <div
              className={`progress__bar${
                run.status === "failed" ? " progress__bar--failed" : ""
              }`}
              style={{ width: `${percent}%` }}
            />
          </div>
          <p className="refresh-stage" data-testid="full-refresh-progress">
            <strong>
              {run.completed_steps}/{run.total_steps} шагов
            </strong>
            {current && (
              <span className="refresh-stage__note">
                {" "}
                · сейчас: {current.competition_name ?? current.tournament_slug} —{" "}
                {STEP_KIND_LABELS[current.kind] ?? current.kind}
              </span>
            )}
            {run.finished_at && (
              <span className="refresh-stage__note">
                {" "}
                · завершено {formatDateTime(run.finished_at)}
              </span>
            )}
          </p>
          <ol className="stage-list" data-testid="full-refresh-steps">
            {run.steps.map((step, index) => (
              <li
                key={`${step.tournament_slug}-${step.kind}-${index}`}
                className={`stage-list__item${
                  step.status === "succeeded" || step.status === "skipped"
                    ? " is-done"
                    : ""
                }${step.status === "running" ? " is-current" : ""}${
                  step.status === "failed" ? " is-failed" : ""
                }`}
              >
                {stepSummary(step)}
              </li>
            ))}
          </ol>
        </div>
      )}
    </section>
  );
}
