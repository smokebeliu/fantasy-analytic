import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { PlayerCard } from "@/components/PlayerCard";
import type { PlayerDetailModel } from "@/lib/types";
import { makePlayer, makePriorSeason } from "./fixtures";

// The card is what a manager opens to judge a signing. In the opening tours the
// current-season columns are still nearly empty, so last season has to be there.

function detail(overrides: Partial<PlayerDetailModel> = {}): PlayerDetailModel {
  return {
    ...makePlayer({
      player_season_id: 11,
      player_name: "Максим Глушенков",
      club_name: "Зенит",
      role: "MIDFIELDER",
    }),
    season_id: 3,
    history: [],
    prior_season: makePriorSeason(),
    ...overrides,
  };
}

describe("PlayerCard", () => {
  it("reports last season's points, average, rank and appearances", () => {
    render(<PlayerCard player={detail()} />);

    const prior = screen.getByTestId("prior-season");
    expect(prior).toHaveTextContent("144");
    expect(prior).toHaveTextContent("5.3");
    expect(prior).toHaveTextContent("#11");
    expect(prior).toHaveTextContent("27");
    expect(prior).toHaveTextContent("1984");
  });

  it("names the season, the club and the closing price", () => {
    render(<PlayerCard player={detail()} />);

    expect(screen.getByText(/Прошлый сезон · 2025\/2026/)).toBeInTheDocument();
    expect(screen.getByText(/Клуб: Зенит/)).toBeInTheDocument();
    expect(screen.getByText(/Цена на конец сезона: 9.0/)).toBeInTheDocument();
  });

  it("shows keepers their saves and outfielders their recoveries", () => {
    render(<PlayerCard player={detail()} />);
    expect(screen.getByTestId("prior-season")).toHaveTextContent("Отборы");

    render(
      <PlayerCard
        player={detail({
          role: "GOALKEEPER",
          prior_season: makePriorSeason({ saves: 74, ball_recoveries: 3 }),
        })}
      />,
    );
    const cards = screen.getAllByTestId("prior-season");
    expect(cards[1]).toHaveTextContent("Сейвы");
    expect(cards[1]).toHaveTextContent("74");
  });

  it("explains the absence rather than showing empty boxes", () => {
    render(<PlayerCard player={detail({ prior_season: null })} />);

    expect(screen.queryByTestId("prior-season")).not.toBeInTheDocument();
    expect(
      screen.getByText(/Нет данных за прошлый сезон/),
    ).toBeInTheDocument();
  });
});
