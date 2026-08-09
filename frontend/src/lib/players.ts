// Client-side ordering for the player table.
//
// The season's player set is bounded (a few hundred rows), so the table loads
// the full working set for the current filters once and then sorts and paginates
// entirely on the client. This keeps re-sorting instant (no server round-trip on
// every header click) while the server still owns filtering. The comparators
// mirror the server order keys in read_repository.PLAYER_ORDERS.

import type { PlayerModel, PlayerOrder } from "./types";

// Numeric columns sort descending with missing values pushed to the bottom;
// the name column sorts ascending. Every comparator falls back to the stable
// player_season_id so equal values keep a deterministic order.
type NumericKey = Exclude<PlayerOrder, "name">;

function numericValue(player: PlayerModel, order: NumericKey): number | null {
  switch (order) {
    case "projection":
      return player.projection?.expected_points ?? null;
    case "price":
      return player.price ?? null;
    case "selected_by":
      return player.selected_by ?? null;
    case "season_score":
      return player.season_score ?? null;
  }
}

function compareNumericDesc(a: number | null, b: number | null): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return b - a;
}

export function sortPlayers(
  players: PlayerModel[],
  order: PlayerOrder,
): PlayerModel[] {
  const sorted = [...players];
  if (order === "name") {
    sorted.sort(
      (a, b) =>
        (a.player_name ?? "").localeCompare(b.player_name ?? "", "ru") ||
        a.player_season_id - b.player_season_id,
    );
    return sorted;
  }
  sorted.sort(
    (a, b) =>
      compareNumericDesc(numericValue(a, order), numericValue(b, order)) ||
      a.player_season_id - b.player_season_id,
  );
  return sorted;
}
