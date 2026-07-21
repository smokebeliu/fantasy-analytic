import { api, ApiError } from "@/lib/api";
import { SquadBuilder } from "@/components/SquadBuilder";
import type { SeasonDetailModel, TourModel } from "@/lib/types";

export const dynamic = "force-dynamic";

function pickDefaultTour(tours: TourModel[]): TourModel | null {
  if (tours.length === 0) return null;
  return tours.find((t) => t.status !== "FINISHED") ?? tours[tours.length - 1];
}

export default async function SquadPage() {
  let season: SeasonDetailModel | null = null;
  let tours: TourModel[] = [];
  let loadError: string | null = null;

  try {
    const seasons = await api.listSeasons({ limit: 1 });
    const seasonId = seasons.items[0]?.season_id ?? null;
    if (seasonId != null) {
      season = await api.getSeason(seasonId);
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
        <h1>Конструктор состава</h1>
        <p>
          Соберите состав вручную с проверкой всех ограничений или получите
          оптимальный состав от солвера.
        </p>
      </div>

      {loadError && (
        <div className="panel panel--pad">
          <div className="error-inline">
            {loadError} Убедитесь, что backend запущен и доступен.
          </div>
        </div>
      )}

      {!loadError && (season == null || defaultTour == null) && (
        <div className="panel panel--pad">
          <div className="inline-note">
            Нет опубликованного снапшота. Выполните импорт и контроль качества.
          </div>
        </div>
      )}

      {!loadError && season != null && defaultTour != null && (
        <SquadBuilder
          seasonId={season.season_id}
          tours={tours}
          defaultTourId={defaultTour.tour_id}
          rules={season.rules ?? null}
        />
      )}
    </div>
  );
}
