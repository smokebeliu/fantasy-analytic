import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RefreshPanel } from "@/components/RefreshPanel";
import { ApiError } from "@/lib/api";
import type {
  CompetitionModel,
  IngestionJob,
  IngestionStatusResponse,
} from "@/lib/types";

const getIngestionStatus = vi.fn();
const refreshIngestion = vi.fn();
const syncCompetitions = vi.fn();
const routerRefresh = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    getIngestionStatus: (...args: unknown[]) => getIngestionStatus(...args),
    refreshIngestion: (...args: unknown[]) => refreshIngestion(...args),
    syncCompetitions: (...args: unknown[]) => syncCompetitions(...args),
  },
  // Mirrors the real ApiError, whose status the panel branches on (409).
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

const STAGES = [
  { stage: "queued", percent: 0 },
  { stage: "starting", percent: 5 },
  { stage: "fetch_players", percent: 20 },
  { stage: "persist", percent: 75 },
  { stage: "finished", percent: 100 },
];

function job(overrides: Partial<IngestionJob> = {}): IngestionJob {
  return {
    id: 42,
    status: "pending",
    tournament_slug: "russia",
    created_at: "2026-08-09T10:00:00Z",
    ...overrides,
  };
}

function competition(
  overrides: Partial<CompetitionModel> = {},
): CompetitionModel {
  return {
    competition_id: 1,
    fantasy_tournament_id: "1",
    slug: "russia",
    name: "Россия",
    sort_order: 1,
    available_seasons: [
      {
        fantasy_season_id: "59",
        stat_season_id: "rfpl_25-26",
        name: "2025/2026",
        label: "2025/2026",
        is_active: false,
      },
      {
        fantasy_season_id: "75",
        stat_season_id: "rfpl_26-27",
        name: "2026/2027",
        label: "2026/2027",
        is_active: true,
      },
    ],
    has_active_season: true,
    seasons: [],
    is_imported: true,
    ...overrides,
  };
}

// A league whose latest season is already finished, so "active season" is not
// offered — Serie A and the Bundesliga are in exactly this state.
const ITALY = competition({
  competition_id: 3,
  fantasy_tournament_id: "15",
  slug: "italy",
  name: "Италия",
  sort_order: 10,
  has_active_season: false,
  available_seasons: [
    {
      fantasy_season_id: "69",
      stat_season_id: "serie_a_25-26",
      name: "2025/2026",
      label: "2025/2026",
      is_active: false,
    },
  ],
});

const COMPETITIONS = [competition(), ITALY];

