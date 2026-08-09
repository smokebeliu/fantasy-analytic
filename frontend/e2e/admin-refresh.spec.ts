import { expect, test, type Page, type Route } from "@playwright/test";
import type { IngestionJob, IngestionStatusResponse } from "../src/lib/types";

// End-to-end coverage of the manual refresh screen (development-plan step 17).
//
// The success/failure job flows are driven through intercepted browser calls, so
// the whole state machine (queued -> running -> terminal, polling, blocked
// button, retry) is exercised deterministically without importing a real season.
// The initial page render is server-side and therefore still reads the live
// backend, which is what keeps the snapshot/target-tour panel honest.
//
// The last test runs a *real* refresh against the live Sports.ru API (a full RPL
// season import, ~45 s) and is opt-in via RUN_LIVE_REFRESH_E2E=1 because it needs
// outbound network and writes a new snapshot.

const ADMIN_ROUTE = "**/api/backend/admin/ingestion/**";

const STAGES = [
  { stage: "queued", percent: 0 },
  { stage: "starting", percent: 5 },
  { stage: "fetch_season", percent: 10 },
  { stage: "fetch_players", percent: 20 },
  { stage: "fetch_history", percent: 45 },
  { stage: "persist", percent: 75 },
  { stage: "quality_gate", percent: 90 },
  { stage: "finished", percent: 100 },
];

function job(overrides: Partial<IngestionJob>): IngestionJob {
  return {
    id: 4242,
    status: "pending",
    tournament_slug: "russia",
    created_at: "2026-08-09T10:00:00Z",
    ...overrides,
  };
}

function statusPayload(
  overrides: Partial<IngestionStatusResponse>,
): IngestionStatusResponse {
  return {
    tournament_slug: "russia",
    is_refreshing: false,
    stages: STAGES,
    snapshot: {
      run_id: 1,
      season_id: 1,
      data_freshness: "2026-08-01T12:00:00Z",
    },
    season: {
      season_id: 1,
      fantasy_season_id: "59",
      stat_season_id: "rfpl_25-26",
      name: "2025/2026",
      is_active: false,
    },
    target_tour: {
      tour_id: 30,
      season_id: 1,
      fantasy_tour_id: "1801",
      name: "30 тур",
      status: "FINISHED",
      transfers_deadline_at: "2026-05-17T15:00:00Z",
    },
    ...overrides,
  };
}

/**
 * Serve a scripted sequence of statuses to the panel's polling requests and
 * count how many it made, so a test can assert that polling stopped.
 */
async function mockJobFlow(
  page: Page,
  {
    queued,
    sequence,
    refreshStatus = 202,
    refreshBody,
  }: {
    queued: IngestionJob;
    sequence: IngestionStatusResponse[];
    refreshStatus?: number;
    refreshBody?: unknown;
  },
): Promise<{ statusCalls: () => number }> {
  let index = 0;
  let calls = 0;

  await page.route(ADMIN_ROUTE, async (route: Route) => {
    const request = route.request();
    if (request.method() === "POST") {
      await route.fulfill({
        status: refreshStatus,
        contentType: "application/json",
        body: JSON.stringify(refreshBody ?? queued),
      });
      return;
    }
    calls += 1;
    const payload = sequence[Math.min(index, sequence.length - 1)];
    index += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    });
  });

  return { statusCalls: () => calls };
}

