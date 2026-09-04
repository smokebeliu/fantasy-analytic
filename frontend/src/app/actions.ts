"use server";

import { cookies } from "next/headers";
import { revalidatePath } from "next/cache";
import { LEAGUE_COOKIE } from "@/lib/league";

const ONE_YEAR_SECONDS = 60 * 60 * 24 * 365;

/**
 * Remember which league the user is looking at (step 22).
 *
 * Writing a cookie rather than a query string keeps the header and the page in
 * agreement: a layout never receives search params, so it could not otherwise
 * render the freshness of the league the page below it is showing.
 */
export async function selectLeague(slug: string): Promise<void> {
  const store = await cookies();
  store.set(LEAGUE_COOKIE, slug, {
    path: "/",
    maxAge: ONE_YEAR_SECONDS,
    sameSite: "lax",
  });
  revalidatePath("/", "layout");
}
