import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { LeagueSwitcher } from "@/components/LeagueSwitcher";
import type { CompetitionModel } from "@/lib/types";

const routerRefresh = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh: routerRefresh }),
}));

function competition(
  overrides: Partial<CompetitionModel> = {},
): CompetitionModel {
  const slug = overrides.slug ?? "russia";
  return {
    competition_id: 1,
    fantasy_tournament_id: "1",
    slug,
    name: "Россия",
    sort_order: 1,
    available_seasons: [],
    has_active_season: true,
    seasons: [],
    latest_season: {
      season_id: 1,
      fantasy_season_id: "59",
      stat_season_id: "rfpl_25-26",
      name: "2025/2026",
      label: "2025/2026",
      is_active: false,
    },
    snapshot: { run_id: 1, season_id: 1 },
    is_imported: true,
    ...overrides,
  };
}

const SPAIN = competition({
  competition_id: 2,
  fantasy_tournament_id: "16",
  slug: "spain",
  name: "Испания",
  sort_order: 8,
  latest_season: {
    season_id: 2,
    fantasy_season_id: "66",
    stat_season_id: "la_liga_25-26",
    name: "2025/2026",
    label: "2025/2026",
    is_active: false,
  },
  snapshot: { run_id: 2, season_id: 2 },
});

describe("LeagueSwitcher", () => {
  beforeEach(() => {
    routerRefresh.mockReset();
  });

  it("lists every imported league with its season", () => {
    render(
      <LeagueSwitcher
        competitions={[competition(), SPAIN]}
        selectedSlug="russia"
        onSelect={vi.fn()}
      />,
    );

    const select = screen.getByTestId("league-select") as HTMLSelectElement;
    expect(select.value).toBe("russia");
    expect(
      Array.from(select.options).map((option) => option.textContent),
    ).toEqual(["Россия · 2025/2026", "Испания · 2025/2026"]);
  });

  it("persists the chosen league and re-renders the server tree", async () => {
    const onSelect = vi.fn().mockResolvedValue(undefined);
    render(
      <LeagueSwitcher
        competitions={[competition(), SPAIN]}
        selectedSlug="russia"
        onSelect={onSelect}
      />,
    );

    await userEvent.selectOptions(screen.getByTestId("league-select"), "spain");

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith("spain"));
    // Without the refresh the header would keep showing the previous league's
    // freshness while the page below it changed.
    await waitFor(() => expect(routerRefresh).toHaveBeenCalled());
  });

  it("does not persist a no-op re-selection of the current league", async () => {
    const onSelect = vi.fn();
    render(
      <LeagueSwitcher
        competitions={[competition(), SPAIN]}
        selectedSlug="russia"
        onSelect={onSelect}
      />,
    );

    await userEvent.selectOptions(screen.getByTestId("league-select"), "russia");

    expect(onSelect).not.toHaveBeenCalled();
    expect(routerRefresh).not.toHaveBeenCalled();
  });

  it("marks a league that has no published snapshot", () => {
    render(
      <LeagueSwitcher
        competitions={[competition({ snapshot: null })]}
        selectedSlug="russia"
        onSelect={vi.fn()}
      />,
    );

    expect(screen.getByTestId("league-select")).toHaveTextContent(
      "нет снапшота",
    );
  });

  it("says so when nothing has been imported yet", () => {
    render(
      <LeagueSwitcher competitions={[]} selectedSlug={null} onSelect={vi.fn()} />,
    );

    expect(screen.getByTestId("league-switcher-empty")).toHaveTextContent(
      "нет импортированных лиг",
    );
    expect(screen.queryByTestId("league-select")).toBeNull();
  });
});
