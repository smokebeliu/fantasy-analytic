import { api, ApiError } from "@/lib/api";
import { TourExplorer } from "@/components/TourExplorer";
import type { TourModel } from "@/lib/types";

export const dynamic = "force-dynamic";

// Default tour: the next non-finished tour (the real "next round" during a live
// season); for a fully finished season fall back to the last tour so the view
// is never empty.
function pickDefaultTour(tours: TourModel[]): TourModel | null {
  if (tours.length === 0) return null;
  const upcoming = tours.find((t) => t.status !== "FINISHED");
  return upcoming ?? tours[tours.length - 1];
}

export default async function HomePage() {
  let seasonId: number | null = null;
  let tours: TourModel[] = [];
  let loadError: string | null = null;

  try {
    const seasons = await api.listSeasons({ limit: 1 });
    seasonId = seasons.items[0]?.season_id ?? null;
    if (seasonId != null) {
      const tourResponse = await api.listTours({ season_id: seasonId, limit: 200 });
      tours = tourResponse.items;
    }
  } catch (error) {
    loadError =
      error instanceof ApiError
        ? error.message
        : "Не удалось получить данные от API аналитики.";
  }

  const defaultTour = pickDefaultTour(tours);

  return (
    <div>
      <div className="page-head">
        <h1>Тур и игроки</h1>
        <p>
          Прогноз ожидаемых очков, фильтры, сортировка и сравнение игроков перед
          туром РПЛ.
        </p>
      </div>

      {loadError && (
        <div className="panel panel--pad">
          <div className="error-inline">
            {loadError} Убедитесь, что backend запущен и доступен по адресу{" "}
            <code>BACKEND_URL</code>.
          </div>
        </div>
      )}

      {!loadError && (seasonId == null || defaultTour == null) && (
        <div className="panel panel--pad">
          <div className="inline-note">
            Нет опубликованного снапшота с турами. Выполните импорт и контроль
            качества, затем обновите страницу.
          </div>
        </div>
      )}

      {!loadError && seasonId != null && defaultTour != null && (
        <TourExplorer
          seasonId={seasonId}
          tours={tours}
          defaultTourId={defaultTour.tour_id}
        />
      )}
    </div>
  );
}
