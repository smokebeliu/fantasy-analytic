import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SquadBuilder } from "@/components/SquadBuilder";
import { ApiError } from "@/lib/api";
import { RPL_RULES, makePlayer, makePriorSeason, makeValidSquad } from "./fixtures";
import type { OptimizerCandidate, OptimizerResponse, TourModel } from "@/lib/types";

const listPlayers = vi.fn();
const optimizeSquad = vi.fn();
const optimizeTransfers = vi.fn();
const importSquad = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    listPlayers: (...args: unknown[]) => listPlayers(...args),
    optimizeSquad: (...args: unknown[]) => optimizeSquad(...args),
    optimizeTransfers: (...args: unknown[]) => optimizeTransfers(...args),
    importSquad: (...args: unknown[]) => importSquad(...args),
  },
  ApiError: class ApiError extends Error {
    status: number;
    type: string;
    details: unknown;
    constructor(status: number, type: string, message: string, details?: unknown) {
      super(message);
      this.status = status;
      this.type = type;
      this.details = details;
    }
  },
}));

const FANTASY_SEASON_ID = "59";

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

function renderSquadBuilder() {
  return render(
    <SquadBuilder
      seasonId={1}
      fantasySeasonId={FANTASY_SEASON_ID}
      competitionSlug="russia"
      tours={TOURS}
      defaultTourId={15}
      rules={RPL_RULES}
    />,
  );
}

function poolResponse() {
  const items = [
    makePlayer({
      player_season_id: 1,
      fantasy_player_id: "f-gk",
      player_name: "Вратарь А",
      role: "GOALKEEPER",
      club_id: 1,
      price: 5,
    }),
    makePlayer({
      player_season_id: 2,
      fantasy_player_id: "f-def",
      player_name: "Защитник Б",
      role: "DEFENDER",
      club_id: 2,
      price: 5,
    }),
  ];
  return { items, pagination: { limit: 200, offset: 0, total: 2, count: 2 } };
}

