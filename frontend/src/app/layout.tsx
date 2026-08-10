import type { Metadata } from "next";
import "./globals.css";
import { relativeFreshness } from "@/lib/format";
import { loadLeagueContext } from "@/lib/league";
import { NavLinks } from "@/components/NavLinks";
import { LeagueSwitcher } from "@/components/LeagueSwitcher";
import { selectLeague } from "./actions";

export const metadata: Metadata = {
  title: "Fantasy Analytics",
  description: "Прогноз очков и подбор состава фэнтези Sports.ru",
};

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  // The header describes the league the pages below are showing, so it resolves
  // the same league context they do (from the cookie) instead of guessing.
  const { competitions, competition, error } = await loadLeagueContext();
  const snapshot = competition?.snapshot ?? null;

  return (
    <html lang="ru">
      <body>
        <header className="site-header">
          <div className="site-header__inner">
            <div className="brand">
              <span className="brand__dot" />
              Fantasy Analytics
            </div>
            <NavLinks />
            <LeagueSwitcher
              competitions={competitions}
              selectedSlug={competition?.slug ?? null}
              onSelect={selectLeague}
            />
            <div className="freshness" data-testid="freshness">
              <div className="freshness__item">
                <span className="freshness__label">Сезон</span>
                <span className="freshness__value" data-testid="freshness-season">
                  {competition?.latest_season?.label ?? "—"}
                </span>
              </div>
              <div className="freshness__item">
                <span className="freshness__label">Данные</span>
                <span className="freshness__value">
                  {error
                    ? "API недоступен"
                    : snapshot?.data_freshness
                      ? relativeFreshness(snapshot.data_freshness)
                      : "нет активного снапшота"}
                </span>
              </div>
              <div className="freshness__item">
                <span className="freshness__label">Модель</span>
                <span className="freshness__value">poisson_events</span>
              </div>
              {snapshot?.run_id != null && (
                <div className="freshness__item">
                  <span className="freshness__label">Снапшот</span>
                  <span className="freshness__value">#{snapshot.run_id}</span>
                </div>
              )}
            </div>
          </div>
        </header>
        <main className="app-shell">{children}</main>
      </body>
    </html>
  );
}
