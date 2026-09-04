import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FullRefreshPanel, stepSummary } from "@/components/FullRefreshPanel";
import type { FullRefreshStatus, FullRefreshStep } from "@/lib/types";

const getFullRefresh = vi.fn();
const startFullRefresh = vi.fn();
const routerRefresh = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    getFullRefresh: (...args: unknown[]) => getFullRefresh(...args),
    startFullRefresh: (...args: unknown[]) => startFullRefresh(...args),
  },
  ApiError: class ApiError extends Error {
    status: number;
    type: string;
    constructor(status: number, type: string, message: string) {
      super(message);
      this.status = status;
      this.type = type;
    }
  },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: routerRefresh }),
}));

function step(overrides: Partial<FullRefreshStep> = {}): FullRefreshStep {
  return {
    tournament_slug: "russia",
    competition_name: "Россия",
    kind: "latest_completed",
    status: "pending",
    ...overrides,
  };
}

function running(completed: number): FullRefreshStatus {
  const steps: FullRefreshStep[] = [
    step({ status: completed > 0 ? "succeeded" : "running" }),
    step({
      kind: "current_season",
      status: completed > 1 ? "succeeded" : completed === 1 ? "running" : "pending",
    }),
    step({ kind: "odds", status: completed > 2 ? "succeeded" : "pending" }),
  ];
  return {
    is_running: true,
    run: {
      id: 1,
      status: "running",
      started_at: "2026-09-04T10:00:00Z",
      total_steps: 3,
      completed_steps: completed,
      failed_steps: 0,
      current_step: steps.find((s) => s.status === "running") ?? null,
      steps,
    },
  };
}

const finished: FullRefreshStatus = {
  is_running: false,
  run: {
    id: 1,
    status: "failed",
    started_at: "2026-09-04T10:00:00Z",
    finished_at: "2026-09-04T10:05:00Z",
    total_steps: 3,
    completed_steps: 3,
    failed_steps: 1,
    current_step: null,
    steps: [
      step({ status: "succeeded", detail: "снапшот опубликован" }),
      step({ kind: "current_season", status: "succeeded" }),
      step({ kind: "odds", status: "failed", error: "calendar widget is down" }),
    ],
  },
};

describe("FullRefreshPanel", () => {
  beforeEach(() => {
    vi.useRealTimers();
    getFullRefresh.mockReset();
    startFullRefresh.mockReset();
    routerRefresh.mockReset();
  });

  it("summarises a step with its league, kind, state and reason", () => {
    expect(stepSummary(step({ status: "running" }))).toBe(
      "Россия — Последний завершённый сезон: Выполняется",
    );
    expect(
      stepSummary(step({ kind: "odds", status: "failed", error: "boom" })),
    ).toBe("Россия — Котировки: Ошибка (boom)");
  });

  it("starts the run, follows it to the end and refreshes the page once", async () => {
    getFullRefresh
      .mockResolvedValueOnce({ is_running: false, run: null })
      .mockResolvedValueOnce(running(1))
      .mockResolvedValue(finished);
    startFullRefresh.mockResolvedValue(running(0));
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });

    render(<FullRefreshPanel initialStatus={null} />);
    await waitFor(() => expect(getFullRefresh).toHaveBeenCalledTimes(1));

    await user.click(screen.getByTestId("full-refresh-button"));
    expect(startFullRefresh).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("full-refresh-button")).toBeDisabled();
    expect(screen.getByTestId("full-refresh-progress").textContent).toContain(
      "0/3 шагов",
    );

    await vi.advanceTimersByTimeAsync(2600);
    await waitFor(() =>
      expect(screen.getByTestId("full-refresh-progress").textContent).toContain(
        "1/3 шагов",
      ),
    );
    expect(screen.getByTestId("full-refresh-progress").textContent).toContain(
      "Текущий сезон",
    );

    await vi.advanceTimersByTimeAsync(2600);
    await waitFor(() =>
      expect(screen.getByTestId("full-refresh-state").textContent).toBe(
        "Завершено с ошибками",
      ),
    );
    expect(screen.getByTestId("full-refresh-button")).toBeEnabled();
    expect(routerRefresh).toHaveBeenCalledTimes(1);
    const items = screen.getAllByRole("listitem").map((item) => item.textContent);
    expect(items[2]).toContain("calendar widget is down");

    // Polling stops with the run.
    const calls = getFullRefresh.mock.calls.length;
    await vi.advanceTimersByTimeAsync(6000);
    expect(getFullRefresh).toHaveBeenCalledTimes(calls);
  });

  it("explains a run that is already in flight", async () => {
    // Idle on mount; the 409 on start makes the panel re-read the state, and
    // only then does it learn about the run somebody else started.
    getFullRefresh
      .mockResolvedValueOnce({ is_running: false, run: null })
      .mockResolvedValue(running(2));
    const { ApiError } = await import("@/lib/api");
    startFullRefresh.mockRejectedValue(
      new ApiError(409, "conflict", "A full refresh is already running"),
    );

    render(<FullRefreshPanel initialStatus={{ is_running: false, run: null }} />);
    const user = userEvent.setup();
    await user.click(screen.getByTestId("full-refresh-button"));
    await waitFor(() =>
      expect(screen.getByTestId("full-refresh-error").textContent).toContain(
        "уже выполняется",
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("full-refresh-button")).toBeDisabled(),
    );
  });
});