describe("SquadBuilder", () => {
  beforeEach(() => {
    listPlayers.mockReset();
    optimizeSquad.mockReset();
    optimizeTransfers.mockReset();
    importSquad.mockReset();
    listPlayers.mockResolvedValue(poolResponse());
  });

  it("renders the candidate pool", async () => {
    renderSquadBuilder();
    await waitFor(() =>
      expect(screen.getAllByTestId("pool-row").length).toBeGreaterThan(0),
    );
    expect(screen.getByText("Вратарь А")).toBeInTheDocument();
  });

  it("filters the pool by position without refetching", async () => {
    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));
    const requests = listPlayers.mock.calls.length;

    const filter = screen.getByTestId("role-filter");
    await userEvent.click(within(filter).getByRole("button", { name: "ЗАЩ" }));

    // The whole season is loaded once, so switching position is instant and the
    // hover cards can still describe players outside the current filter.
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(1));
    expect(screen.getByText("Защитник Б")).toBeInTheDocument();
    expect(listPlayers.mock.calls.length).toBe(requests);

    await userEvent.click(within(filter).getByRole("button", { name: "Все" }));
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));
  });

  it("loads every page of the player pool", async () => {
    const first = Array.from({ length: 200 }, (_, i) =>
      makePlayer({
        player_season_id: 100 + i,
        fantasy_player_id: `f-${i}`,
        player_name: `Игрок ${i}`,
        role: "MIDFIELDER",
        club_id: 1,
        price: 5,
      }),
    );
    listPlayers.mockReset();
    listPlayers
      .mockResolvedValueOnce({
        items: first,
        pagination: { limit: 200, offset: 0, total: 202, count: 200 },
      })
      .mockResolvedValueOnce({
        items: poolResponse().items,
        pagination: { limit: 200, offset: 200, total: 202, count: 2 },
      });

    renderSquadBuilder();

    await waitFor(() => expect(listPlayers).toHaveBeenCalledTimes(2));
    expect(listPlayers).toHaveBeenLastCalledWith(
      expect.objectContaining({ offset: 200 }),
    );
  });

  it("shows the pitch by default and switches to the list view", async () => {
    renderSquadBuilder();
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
    renderSquadBuilder();
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

    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    await userEvent.click(screen.getByTestId("optimize-from-scratch"));

    const result = await screen.findByTestId("optimizer-result");
    expect(result).toHaveTextContent("4-5-1");
    expect(result).toHaveTextContent("76.3");
    expect(optimizeSquad).toHaveBeenCalledWith({
      season: FANTASY_SEASON_ID,
      tour: "1786",
      model: "poisson_events",
    });
  });

  it("keeps the season history of a generated squad", async () => {
    // The solver reports only a price and a projection, so a generated squad has
    // to be matched back against the loaded players — otherwise the hover cards
    // on the pitch come up empty for players the table describes in full.
    const keeper = makePlayer({
      player_season_id: 1,
      fantasy_player_id: "f-gk",
      player_name: "Вратарь А",
      role: "GOALKEEPER",
      club_id: 1,
      price: 5,
      season_score: 31,
      average_score: 10.3,
      prior_season: makePriorSeason({ points: 144, rank: 11 }),
    });
    listPlayers.mockResolvedValue({
      items: [keeper],
      pagination: { limit: 200, offset: 0, total: 1, count: 1 },
    });
    optimizeSquad.mockResolvedValue(
      optimizerResponse([
        {
          ...makeCandidate("Вратарь А"),
          player_season_id: 1,
          fantasy_player_id: "f-gk",
          role: "GOALKEEPER",
        },
      ]),
    );

    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(1));
    await userEvent.click(screen.getByTestId("optimize-from-scratch"));
    await screen.findByTestId("optimizer-result");

    const pitchPlayer = within(screen.getByTestId("squad-pitch")).getByTestId(
      "pitch-player",
    );
    await userEvent.hover(pitchPlayer);

    const card = await screen.findByTestId("player-hover-card");
    expect(card).toHaveTextContent("31");
    expect(card).toHaveTextContent("10.3");
    expect(await screen.findByTestId("hover-card-prior")).toHaveTextContent("144");
  });

  it("backfills the history when the pool arrives after the squad", async () => {
    // Nothing stops the user from generating a squad while the pool is still
    // loading; the pitch must catch up rather than stay historyless.
    const keeper = makePlayer({
      player_season_id: 1,
      fantasy_player_id: "f-gk",
      player_name: "Вратарь А",
      role: "GOALKEEPER",
      club_id: 1,
      price: 5,
      season_score: 31,
      prior_season: makePriorSeason({ points: 144 }),
    });
    let releasePool: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      releasePool = resolve;
    });
    listPlayers.mockImplementation(async () => {
      await pending;
      return {
        items: [keeper],
        pagination: { limit: 200, offset: 0, total: 1, count: 1 },
      };
    });
    optimizeSquad.mockResolvedValue(
      optimizerResponse([
        {
          ...makeCandidate("Вратарь А"),
          player_season_id: 1,
          fantasy_player_id: "f-gk",
          role: "GOALKEEPER",
        },
      ]),
    );

    renderSquadBuilder();
    await userEvent.click(screen.getByTestId("optimize-from-scratch"));
    await screen.findByTestId("optimizer-result");

    releasePool(undefined);
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(1));

    await userEvent.hover(
      within(screen.getByTestId("squad-pitch")).getByTestId("pitch-player"),
    );
    expect(await screen.findByTestId("hover-card-prior")).toHaveTextContent("144");
  });

  it("pins a player and asks the optimizer to fill the rest under a formation", async () => {
    const pinned: OptimizerCandidate = {
      ...makeCandidate("Вратарь А"),
      player_season_id: 1,
      fantasy_player_id: "f-gk",
      role: "GOALKEEPER",
      is_locked: true,
    };
    optimizeSquad.mockResolvedValue(optimizerResponse([pinned]));

    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    // Add the goalkeeper to the squad, then pin them on the pitch.
    const firstRow = screen.getAllByTestId("pool-row")[0];
    await userEvent.click(within(firstRow).getByRole("button", { name: /Добавить/ }));
    await userEvent.click(screen.getByRole("button", { name: /Закрепить Вратарь А/ }));
    expect(screen.getByTestId("locked-note")).toHaveTextContent("Закреплено 1");

    await userEvent.selectOptions(screen.getByTestId("formation-select"), "4-4-2");
    await userEvent.click(screen.getByTestId("optimize-locked"));

    await waitFor(() =>
      expect(optimizeSquad).toHaveBeenCalledWith({
        season: FANTASY_SEASON_ID,
        tour: "1786",
        model: "poisson_events",
        locked: ["f-gk"],
        formation: "4-4-2",
      }),
    );

    // The pin survives the round-trip because the solver had to keep the player.
    const result = await screen.findByTestId("optimizer-result");
    expect(within(result).getByTitle("Закреплён пользователем")).toBeInTheDocument();
    expect(screen.getByTestId("locked-note")).toHaveTextContent("Закреплено 1");
  });

  it("omits the lock key when only a formation is chosen", async () => {
    optimizeSquad.mockResolvedValue(optimizerResponse([makeCandidate("Капитан")]));

    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    await userEvent.selectOptions(screen.getByTestId("formation-select"), "4-4-2");
    await userEvent.click(screen.getByTestId("optimize-locked"));

    await waitFor(() =>
      expect(optimizeSquad).toHaveBeenCalledWith({
        season: FANTASY_SEASON_ID,
        tour: "1786",
        model: "poisson_events",
        formation: "4-4-2",
      }),
    );
  });

  it("explains that the two build buttons differ in what they keep", async () => {
    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    const help = screen.getByTestId("optimizer-help");
    expect(help).toHaveTextContent("игнорирует всё, что вы выбрали");
    expect(help).toHaveTextContent("сохраняет закреплённых");

    // With nothing pinned and no formation the constrained run would return the
    // same squad, so it is offered as unavailable with the reason attached.
    const constrained = screen.getByTestId("optimize-locked");
    expect(constrained).toBeDisabled();
    expect(constrained).toHaveAttribute(
      "title",
      expect.stringContaining("совпадёт"),
    );

    await userEvent.selectOptions(screen.getByTestId("formation-select"), "4-4-2");
    expect(screen.getByTestId("optimize-locked")).toBeEnabled();
  });

  it("explains the head-to-head clashes the optimizer had to pay for", async () => {
    const ours = { ...makeCandidate("Защитник"), player_season_id: 11, role: "DEFENDER" as const };
    const theirs = { ...makeCandidate("Форвард"), player_season_id: 22, club_name: "Соперник" };
    const response = optimizerResponse([ours, theirs]);
    response.solution.fixture_penalty = 0.55;
    response.solution.objective_score = 69.55;
    response.solution.fixtures = {
      conflict_weight: 0.25,
      head_to_head: [
        {
          match_id: 262,
          clubs: [
            { club_id: 1, club_name: "Клуб", starters: 1 },
            { club_id: 2, club_name: "Соперник", starters: 1 },
          ],
        },
      ],
      cancellation: 2.2144,
      clashes: [
        {
          match_id: 262,
          player_season_id: 11,
          player_name: "Защитник",
          role: "DEFENDER",
          club_name: "Клуб",
          opponent_player_season_id: 22,
          opponent_player_name: "Форвард",
          opponent_role: "MIDFIELDER",
          opponent_club_name: "Соперник",
          cancellation: 2.2144,
          penalty: 0.55,
        },
      ],
    };
    optimizeSquad.mockResolvedValue(response);

    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    await userEvent.click(screen.getByTestId("optimize-from-scratch"));

    const clashes = await screen.findByTestId("optimizer-clashes");
    expect(clashes).toHaveTextContent("Очные встречи в составе: 1");
    expect(clashes).toHaveTextContent("Защитник");
    expect(clashes).toHaveTextContent("Соперник");
    expect(clashes).toHaveTextContent("0.55");
  });

  it("hides the clash panel when nothing cancels out", async () => {
    optimizeSquad.mockResolvedValue(optimizerResponse([makeCandidate("Капитан")]));

    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    await userEvent.click(screen.getByTestId("optimize-from-scratch"));

    await screen.findByTestId("optimizer-result");
    expect(screen.queryByTestId("optimizer-clashes")).not.toBeInTheDocument();
  });

  it("surfaces an incompatible set of pins as an error", async () => {
    optimizeSquad.mockRejectedValue(
      new Error("3 locked GK exceed the squad limit of 2 for this position"),
    );

    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    const firstRow = screen.getAllByTestId("pool-row")[0];
    await userEvent.click(within(firstRow).getByRole("button", { name: /Добавить/ }));
    await userEvent.click(screen.getByRole("button", { name: /Закрепить Вратарь А/ }));
    await userEvent.click(screen.getByTestId("optimize-locked"));

    expect(await screen.findByText(/Ошибка оптимизатора/)).toBeInTheDocument();
  });

  it("asks for the tour's transfer allowance and keeps the user's squad", async () => {
    const squad = makeValidSquad();
    listPlayers.mockResolvedValue({
      items: squad,
      pagination: { limit: 200, offset: 0, total: squad.length, count: squad.length },
    });
    optimizeTransfers.mockResolvedValue(transfersResponse());

    renderSquadBuilder();
    await waitFor(() =>
      expect(screen.getAllByTestId("pool-row").length).toBe(squad.length),
    );

    for (const row of screen.getAllByTestId("pool-row")) {
      await userEvent.click(within(row).getByRole("button", { name: /Добавить/ }));
    }
    expect(await screen.findByTestId("valid-note")).toBeInTheDocument();

    // The tour allows three transfers, which is what the button offers.
    expect(screen.getByTestId("transfers-select")).toHaveValue("3");
    await userEvent.click(screen.getByTestId("optimize-transfers"));

    await waitFor(() =>
      expect(optimizeTransfers).toHaveBeenCalledWith(
        expect.objectContaining({
          season: FANTASY_SEASON_ID,
          max_transfers: 3,
          tour: "1786",
        }),
      ),
    );

    // The suggestion is shown next to the squad the user owns, not instead of it.
    const plan = await screen.findByTestId("transfer-plan");
    expect(plan).toHaveTextContent("Слабый");
    expect(plan).toHaveTextContent("Сильный");
    expect(plan).toHaveTextContent("+4.2 очк.");
    // A signed number next to "budget" reads both ways, so the direction is
    // spelled out instead.
    expect(plan).toHaveTextContent("дороже на 5.0");
    expect(screen.getAllByTestId("pitch-player").length).toBe(squad.length);
  });

  it("says when a suggested squad needs no changes at all", async () => {
    const squad = makeValidSquad();
    listPlayers.mockResolvedValue({
      items: squad,
      pagination: { limit: 200, offset: 0, total: squad.length, count: squad.length },
    });
    const response = transfersResponse();
    response.solution.transfers = {
      allowed: 3,
      made: 0,
      kept: 15,
      in: [],
      out: [],
      pairs: [],
      missing_from_pool: [],
    };
    optimizeTransfers.mockResolvedValue(response);

    renderSquadBuilder();
    await waitFor(() =>
      expect(screen.getAllByTestId("pool-row").length).toBe(squad.length),
    );
    for (const row of screen.getAllByTestId("pool-row")) {
      await userEvent.click(within(row).getByRole("button", { name: /Добавить/ }));
    }
    await userEvent.click(screen.getByTestId("optimize-transfers"));

    expect(
      await screen.findByText(/Состав уже оптимален для этого тура/),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("transfer-plan")).not.toBeInTheDocument();
  });

  it("limits the suggestion to the number of transfers the user picks", async () => {
    const squad = makeValidSquad();
    listPlayers.mockResolvedValue({
      items: squad,
      pagination: { limit: 200, offset: 0, total: squad.length, count: squad.length },
    });
    optimizeTransfers.mockResolvedValue(transfersResponse());

    renderSquadBuilder();
    await waitFor(() =>
      expect(screen.getAllByTestId("pool-row").length).toBe(squad.length),
    );
    for (const row of screen.getAllByTestId("pool-row")) {
      await userEvent.click(within(row).getByRole("button", { name: /Добавить/ }));
    }

    await userEvent.selectOptions(screen.getByTestId("transfers-select"), "1");
    await userEvent.click(screen.getByTestId("optimize-transfers"));

    await waitFor(() =>
      expect(optimizeTransfers).toHaveBeenCalledWith(
        expect.objectContaining({ season: FANTASY_SEASON_ID, max_transfers: 1 }),
      ),
    );
  });

  it("loads a Sports.ru team from a pasted link into the pitch", async () => {
    const squad = makeValidSquad();
    listPlayers.mockResolvedValue({
      items: squad,
      pagination: { limit: 200, offset: 0, total: squad.length, count: squad.length },
    });
    importSquad.mockResolvedValue({
      squad_id: "588960",
      squad_name: "бегим",
      competition_slug: "russia",
      competition_name: "Россия",
      remote_season_id: "59",
      remote_tour: { fantasy_tour_id: "1786", name: "15 тур", status: "FINISHED" },
      players: squad,
      missing: [],
    });

    renderSquadBuilder();
    await waitFor(() =>
      expect(screen.getAllByTestId("pool-row").length).toBe(squad.length),
    );

    const link = "https://www.sports.ru/fantasy/football/russia/588960/";
    await userEvent.type(screen.getByTestId("squad-import-url"), link);
    await userEvent.click(screen.getByTestId("squad-import-submit"));

    await waitFor(() =>
      expect(importSquad).toHaveBeenCalledWith({
        url: link,
        season_id: 1,
        tour_id: 15,
        competition: "russia",
        model: "poisson_events",
      }),
    );
    expect(screen.getByTestId("squad-import-note")).toHaveTextContent("бегим");
    expect(screen.getByTestId("squad-import-note")).toHaveTextContent("15 игроков");
    expect(screen.getByTestId("valid-note")).toBeInTheDocument();
    expect(screen.getByTestId("squad-pitch").querySelectorAll("[data-testid='pitch-player']").length).toBe(15);
  });

  it("shows an error when the pasted link belongs to another league", async () => {
    importSquad.mockRejectedValue(
      new ApiError(
        409,
        "league_mismatch",
        "Лига в ссылке (portugal) не совпадает с выбранной лигой (russia).",
      ),
    );

    renderSquadBuilder();
    await waitFor(() => expect(screen.getAllByTestId("pool-row").length).toBe(2));

    await userEvent.type(
      screen.getByTestId("squad-import-url"),
      "https://www.sports.ru/fantasy/football/portugal/588960/",
    );
    await userEvent.click(screen.getByTestId("squad-import-submit"));

    expect(await screen.findByTestId("squad-import-error")).toHaveTextContent("portugal");
  });
});

