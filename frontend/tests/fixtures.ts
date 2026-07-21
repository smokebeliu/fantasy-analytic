import type { PlayerModel, Role, SeasonRulesModel } from "@/lib/types";

export const RPL_RULES: SeasonRulesModel = {
  total_budget: 100,
  total_players: 15,
  starting_players: 11,
  full_roster_constraints: [
    { role: "GOALKEEPER", minCount: 2, maxCount: 2 },
    { role: "DEFENDER", minCount: 5, maxCount: 5 },
    { role: "MIDFIELDER", minCount: 5, maxCount: 5 },
    { role: "FORWARD", minCount: 3, maxCount: 3 },
  ],
  starting_roster_constraints: [
    { role: "GOALKEEPER", minCount: 1, maxCount: 1 },
    { role: "DEFENDER", minCount: 3, maxCount: 5 },
    { role: "MIDFIELDER", minCount: 2, maxCount: 5 },
    { role: "FORWARD", minCount: 1, maxCount: 3 },
  ],
};

let counter = 0;

export function makePlayer(overrides: Partial<PlayerModel> = {}): PlayerModel {
  counter += 1;
  return {
    player_season_id: overrides.player_season_id ?? counter,
    role: overrides.role ?? "MIDFIELDER",
    fantasy_player_id: overrides.fantasy_player_id ?? `f${counter}`,
    player_name: overrides.player_name ?? `Игрок ${counter}`,
    club_id: overrides.club_id ?? 1,
    club_name: overrides.club_name ?? "Клуб 1",
    price: overrides.price ?? 5,
    availability_status: overrides.availability_status ?? "UNKNOWN",
    selected_by: overrides.selected_by ?? 10,
    form: overrides.form ?? 5,
    season_score: overrides.season_score ?? 50,
    average_score: overrides.average_score ?? 4,
    projection: overrides.projection ?? {
      model_name: "poisson_events",
      model_version: "1.0.0",
      expected_points: 4,
    },
    ...overrides,
  };
}

// Build a valid 15-man squad: 2 GK, 5 DEF, 5 MID, 3 FWD spread across enough
// clubs to stay under the 3-per-club cap, priced to fit a 100 budget.
export function makeValidSquad(): PlayerModel[] {
  const spec: [Role, number][] = [
    ["GOALKEEPER", 2],
    ["DEFENDER", 5],
    ["MIDFIELDER", 5],
    ["FORWARD", 3],
  ];
  const squad: PlayerModel[] = [];
  let id = 100;
  let club = 1;
  let inClub = 0;
  for (const [role, count] of spec) {
    for (let i = 0; i < count; i += 1) {
      if (inClub >= 3) {
        club += 1;
        inClub = 0;
      }
      inClub += 1;
      id += 1;
      squad.push(
        makePlayer({
          player_season_id: id,
          fantasy_player_id: `f${id}`,
          role,
          club_id: club,
          club_name: `Клуб ${club}`,
          price: 6,
        }),
      );
    }
  }
  return squad;
}
