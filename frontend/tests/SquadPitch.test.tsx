import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SquadPitch } from "@/components/SquadPitch";
import { resolveSquadLimits } from "@/lib/squad";
import { RPL_RULES, makePlayer } from "./fixtures";

const limits = resolveSquadLimits(RPL_RULES, 3);

describe("SquadPitch", () => {
  it("renders selected players and empty slots per role", () => {
    const selected = [
      makePlayer({ player_season_id: 1, player_name: "Вратарь", role: "GOALKEEPER" }),
    ];
    render(
      <SquadPitch selected={selected} limits={limits} onRemove={() => {}} />,
    );

    expect(screen.getByText("Вратарь")).toBeInTheDocument();
    // RPL roster is 2 GK / 5 DEF / 5 MID / 3 FWD = 15 slots; one GK is filled,
    // so 14 empty placeholder buttons remain.
    const emptySlots = screen.getAllByRole("button", {
      name: /Добавить на позицию/,
    });
    expect(emptySlots.length).toBe(14);
  });

  it("removes a player and focuses an empty slot", async () => {
    const onRemove = vi.fn();
    const onEmptySlot = vi.fn();
    const selected = [
      makePlayer({ player_season_id: 7, player_name: "Форвард", role: "FORWARD" }),
    ];
    render(
      <SquadPitch
        selected={selected}
        limits={limits}
        onRemove={onRemove}
        onEmptySlot={onEmptySlot}
      />,
    );

    await userEvent.click(screen.getByRole("button", { name: /Убрать Форвард/ }));
    expect(onRemove).toHaveBeenCalledWith(7);

    await userEvent.click(
      screen.getAllByRole("button", { name: "Добавить на позицию ВРТ" })[0],
    );
    expect(onEmptySlot).toHaveBeenCalledWith("GOALKEEPER");
  });

  it("skips placeholder slots when role maxima do not sum to the roster size", () => {
    // No roster constraints -> each role max defaults to the roster size, which
    // would draw a nonsensical number of slots; the component suppresses them.
    const looseLimits = resolveSquadLimits(
      { total_budget: 100, total_players: 15 },
      3,
    );
    render(
      <SquadPitch selected={[]} limits={looseLimits} onRemove={() => {}} />,
    );
    expect(
      screen.queryByRole("button", { name: /Добавить на позицию/ }),
    ).not.toBeInTheDocument();
  });
});