function transfersResponse(): OptimizerResponse {
  const leaving = { ...makeCandidate("Слабый"), player_season_id: 3, price: 4 };
  const arriving = { ...makeCandidate("Сильный"), player_season_id: 900, price: 9 };
  const response = optimizerResponse([arriving]);
  response.mode = "transfers";
  response.solution.transfers = {
    allowed: 3,
    made: 1,
    kept: 14,
    in: [arriving],
    out: [leaving],
    pairs: [
      {
        out: leaving,
        in: arriving,
        delta_expected_points: 4.2,
        delta_price: 5,
      },
    ],
    missing_from_pool: [],
  };
  return response;
}

function optimizerResponse(squad: OptimizerCandidate[]): OptimizerResponse {
  return {
    optimizer_version: "1.1.0",
    model: "poisson_events",
    mode: "squad",
    generated_at: "2026-08-09T00:00:00Z",
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
      objective_expected_points: 70.1,
      starting_expected_points: 62.6,
      formation: "4-4-2",
      total_price: 99,
      unused_budget: 1,
      captain: squad[0],
      vice_captain: squad[0],
      squad,
      starting: squad,
      bench: [],
      transfers: null,
      constraints: {
        locked: squad.filter((p) => p.is_locked).map((p) => p.player_season_id),
        locked_starters: [],
        formation: "4-4-2",
      },
    },
  };
}

function makeCandidate(name: string): OptimizerCandidate {
  return {
    player_season_id: Math.floor(Math.random() * 1e6),
    fantasy_player_id: "f1",
    player_name: name,
    role: "MIDFIELDER",
    club_id: 1,
    club_name: "Клуб",
    price: 9,
    expected_points: 7.5,
    is_starter: true,
    is_captain: name === "Капитан",
    is_vice_captain: name === "Вице",
    is_locked: false,
  };
}
