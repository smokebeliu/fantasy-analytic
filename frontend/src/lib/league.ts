// Which league the UI is looking at (step 22).
//
// Every page renders one league at a time, and the choice has to survive a
// navigation and a reload, so it lives in a cookie rather than in React state or
// a query string: the layout (which never sees search params) and the pages then
// read the same value during the same server render, and no page can disagree
// with the header about which league it is showing.
//
// The switcher writes the cookie through the `selectLeague` server action.

import { cookies } from "next/headers";
import { api, ApiError } from "./api";
import type { CompetitionModel } from "./types";

export const LEAGUE_COOKIE = "fa_league";

export interface LeagueContext {
  /** Leagues with at least one imported season — what the UI can show. */
  competitions: CompetitionModel[];
  /** The league being shown, or null when nothing has been imported yet. */
  competition: CompetitionModel | null;
  error: string | null;
}

/**
 * Pick the league to show: the one remembered in the cookie when it still has
 * data, otherwise the first league with a published snapshot, otherwise the
 * first imported one. Falling back rather than failing matters because a
 * remembered league can disappear (a database reset, or a snapshot that the
 * quality gate later refused to publish).
 */
export function pickCompetition(
  competitions: CompetitionModel[],
  preferredSlug: string | null,
): CompetitionModel | null {
  const preferred = competitions.find((item) => item.slug === preferredSlug);
  if (preferred) return preferred;
  return competitions.find((item) => item.snapshot) ?? competitions[0] ?? null;
}

export async function loadLeagueContext(): Promise<LeagueContext> {
  let competitions: CompetitionModel[] = [];
  let error: string | null = null;
  try {
    const response = await api.listCompetitions({
      imported_only: true,
      limit: 200,
    });
    competitions = response.items;
  } catch (cause) {
    error =
      cause instanceof ApiError
        ? cause.message
        : "Не удалось получить список лиг от API аналитики.";
  }

  const store = await cookies();
  const preferredSlug = store.get(LEAGUE_COOKIE)?.value ?? null;
  return {
    competitions,
    competition: pickCompetition(competitions, preferredSlug),
    error,
  };
}

/** The season of a league that the read endpoints should be asked about. */
export function leagueSeasonId(
  competition: CompetitionModel | null,
): number | null {
  return competition?.latest_season?.season_id ?? null;
}

/**
 * Identity of the league a page is rendering, for use as a React `key` on the
 * client components below it.
 *
 * Switching leagues re-renders the server tree, and React would otherwise keep
 * the client state of the components in it — a squad of players from the
 * previous league, a pasted team link, a club filter, a tour id that no longer
 * exists in the new league (which silently disables the import button). Keying
 * the subtree on the league unmounts it instead, so a league change starts from
 * a clean screen. The season is part of the key because a first import moves a
 * league onto a season whose tour ids are equally foreign to that state.
 */
export function leagueKey(competition: CompetitionModel | null): string {
  if (!competition) return "no-league";
  return `${competition.slug}:${competition.latest_season?.season_id ?? "none"}`;
}
