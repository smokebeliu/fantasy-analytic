// Pure helpers for the admin refresh screen (development-plan step 17).
//
// The backend owns the stage vocabulary (fantasy_analytics.ingestion_progress);
// this module only translates it for the UI and derives the small amount of
// state the panel needs — how far a job has come, whether it is still worth
// polling and how to phrase what happened. Keeping it free of React makes the
// polling and labelling rules directly testable.

import type { IngestionJob, IngestionStatusResponse } from "./types";

export const ACTIVE_JOB_STATUSES = ["pending", "running"] as const;

// Russian labels for the backend stage keys, in pipeline order.
export const STAGE_LABELS: Record<string, string> = {
  queued: "В очереди",
  starting: "Запуск",
  fetch_season: "Загрузка сезона",
  fetch_players: "Загрузка игроков",
  fetch_history: "Загрузка истории матчей",
  persist: "Запись в базу",
  quality_gate: "Контроль качества",
  forecast: "Расчёт прогноза",
  finished: "Готово",
};

export const JOB_STATUS_LABELS: Record<string, string> = {
  pending: "В очереди",
  running: "Выполняется",
  succeeded: "Успешно",
  failed: "Ошибка",
};

export function stageLabel(stage: string | null | undefined): string {
  if (!stage) return "—";
  return STAGE_LABELS[stage] ?? stage;
}

export function jobStatusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return JOB_STATUS_LABELS[status] ?? status;
}

export function isJobActive(job: IngestionJob | null | undefined): boolean {
  if (!job) return false;
  return (ACTIVE_JOB_STATUSES as readonly string[]).includes(job.status);
}

/** Poll only while a job is still running; a terminal job needs no requests. */
export function shouldPoll(status: IngestionStatusResponse | null): boolean {
  if (!status) return false;
  return status.is_refreshing || isJobActive(status.active_job);
}

/**
 * Completion percentage to render. A finished job is always 100% even if the
 * worker died before writing its last progress row; a job with no progress yet
 * shows the "queued" 0%.
 */
export function jobPercent(job: IngestionJob | null | undefined): number {
  if (!job) return 0;
  if (job.status === "succeeded") return 100;
  const percent = job.progress?.percent;
  if (percent === null || percent === undefined) return job.status === "failed" ? 100 : 0;
  return Math.max(0, Math.min(100, percent));
}

/** The job the panel should describe: an in-flight one, else the last one. */
export function currentJob(
  status: IngestionStatusResponse | null,
): IngestionJob | null {
  if (!status) return null;
  return status.active_job ?? status.latest_job ?? null;
}

/** Wall-clock duration of a job as a short "5 мин 12 с" string. */
export function formatDuration(
  fromIso: string | null | undefined,
  toIso: string | null | undefined,
  now: number = Date.now(),
): string {
  if (!fromIso) return "—";
  const from = new Date(fromIso).getTime();
  if (Number.isNaN(from)) return "—";
  const parsedTo = toIso ? new Date(toIso).getTime() : now;
  const to = Number.isNaN(parsedTo) ? now : parsedTo;
  const seconds = Math.max(0, Math.round((to - from) / 1000));
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return rest === 0 ? `${minutes} мин` : `${minutes} мин ${rest} с`;
}

/**
 * A user-facing sentence about a finished job. A successful job that did not
 * publish its snapshot is a distinct, important case: the import worked but the
 * quality gate refused to promote it, so the previous snapshot is still served.
 */
export function jobOutcome(job: IngestionJob | null | undefined): {
  kind: "none" | "running" | "published" | "blocked" | "failed";
  message: string;
} {
  if (!job) return { kind: "none", message: "Обновление ещё не запускалось." };
  if (isJobActive(job)) {
    return { kind: "running", message: "Обновление выполняется." };
  }
  if (job.status === "failed") {
    return {
      kind: "failed",
      message:
        job.error_message ??
        "Обновление не удалось. Попробуйте запустить его снова.",
    };
  }
  if (job.result?.snapshot_active) {
    return { kind: "published", message: "Снапшот обновлён и опубликован." };
  }
  return {
    kind: "blocked",
    message:
      "Импорт прошёл, но контроль качества не опубликовал снапшот — " +
      "используются данные предыдущего снапшота.",
  };
}

/** Headline counts of a finished import, ready to render as label/value pairs. */
export function importHighlights(
  job: IngestionJob | null | undefined,
): { label: string; value: number }[] {
  const counts = job?.result?.counts;
  if (!counts) return [];
  const wanted: [string, string][] = [
    ["players", "Игроки"],
    ["matches", "Матчи"],
    ["tours", "Туры"],
    ["player_match_stats", "Матч-статистика"],
  ];
  return wanted
    .filter(([key]) => typeof counts[key] === "number")
    .map(([key, label]) => ({ label, value: counts[key] as number }));
}
