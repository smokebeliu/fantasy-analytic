"use client";

import { useTransition } from "react";
import { useRouter } from "next/navigation";
import type { CompetitionModel } from "@/lib/types";

/**
 * Switch the league the whole app is showing (step 22).
 *
 * `onSelect` persists the choice (a server action writing the league cookie) and
 * is injected rather than imported so the component stays renderable in a plain
 * test environment. The refresh afterwards is what re-renders the server tree —
 * the header's freshness, the tour list and the player table all belong to the
 * league that was just chosen.
 */
export function LeagueSwitcher({
  competitions,
  selectedSlug,
  onSelect,
}: {
  competitions: CompetitionModel[];
  selectedSlug: string | null;
  onSelect: (slug: string) => Promise<void> | void;
}) {
  const router = useRouter();
  const [pending, startTransition] = useTransition();

  if (competitions.length === 0) {
    return (
      <div className="league-switcher" data-testid="league-switcher-empty">
        <span className="league-switcher__label">Лига</span>
        <span className="league-switcher__note">нет импортированных лиг</span>
      </div>
    );
  }

  const change = (slug: string) => {
    if (slug === selectedSlug) return;
    startTransition(async () => {
      await onSelect(slug);
      router.refresh();
    });
  };

  return (
    <div className="league-switcher" data-testid="league-switcher">
      <label className="league-switcher__label" htmlFor="league-select">
        Лига
      </label>
      <select
        id="league-select"
        data-testid="league-select"
        value={selectedSlug ?? ""}
        disabled={pending}
        aria-busy={pending}
        onChange={(event) => change(event.target.value)}
      >
        {competitions.map((competition) => (
          <option key={competition.slug} value={competition.slug}>
            {competition.name}
            {competition.latest_season
              ? ` · ${competition.latest_season.label}`
              : ""}
            {competition.snapshot ? "" : " (нет снапшота)"}
          </option>
        ))}
      </select>
    </div>
  );
}
