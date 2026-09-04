import { describe, expect, it } from "vitest";
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
import type { IngestionJob, IngestionStatusResponse } from "@/lib/types";

function job(overrides: Partial<IngestionJob> = {}): IngestionJob {
  return {
    id: 1,
    status: "running",
    tournament_slug: "russia",
    created_at: "2026-08-09T10:00:00Z",
    started_at: "2026-08-09T10:00:05Z",
    progress: { stage: "fetch_players", percent: 20, message: "Fetched player page 3" },
    ...overrides,
  };
}

function status(overrides: Partial<IngestionStatusResponse> = {}): IngestionStatusResponse {
  return {
    tournament_slug: "russia",
    is_refreshing: false,
    stages: [
      { stage: "queued", percent: 0 },
      { stage: "finished", percent: 100 },
    ],
    ...overrides,
  };
}

describe("stage and status labels", () => {
  it("translates known stages and falls back to the raw key", () => {
    expect(stageLabel("fetch_history")).toBe("Загрузка истории матчей");
    expect(stageLabel("unknown_stage")).toBe("unknown_stage");
    expect(stageLabel(null)).toBe("—");
  });

  it("translates job statuses", () => {
    expect(jobStatusLabel("succeeded")).toBe("Успешно");
    expect(jobStatusLabel("failed")).toBe("Ошибка");
    expect(jobStatusLabel(undefined)).toBe("—");
  });
});

describe("polling decisions", () => {
  it("polls while a job is pending or running", () => {
    expect(isJobActive(job({ status: "pending" }))).toBe(true);
    expect(isJobActive(job({ status: "running" }))).toBe(true);
    expect(shouldPoll(status({ is_refreshing: true, active_job: job() }))).toBe(true);
  });

  it("stops polling once the job reaches a terminal status", () => {
    const finished = job({ status: "succeeded", finished_at: "2026-08-09T10:01:00Z" });
    expect(isJobActive(finished)).toBe(false);
    expect(shouldPoll(status({ latest_job: finished }))).toBe(false);
    expect(shouldPoll(null)).toBe(false);
  });
});

describe("progress", () => {
  it("reads the percentage the worker recorded", () => {
    expect(jobPercent(job({ progress: { stage: "persist", percent: 75 } }))).toBe(75);
  });

  it("treats a finished job as complete even without a progress row", () => {
    expect(jobPercent(job({ status: "succeeded", progress: null }))).toBe(100);
    expect(jobPercent(job({ status: "failed", progress: null }))).toBe(100);
  });

  it("clamps out-of-range values and handles a missing job", () => {
    expect(jobPercent(job({ progress: { stage: "x", percent: 140 } }))).toBe(100);
    expect(jobPercent(job({ progress: { stage: "x", percent: -5 } }))).toBe(0);
    expect(jobPercent(null)).toBe(0);
  });
});

describe("currentJob", () => {
  it("prefers the active job over the latest one", () => {
    const active = job({ id: 7 });
    const latest = job({ id: 6, status: "succeeded" });
    expect(currentJob(status({ active_job: active, latest_job: latest }))?.id).toBe(7);
    expect(currentJob(status({ latest_job: latest }))?.id).toBe(6);
    expect(currentJob(status())).toBeNull();
  });
});

describe("formatDuration", () => {
  it("measures a finished job between its own timestamps", () => {
    expect(
      formatDuration("2026-08-09T10:00:00Z", "2026-08-09T10:00:45Z"),
    ).toBe("45 с");
    expect(
      formatDuration("2026-08-09T10:00:00Z", "2026-08-09T10:02:30Z"),
    ).toBe("2 мин 30 с");
    expect(
      formatDuration("2026-08-09T10:00:00Z", "2026-08-09T10:03:00Z"),
    ).toBe("3 мин");
  });

  it("measures a running job against the current time", () => {
    const now = new Date("2026-08-09T10:00:20Z").getTime();
    expect(formatDuration("2026-08-09T10:00:00Z", null, now)).toBe("20 с");
    expect(formatDuration(null, null, now)).toBe("—");
  });
});

describe("jobOutcome", () => {
  it("distinguishes a published snapshot from a blocked one", () => {
    const published = job({
      status: "succeeded",
      result: { snapshot_active: true, data_freshness: "2026-08-09T10:01:00Z" },
    });
    expect(jobOutcome(published).kind).toBe("published");

    const blocked = job({ status: "succeeded", result: { snapshot_active: false } });
    expect(jobOutcome(blocked).kind).toBe("blocked");
    expect(jobOutcome(blocked).message).toContain("контроль качества");
  });

  it("surfaces the safe error message of a failed job", () => {
    const failed = job({ status: "failed", error_message: "RuntimeError: boom" });
    expect(jobOutcome(failed)).toEqual({
      kind: "failed",
      message: "RuntimeError: boom",
    });
  });

  it("reports running and never-run states", () => {
    expect(jobOutcome(job()).kind).toBe("running");
    expect(jobOutcome(null).kind).toBe("none");
  });
});

describe("importHighlights", () => {
  it("keeps only the known headline counts", () => {
    const finished = job({
      status: "succeeded",
      result: {
        snapshot_active: true,
        counts: { players: 590, matches: 240, tours: 30, raw_responses: 432 },
      },
    });
    expect(importHighlights(finished)).toEqual([
      { label: "Игроки", value: 590 },
      { label: "Матчи", value: 240 },
      { label: "Туры", value: 30 },
    ]);
    expect(importHighlights(null)).toEqual([]);
  });
});
