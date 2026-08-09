import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PlayerTable } from "@/components/PlayerTable";
import { makePlayer } from "./fixtures";

function setup(overrides: Partial<Parameters<typeof PlayerTable>[0]> = {}) {
  const props = {
    players: [
      makePlayer({ player_season_id: 1, player_name: "Батраков", role: "MIDFIELDER" }),
      makePlayer({ player_season_id: 2, player_name: "Сперцян", role: "MIDFIELDER" }),
    ],
    order: "projection" as const,
    onOrderChange: vi.fn(),
    onOpenPlayer: vi.fn(),
    compareIds: [] as number[],
    onToggleCompare: vi.fn(),
    canCompareMore: true,
    ...overrides,
  };
  render(<PlayerTable {...props} />);
  return props;
}

describe("PlayerTable", () => {
  it("renders a row per player", () => {
    setup();
    expect(screen.getAllByTestId("player-row")).toHaveLength(2);
    expect(screen.getByText("Батраков")).toBeInTheDocument();
  });

  it("calls onOrderChange when a sortable header is clicked", async () => {
    const props = setup();
    await userEvent.click(screen.getByText(/Цена/));
    expect(props.onOrderChange).toHaveBeenCalledWith("price");
  });

  it("sorts by season points when the Очки header is clicked", async () => {
    const props = setup();
    await userEvent.click(screen.getByText(/Очки/));
    expect(props.onOrderChange).toHaveBeenCalledWith("season_score");
  });

  it("opens the player card on name click", async () => {
    const props = setup();
    await userEvent.click(screen.getByText("Сперцян"));
    expect(props.onOpenPlayer).toHaveBeenCalledWith(2);
  });

  it("toggles comparison via the checkbox", async () => {
    const props = setup();
    const checkboxes = screen.getAllByRole("checkbox");
    await userEvent.click(checkboxes[0]);
    expect(props.onToggleCompare).toHaveBeenCalledWith(props.players[0]);
  });

  it("disables extra compare checkboxes when the limit is reached", () => {
    setup({ compareIds: [99], canCompareMore: false });
    const checkboxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    expect(checkboxes.every((c) => c.disabled)).toBe(true);
  });

  it("marks prior-season and newcomer projections (step 14)", () => {
    setup({
      players: [
        makePlayer({
          player_season_id: 1,
          player_name: "Вернувшийся",
          projection: {
            model_name: "poisson_events",
            model_version: "1.0.0",
            stat_source: "prior_season",
            has_history: true,
            expected_points: 5,
          },
        }),
        makePlayer({
          player_season_id: 2,
          player_name: "Новобранец",
          projection: {
            model_name: "poisson_events",
            model_version: "1.0.0",
            stat_source: "prior_season",
            has_history: false,
            expected_points: 2,
          },
        }),
        makePlayer({
          player_season_id: 3,
          player_name: "Текущий",
          projection: {
            model_name: "poisson_events",
            model_version: "1.0.0",
            stat_source: "current_season",
            has_history: true,
            expected_points: 4,
          },
        }),
      ],
    });
    expect(screen.getByText("прошлый сезон")).toBeInTheDocument();
    expect(screen.getByText("новичок")).toBeInTheDocument();
    // A current-season projection carries no provenance badge.
    expect(screen.getAllByText("прошлый сезон")).toHaveLength(1);
  });
});
