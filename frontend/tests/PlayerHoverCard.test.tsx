import { describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { PlayerTable } from "@/components/PlayerTable";
import { SquadPitch } from "@/components/SquadPitch";
import { resolveSquadLimits } from "@/lib/squad";
import { RPL_RULES, makePlayer, makePriorSeason } from "./fixtures";

// A pitch card and a table row only have room for a name and a number, so the
// numbers a manager actually decides on live in a card that opens on hover.

const PLAYER = makePlayer({
  player_season_id: 11,
  player_name: "Максим Глушенков",
  club_name: "Зенит",
  role: "MIDFIELDER",
  price: 10,
  season_score: 31,
  average_score: 10.3,
  projection: {
    model_name: "poisson_events",
    model_version: "1.0.0",
    expected_points: 11.4,
  },
  prior_season: makePriorSeason(),
});

function renderPitch(player = PLAYER) {
  return render(
    <SquadPitch
      selected={[player]}
      limits={resolveSquadLimits(RPL_RULES, 3)}
      onRemove={() => {}}
    />,
  );
}

describe("player hover card", () => {
  it("stays closed until a player is hovered", async () => {
    renderPitch();
    expect(screen.queryByTestId("player-hover-card")).not.toBeInTheDocument();

    await userEvent.hover(screen.getByTestId("pitch-player"));
    expect(await screen.findByTestId("player-hover-card")).toBeInTheDocument();
  });

  it("names the player and his club in full", async () => {
    renderPitch();
    await userEvent.hover(screen.getByTestId("pitch-player"));

    const card = await screen.findByTestId("player-hover-card");
    // The pitch itself can only fit a truncated name.
    expect(card).toHaveTextContent("Максим Глушенков");
    expect(card).toHaveTextContent("Зенит");
    expect(card).toHaveTextContent("Полузащитник");
  });

  it("shows this season's points and average alongside the projection", async () => {
    renderPitch();
    await userEvent.hover(screen.getByTestId("pitch-player"));

    const card = await screen.findByTestId("player-hover-card");
    expect(card).toHaveTextContent("Очки за сезон");
    expect(card).toHaveTextContent("31");
    expect(card).toHaveTextContent("10.3");
    expect(card).toHaveTextContent("11.4");
  });

  it("shows last season's points, average, rank and matches", async () => {
    renderPitch();
    await userEvent.hover(screen.getByTestId("pitch-player"));

    const prior = await screen.findByTestId("hover-card-prior");
    expect(prior).toHaveTextContent("144");
    expect(prior).toHaveTextContent("5.3");
    expect(prior).toHaveTextContent("#11");
    expect(prior).toHaveTextContent("27");
  });

  it("says so when the player has no previous season", async () => {
    renderPitch(makePlayer({ player_season_id: 7, prior_season: null }));
    await userEvent.hover(screen.getByTestId("pitch-player"));

    const card = await screen.findByTestId("player-hover-card");
    expect(card).toHaveTextContent("Нет данных за прошлый сезон");
    expect(screen.queryByTestId("hover-card-prior")).not.toBeInTheDocument();
  });

  it("closes when the pointer leaves", async () => {
    renderPitch();
    const player = screen.getByTestId("pitch-player");
    await userEvent.hover(player);
    expect(await screen.findByTestId("player-hover-card")).toBeInTheDocument();

    await userEvent.unhover(player);
    await waitFor(() =>
      expect(screen.queryByTestId("player-hover-card")).not.toBeInTheDocument(),
    );
  });

  it("opens from the player table too", async () => {
    render(
      <PlayerTable
        players={[PLAYER]}
        order="season_score"
        onOrderChange={() => {}}
        onOpenPlayer={() => {}}
        compareIds={[]}
        onToggleCompare={() => {}}
        canCompareMore
      />,
    );

    await userEvent.hover(screen.getByText("Максим Глушенков"));
    expect(await screen.findByTestId("player-hover-card")).toHaveTextContent(
      "Максим Глушенков",
    );
  });

  it("puts last season's points in their own table column", () => {
    render(
      <PlayerTable
        players={[PLAYER, makePlayer({ player_season_id: 9, prior_season: null })]}
        order="season_score"
        onOrderChange={() => {}}
        onOpenPlayer={() => {}}
        compareIds={[]}
        onToggleCompare={() => {}}
        canCompareMore
      />,
    );

    const cells = screen.getAllByTestId("prior-points");
    expect(cells[0]).toHaveTextContent("144");
    expect(cells[1]).toHaveTextContent("—");
  });
});