test.describe("Manual refresh screen", () => {
  test("shows the published snapshot and the target tour", async ({ page }) => {
    await page.goto("/admin");
    await expect(
      page.getByRole("heading", { name: "Обновление данных" }),
    ).toBeVisible();
    // Served from the live backend, so only structure is asserted.
    await expect(page.getByTestId("refresh-snapshot")).toContainText("Снапшот");
    await expect(page.getByTestId("refresh-button")).toBeEnabled();
  });

  test("runs a job to success, then stops polling", async ({ page }) => {
    const running = job({
      status: "running",
      started_at: "2026-08-09T10:00:02Z",
      progress: {
        stage: "fetch_history",
        percent: 45,
        message: "Fetching match history for 423 players with 8 workers",
      },
    });
    const succeeded = job({
      status: "succeeded",
      started_at: "2026-08-09T10:00:02Z",
      finished_at: "2026-08-09T10:00:47Z",
      data_freshness: "2026-08-09T10:00:46Z",
      progress: { stage: "finished", percent: 100, message: "Job 4242 succeeded" },
      result: {
        snapshot_active: true,
        data_freshness: "2026-08-09T10:00:46Z",
        counts: { players: 590, matches: 240, tours: 30 },
      },
    });

    await page.goto("/admin");
    const flow = await mockJobFlow(page, {
      queued: job({ status: "pending" }),
      sequence: [
        statusPayload({ is_refreshing: true, active_job: running }),
        statusPayload({ is_refreshing: true, active_job: running }),
        statusPayload({
          is_refreshing: false,
          latest_job: succeeded,
          latest_successful_job: succeeded,
          snapshot: {
            run_id: 2,
            season_id: 1,
            data_freshness: "2026-08-09T10:00:46Z",
          },
        }),
      ],
    });

    await page.getByTestId("refresh-button").click();

    // While it runs: the stage is named and the button cannot start a second job.
    await expect(page.getByTestId("refresh-stage")).toContainText(
      "Загрузка истории матчей",
    );
    await expect(page.getByTestId("refresh-button")).toBeDisabled();

    // On success: the new snapshot and the import headline counts are shown.
    await expect(page.getByTestId("refresh-job-status")).toHaveText("Успешно");
    await expect(page.getByTestId("refresh-outcome")).toContainText(
      "Снапшот обновлён и опубликован",
    );
    await expect(page.getByTestId("refresh-highlights")).toContainText("590");
    await expect(page.getByTestId("refresh-button")).toBeEnabled();

    const settled = flow.statusCalls();
    await page.waitForTimeout(4000);
    expect(flow.statusCalls()).toBe(settled);
  });

  test("shows a safe error and retries after a failed job", async ({ page }) => {
    const failed = job({
      status: "failed",
      started_at: "2026-08-09T10:00:02Z",
      finished_at: "2026-08-09T10:00:12Z",
      error_message: "RuntimeError: simulated history failure",
      progress: { stage: "fetch_history", percent: 45 },
    });

    await page.goto("/admin");
    await mockJobFlow(page, {
      queued: job({ status: "pending" }),
      sequence: [
        statusPayload({
          is_refreshing: true,
          active_job: job({ status: "running", progress: { stage: "starting", percent: 5 } }),
        }),
        statusPayload({ is_refreshing: false, latest_job: failed }),
      ],
    });

    await page.getByTestId("refresh-button").click();

    await expect(page.getByTestId("refresh-outcome")).toContainText(
      "simulated history failure",
    );
    // The failure is recoverable: retry is offered and the button is free again.
    await expect(page.getByTestId("refresh-retry")).toBeEnabled();
    await expect(page.getByTestId("refresh-button")).toBeEnabled();
  });

  test("refuses a parallel refresh with a clear message", async ({ page }) => {
    await page.goto("/admin");
    await mockJobFlow(page, {
      queued: job({ status: "pending" }),
      refreshStatus: 409,
      refreshBody: {
        error: {
          type: "conflict",
          message: "A refresh for this tournament is already in progress",
          details: { job: job({ status: "running" }) },
        },
      },
      sequence: [
        statusPayload({
          is_refreshing: true,
          active_job: job({
            status: "running",
            progress: { stage: "persist", percent: 75 },
          }),
        }),
      ],
    });

    await page.getByTestId("refresh-button").click();

    await expect(page.getByTestId("refresh-action-error")).toContainText(
      "уже выполняется",
    );
    await expect(page.getByTestId("refresh-button")).toBeDisabled();
    await expect(page.getByTestId("refresh-stage")).toContainText("Запись в базу");
  });

  test("keeps the job state across a page reload", async ({ page }) => {
    // The status endpoint answers without a job id, so a reload rediscovers an
    // in-flight refresh. The server-rendered page is what proves it: the browser
    // starts from scratch and still shows the running job.
    const running = job({
      status: "running",
      started_at: "2026-08-09T10:00:02Z",
      progress: { stage: "fetch_players", percent: 20, message: "Fetched player page 3" },
    });
    await page.route("**/admin/ingestion/rpl/status", async (route: Route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          statusPayload({ is_refreshing: true, active_job: running }),
        ),
      });
    });

    await page.goto("/admin");
    await expect(page.getByTestId("refresh-job-status")).toHaveText("Выполняется");
    await expect(page.getByTestId("refresh-button")).toBeDisabled();

    await page.reload();
    await expect(page.getByTestId("refresh-job-status")).toHaveText("Выполняется");
    await expect(page.getByTestId("refresh-stage")).toContainText(
      "Загрузка игроков",
    );
    await expect(page.getByTestId("refresh-button")).toBeDisabled();
  });

  test("imports a real season end to end", async ({ page }) => {
    test.skip(
      !process.env.RUN_LIVE_REFRESH_E2E,
      "Set RUN_LIVE_REFRESH_E2E=1 to run a real import (needs outbound network)",
    );
    // A full RPL season import takes ~45 s plus the quality gate.
    test.setTimeout(300_000);

    await page.goto("/admin");
    await expect(page.getByTestId("refresh-button")).toBeEnabled();
    await page.getByTestId("refresh-button").click();

    // A real worker is now running: the button is blocked and the stage advances.
    await expect(page.getByTestId("refresh-button")).toBeDisabled();
    await expect(page.getByTestId("refresh-job-status")).toHaveText("Выполняется", {
      timeout: 60_000,
    });

    // A reload must not lose the running job (state lives in PostgreSQL).
    await page.reload();
    await expect(page.getByTestId("refresh-button")).toBeDisabled();
    await expect(page.getByTestId("refresh-job-status")).toHaveText("Выполняется");

    await expect(page.getByTestId("refresh-job-status")).toHaveText("Успешно", {
      timeout: 240_000,
    });
    await expect(page.getByTestId("refresh-outcome")).toContainText(
      "Снапшот обновлён и опубликован",
    );
    await expect(page.getByTestId("refresh-highlights")).toContainText("590");
    await expect(page.getByTestId("refresh-button")).toBeEnabled();
  });
});