function status(overrides: Partial<IngestionStatusResponse> = {}): IngestionStatusResponse {
  return {
    tournament_slug: "russia",
    competition: competition(),
    is_refreshing: false,
    stages: STAGES,
    snapshot: {
      run_id: 1,
      season_id: 1,
      data_freshness: "2026-08-01T12:00:00Z",
      quality_checked_at: "2026-08-01T12:00:10Z",
    },
    season: {
      season_id: 1,
      fantasy_season_id: "59",
      stat_season_id: "rfpl_25-26",
      name: "2025/2026",
      label: "2025/2026",
      competition_id: 1,
      competition_name: "Россия",
      competition_slug: "russia",
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

type PanelProps = Parameters<typeof RefreshPanel>[0];

function renderPanel(overrides: Partial<PanelProps> = {}) {
  return render(
    <RefreshPanel
      initialStatus={status()}
      competitions={COMPETITIONS}
      initialSlug="russia"
      {...overrides}
    />,
  );
}

describe("RefreshPanel", () => {
  beforeEach(() => {
    getIngestionStatus.mockReset();
    refreshIngestion.mockReset();
    syncCompetitions.mockReset();
    routerRefresh.mockReset();
  });

  it("shows the published snapshot and the target tour", async () => {
    getIngestionStatus.mockResolvedValue(status());
    renderPanel();
    await waitFor(() => expect(getIngestionStatus).toHaveBeenCalledWith("russia"));

    const snapshot = screen.getByTestId("refresh-snapshot");
    expect(snapshot).toHaveTextContent("#1");
    expect(snapshot).toHaveTextContent("2025/2026");
    expect(screen.getByTestId("refresh-snapshot-league")).toHaveTextContent(
      "Россия",
    );
    expect(screen.getByTestId("refresh-target-tour")).toHaveTextContent("30 тур");
    expect(screen.getByTestId("refresh-button")).toBeEnabled();
  });

  it("imports the league that is selected, not a hardcoded one", async () => {
    refreshIngestion.mockResolvedValue(job({ tournament_slug: "italy" }));
    getIngestionStatus.mockResolvedValue(
      status({ tournament_slug: "italy", competition: ITALY }),
    );

    renderPanel();
    await userEvent.selectOptions(screen.getByTestId("refresh-league"), "italy");
    // Switching leagues re-reads that league's own job history.
    await waitFor(() => expect(getIngestionStatus).toHaveBeenCalledWith("italy"));

    await userEvent.click(screen.getByTestId("refresh-button"));
    expect(refreshIngestion).toHaveBeenCalledWith("italy", {});
  });

  it("offers the league's own seasons and disables an absent active season", async () => {
    getIngestionStatus.mockResolvedValue(status());
    renderPanel();
    await waitFor(() => expect(getIngestionStatus).toHaveBeenCalled());

    const select = screen.getByTestId("refresh-season") as HTMLSelectElement;
    const active = Array.from(select.options).find(
      (option) => option.value === "current",
    );
    expect(active?.disabled).toBe(false);
    expect(
      Array.from(select.options).map((option) => option.value),
    ).toContain("59");

    await userEvent.selectOptions(screen.getByTestId("refresh-league"), "italy");
    await waitFor(() => {
      const italyActive = Array.from(
        (screen.getByTestId("refresh-season") as HTMLSelectElement).options,
      ).find((option) => option.value === "current");
      expect(italyActive?.disabled).toBe(true);
    });
  });

  it("imports one explicit season of the league by fantasy id", async () => {
    refreshIngestion.mockResolvedValue(job({ status: "pending" }));
    getIngestionStatus
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ is_refreshing: true }));

    renderPanel();
    await waitFor(() => expect(getIngestionStatus).toHaveBeenCalled());
    await userEvent.selectOptions(screen.getByTestId("refresh-season"), "59");
    await userEvent.click(screen.getByTestId("refresh-button"));

    expect(refreshIngestion).toHaveBeenCalledWith("russia", {
      season_id: "59",
    });
  });

  it("re-reads the league catalogue on demand", async () => {
    getIngestionStatus.mockResolvedValue(status());
    syncCompetitions.mockResolvedValue({
      synced_at: "2026-08-10T00:00:00Z",
      competitions: 24,
      seasons: 77,
      slugs: ["russia", "italy"],
    });

    renderPanel();
    await waitFor(() => expect(getIngestionStatus).toHaveBeenCalled());
    await userEvent.click(screen.getByTestId("sync-competitions"));

    await waitFor(() => expect(syncCompetitions).toHaveBeenCalled());
    // The league list is server-rendered, so a fresh server tree is required.
    await waitFor(() => expect(routerRefresh).toHaveBeenCalled());
  });

  it("starts a refresh and follows the job to success", async () => {
    const queued = job({ status: "pending" });
    const running = job({
      status: "running",
      started_at: "2026-08-09T10:00:02Z",
      progress: { stage: "fetch_players", percent: 20, message: "Fetched player page 3" },
    });
    const done = job({
      status: "succeeded",
      started_at: "2026-08-09T10:00:02Z",
      finished_at: "2026-08-09T10:00:47Z",
      data_freshness: "2026-08-09T10:00:46Z",
      progress: { stage: "finished", percent: 100, message: "Job 42 succeeded" },
      result: {
        snapshot_active: true,
        data_freshness: "2026-08-09T10:00:46Z",
        counts: { players: 590, matches: 240, tours: 30 },
      },
    });

    refreshIngestion.mockResolvedValue(queued);
    getIngestionStatus
      // The revalidation on mount still reports an idle tournament.
      .mockResolvedValueOnce(status())
      .mockResolvedValueOnce(status({ is_refreshing: true, active_job: running }))
      .mockResolvedValue(
        status({
          is_refreshing: false,
          latest_job: done,
          latest_successful_job: done,
          snapshot: {
            run_id: 2,
            season_id: 1,
            data_freshness: "2026-08-09T10:00:46Z",
          },
        }),
      );

    renderPanel();
    await userEvent.click(screen.getByTestId("refresh-button"));

    expect(refreshIngestion).toHaveBeenCalledWith("russia", {});
    // While the job runs the button is blocked, so a second click cannot start
    // a parallel import.
    await waitFor(() =>
      expect(screen.getByTestId("refresh-button")).toBeDisabled(),
    );
    expect(screen.getByTestId("refresh-stage")).toHaveTextContent(
      "Загрузка игроков",
    );

    // The poller picks up the terminal status, stops and refreshes the server
    // tree so the header shows the new snapshot.
    await waitFor(
      () => expect(screen.getByTestId("refresh-job-status")).toHaveTextContent("Успешно"),
      { timeout: 5000 },
    );
    expect(screen.getByTestId("refresh-outcome")).toHaveTextContent(
      "Снапшот обновлён и опубликован",
    );
    expect(screen.getByTestId("refresh-highlights")).toHaveTextContent("590");
    expect(screen.getByTestId("refresh-button")).toBeEnabled();
    await waitFor(() => expect(routerRefresh).toHaveBeenCalled());

    const callsAfterFinish = getIngestionStatus.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 3200));
    expect(getIngestionStatus.mock.calls.length).toBe(callsAfterFinish);
  }, 20000);

  it("shows a safe message and a retry button after a failure", async () => {
    const failed = job({
      status: "failed",
      started_at: "2026-08-09T10:00:02Z",
      finished_at: "2026-08-09T10:00:09Z",
      error_message: "RuntimeError: simulated history failure",
      progress: { stage: "fetch_history", percent: 45 },
    });

    getIngestionStatus.mockResolvedValue(status({ latest_job: failed }));
    renderPanel({ initialStatus: status({ latest_job: failed }) });

    expect(screen.getByTestId("refresh-outcome")).toHaveTextContent(
      "simulated history failure",
    );

    refreshIngestion.mockResolvedValue(job({ id: 43, status: "pending" }));
    getIngestionStatus.mockResolvedValue(
      status({ is_refreshing: true, active_job: job({ id: 43, status: "pending" }) }),
    );

    await userEvent.click(screen.getByTestId("refresh-retry"));
    expect(refreshIngestion).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByTestId("refresh-button")).toBeDisabled(),
    );
  });

  it("reports a conflict when a refresh is already running", async () => {
    refreshIngestion.mockRejectedValue(
      new ApiError(
        409,
        "conflict",
        "A refresh for this tournament is already in progress",
      ),
    );
    getIngestionStatus
      .mockResolvedValueOnce(status())
      .mockResolvedValue(
        status({ is_refreshing: true, active_job: job({ status: "running" }) }),
      );

    renderPanel();
    await waitFor(() => expect(getIngestionStatus).toHaveBeenCalled());
    await userEvent.click(screen.getByTestId("refresh-button"));

    await waitFor(() =>
      expect(screen.getByTestId("refresh-action-error")).toHaveTextContent(
        "уже выполняется",
      ),
    );
    await waitFor(() =>
      expect(screen.getByTestId("refresh-button")).toBeDisabled(),
    );
  });

  it("can refresh the active season instead of the latest completed one", async () => {
    refreshIngestion.mockResolvedValue(job({ status: "pending" }));
    getIngestionStatus
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ is_refreshing: true }));

    renderPanel();
    await waitFor(() => expect(getIngestionStatus).toHaveBeenCalled());
    await userEvent.selectOptions(screen.getByTestId("refresh-season"), "current");
    await userEvent.click(screen.getByTestId("refresh-button"));

    expect(refreshIngestion).toHaveBeenCalledWith("russia", { current: true });
  });

  it("offers a retry when the status cannot be loaded at all", async () => {
    getIngestionStatus
      .mockRejectedValueOnce(new ApiError(0, "network_error", "API недоступен"))
      .mockResolvedValue(status());

    renderPanel({ initialStatus: null, initialError: "API недоступен" });
    expect(screen.getByTestId("error")).toHaveTextContent("API недоступен");

    await userEvent.click(screen.getByRole("button", { name: "Повторить" }));
    await waitFor(() =>
      expect(screen.getByTestId("refresh-panel")).toBeInTheDocument(),
    );
  });
});
