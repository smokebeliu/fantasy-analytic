import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SquadBuilder } from "@/components/SquadBuilder";
import { RPL_RULES, makePlayer } from "./fixtures";
import type { TourModel } from "@/lib/types";

const listPlayers = vi.fn();
const optimizeSquad = vi.fn();
const optimizeTransfers = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    listPlayers: (...args: unknown[]) => listPlayers(...args),
    optimizeSquad: (...args: unknown[]) => optimizeSquad(...args),
    optimizeTransfers: (...args: unknown[]) => optimizeTransfers(...args),
  },
  ApiError: class ApiError extends Error {},
}));

const TOURS: TourModel[] = [
  {
    tour_id: 15,
    season_id: 1,
    fantasy_tour_id: "1786",
    name: "15 тур",
    status: "FINISHED",
    max_same_team_players: 3,
    total_transfers: 3,
  },
];

function poolResponse() {
  const items = [
    makePlayer({ player_season_id: 1, player_name: "Вратарь А", role: "GOALKEEPER", club_id: 1, price: 5 }),
    makePlayer({ player_season_id: 2, player_name: "Защитник Б", role: "DEFENDER", club_id: 2, price: 5 }),
  ];
  return { items, pagination: { limit: 200, offset: 0, total: 2, count: 2 } };
}

describe("SquadBuilder", () => {
  beforeEach(() => {
    listPlayers.mockReset();
    optimizeSquad.mockReset();
    optimizeTransfers.mockReset();
    listPlayers.mockResolvedValue(poolResponse());
  });

  it("renders the candidate pool", async () => {
    render(
      <SquadBuilder seasonId={1} tours={TOURS} defaultTourId={15} rules={RPL_RULES} />,
    );
    await waitFor(() =>
      expect(screen.getAllByTestId("pool-row").length).toBeGreaterThan(0),
    );
    expect(screen.getByText("Вратарь А")).toBeInTheDocument();
  });

  it("filters the pool by position via the group buttons", async () => {
    render(
      <SquadBuilder seasonId={1} tours={TOURS} defaultTourId={15} rules={RPL_RULES} />,
    );
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    const filter = screen.getByTestId("role-filter");
    await userEvent.click(within(filter).getByRole("button", { name: "ЗАЩ" }));

    await waitFor(() =>
      expect(listPlayers).toHaveBeenLastCalledWith(
        expect.objectContaining({ role: "DEFENDER" }),
      ),
    );
    // "Все" resets the filter back to no role.
    await userEvent.click(within(filter).getByRole("button", { name: "Все" }));
    await waitFor(() =>
      expect(listPlayers).toHaveBeenLastCalledWith(
        expect.objectContaining({ role: undefined }),
      ),
    );
  });

  it("shows the pitch by default and switches to the list view", async () => {
    render(
      <SquadBuilder seasonId={1} tours={TOURS} defaultTourId={15} rules={RPL_RULES} />,
    );
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    // Pitch view is the default.
    expect(screen.getByTestId("squad-pitch")).toBeInTheDocument();

    // Add a player, then switch to the list view.
    const firstRow = screen.getAllByTestId("pool-row")[0];
    await userEvent.click(within(firstRow).getByRole("button", { name: /Добавить/ }));

    await userEvent.click(screen.getByRole("button", { name: "Список" }));
    const list = screen.getByTestId("squad-list");
    expect(list).toHaveTextContent("Вратарь А");
    expect(screen.queryByTestId("squad-pitch")).not.toBeInTheDocument();
  });

  it("keeps transfers disabled until the squad is valid and shows violations", async () => {
    render(
      <SquadBuilder seasonId={1} tours={TOURS} defaultTourId={15} rules={RPL_RULES} />,
    );
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    const transfersBtn = screen.getByTestId("optimize-transfers");
    expect(transfersBtn).toBeDisabled();

    // Add one player -> squad becomes incomplete (violations listed).
    const firstRow = screen.getAllByTestId("pool-row")[0];
    await userEvent.click(within(firstRow).getByRole("button", { name: /Добавить/ }));

    const violations = await screen.findByTestId("violations");
    expect(violations).toHaveTextContent("15 игроков");
    expect(transfersBtn).toBeDisabled();
  });

  it("runs the optimizer and renders the resulting squad", async () => {
    optimizeSquad.mockResolvedValue({
      optimizer_version: "1.0.0",
      model: "poisson_events",
      mode: "squad",
      generated_at: "2026-07-21T00:00:00Z",
      run_id: 1,
      season_id: 1,
      season: {},
      tour: {},
      rules: {
        total_budget: 100,
        total_players: 15,
        starting_players: 11,
        full_limits: {},
        starting_limits: {},
        max_same_team: 3,
      },
      counts: {},
      valid: true,
      solution: {
        status: "OPTIMAL",
        objective_expected_points: 76.3,
        starting_expected_points: 68.5,
        formation: "4-5-1",
        total_price: 100,
        unused_budget: 0,
        captain: makeCandidate("Капитан"),
        vice_captain: makeCandidate("Вице"),
        squad: [makeCandidate("Капитан")],
        starting: [makeCandidate("Капитан")],
        bench: [makeCandidate("Запасной")],
        transfers: null,
      },
    });

    render(
      <SquadBuilder seasonId={1} tours={TOURS} defaultTourId={15} rules={RPL_RULES} />,
    );
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    await userEvent.click(screen.getByRole("button", { name: /Автосостав/ }));

    const result = await screen.findByTestId("optimizer-result");
    expect(result).toHaveTextContent("4-5-1");
    expect(result).toHaveTextContent("76.3");
    expect(optimizeSquad).toHaveBeenCalledWith({ tour: "1786", model: "poisson_events" });
  });
});

function makeCandidate(name: string) {
  return {
    player_season_id: Math.floor(Math.random() * 1e6),
    fantasy_player_id: "f1",
    player_name: name,
    role: "MIDFIELDER" as const,
    club_id: 1,
    club_name: "Клуб",
    price: 9,
    expected_points: 7.5,
    is_starter: true,
    is_captain: name === "Капитан",
    is_vice_captain: name === "Вице",
  };
}
