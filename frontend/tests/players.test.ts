import { describe, expect, it } from "vitest";
import { sortPlayers } from "@/lib/players";
import { makePlayer } from "./fixtures";

describe("sortPlayers", () => {
  it("sorts by season score descending with nulls last", () => {
    const players = [
      makePlayer({ player_season_id: 1, season_score: 40 }),
      makePlayer({ player_season_id: 2, season_score: null }),
      makePlayer({ player_season_id: 3, season_score: 120 }),
    ];
    const ids = sortPlayers(players, "season_score").map((p) => p.player_season_id);
    expect(ids).toEqual([3, 1, 2]);
  });

  it("sorts by projection expected points descending", () => {
    const players = [
      makePlayer({
        player_season_id: 1,
        projection: { model_name: "poisson_events", model_version: "1", expected_points: 2 },
      }),
      makePlayer({
        player_season_id: 2,
        projection: { model_name: "poisson_events", model_version: "1", expected_points: 9 },
      }),
      makePlayer({ player_season_id: 3, projection: null }),
    ];
    const ids = sortPlayers(players, "projection").map((p) => p.player_season_id);
    expect(ids).toEqual([2, 1, 3]);
  });

  it("sorts by name ascending (ru locale) with id tie-break", () => {
    const players = [
      makePlayer({ player_season_id: 5, player_name: "Ярков" }),
      makePlayer({ player_season_id: 6, player_name: "Ааронов" }),
      makePlayer({ player_season_id: 7, player_name: "Ааронов" }),
    ];
    const ids = sortPlayers(players, "name").map((p) => p.player_season_id);
    expect(ids).toEqual([6, 7, 5]);
  });

  it("does not mutate the input array", () => {
    const players = [
      makePlayer({ player_season_id: 1, price: 5 }),
      makePlayer({ player_season_id: 2, price: 9 }),
    ];
    const original = players.map((p) => p.player_season_id);
    sortPlayers(players, "price");
    expect(players.map((p) => p.player_season_id)).toEqual(original);
  });
});
