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
});
