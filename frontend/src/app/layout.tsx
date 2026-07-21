import type { Metadata } from "next";
import "./globals.css";
import { api } from "@/lib/api";
import { relativeFreshness } from "@/lib/format";
import { NavLinks } from "@/components/NavLinks";
import type { SnapshotMeta } from "@/lib/types";

export const metadata: Metadata = {
  title: "Fantasy Analytics — РПЛ",
  description: "Прогноз очков и подбор состава Fantasy РПЛ",
};

async function loadSnapshot(): Promise<{ snapshot: SnapshotMeta | null; ok: boolean }> {
  try {
    const seasons = await api.listSeasons({ limit: 1 });
    return { snapshot: seasons.items[0]?.snapshot ?? null, ok: true };
  } catch {
    return { snapshot: null, ok: false };
  }
}

export default async function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { snapshot, ok } = await loadSnapshot();

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
            <div className="freshness" data-testid="freshness">
              <div className="freshness__item">
                <span className="freshness__label">Данные</span>
                <span className="freshness__value">
                  {ok
                    ? snapshot?.data_freshness
                      ? relativeFreshness(snapshot.data_freshness)
                      : "нет активного снапшота"
                    : "API недоступен"}
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
