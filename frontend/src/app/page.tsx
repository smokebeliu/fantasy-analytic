import { api, ApiError } from "@/lib/api";
import { leagueKey, leagueSeasonId, loadLeagueContext } from "@/lib/league";
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
  const { competition, error } = await loadLeagueContext();
  const seasonId = leagueSeasonId(competition);
  let tours: TourModel[] = [];
  let loadError: string | null = error;

  if (loadError === null && seasonId != null) {
    try {
      const tourResponse = await api.listTours({
        season_id: seasonId,
        limit: 200,
      });
      tours = tourResponse.items;
    } catch (cause) {
      loadError =
        cause instanceof ApiError
          ? cause.message
          : "Не удалось получить данные от API аналитики.";
    }
  }

  const defaultTour = pickDefaultTour(tours);

  return (
    <div>
      <div className="page-head">
        <h1>Тур и игроки</h1>
        <p>
          Прогноз ожидаемых очков, фильтры, сортировка и сравнение игроков перед
          туром
          {competition ? ` ${competition.name}` : ""}.
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
          key={leagueKey(competition)}
          seasonId={seasonId}
          tours={tours}
          defaultTourId={defaultTour.tour_id}
        />
      )}
    </div>
  );
}
