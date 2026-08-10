import { describe, expect, it } from "vitest";
import { leagueSeasonId, pickCompetition } from "@/lib/league";
import type { CompetitionModel } from "@/lib/types";

function competition(
  slug: string,
  overrides: Partial<CompetitionModel> = {},
): CompetitionModel {
  return {
    competition_id: 1,
    fantasy_tournament_id: "1",
    slug,
    name: slug,
    sort_order: 1,
    available_seasons: [],
    has_active_season: false,
    seasons: [],
    latest_season: {
      season_id: 7,
      fantasy_season_id: "59",
      stat_season_id: "s",
      name: "2025/2026",
      label: "2025/2026",
      is_active: false,
    },
    snapshot: { run_id: 1, season_id: 7 },
    is_imported: true,
    ...overrides,
  };
}

describe("pickCompetition", () => {
  const russia = competition("russia");
  const spain = competition("spain");

  it("honours the remembered league", () => {
    expect(pickCompetition([russia, spain], "spain")).toBe(spain);
  });

  it("falls back to the first league with a snapshot", () => {
    // A remembered league can vanish after a database reset, so an unknown slug
    // must not leave the UI with nothing to show.
    const unpublished = competition("italy", { snapshot: null });
    expect(pickCompetition([unpublished, spain], "germany")).toBe(spain);
  });

  it("falls back to the first league when none is published", () => {
    const first = competition("italy", { snapshot: null });
    const second = competition("germany", { snapshot: null });
    expect(pickCompetition([first, second], null)).toBe(first);
  });

  it("returns null when nothing has been imported", () => {
    expect(pickCompetition([], "russia")).toBeNull();
  });
});

describe("leagueSeasonId", () => {
  it("uses the league's latest published season", () => {
    expect(leagueSeasonId(competition("russia"))).toBe(7);
  });

  it("is null for a league with no imported season", () => {
    expect(
      leagueSeasonId(competition("russia", { latest_season: null })),
    ).toBeNull();
    expect(leagueSeasonId(null)).toBeNull();
  });
});
