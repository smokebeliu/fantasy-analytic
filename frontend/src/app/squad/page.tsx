import { api, ApiError } from "@/lib/api";
import { leagueSeasonId, loadLeagueContext } from "@/lib/league";
import { SquadBuilder } from "@/components/SquadBuilder";
import type { SeasonDetailModel, TourModel } from "@/lib/types";

export const dynamic = "force-dynamic";

function pickDefaultTour(tours: TourModel[]): TourModel | null {
  if (tours.length === 0) return null;
  return tours.find((t) => t.status !== "FINISHED") ?? tours[tours.length - 1];
}

export default async function SquadPage() {
  const { competition, error } = await loadLeagueContext();
  const seasonId = leagueSeasonId(competition);
  let season: SeasonDetailModel | null = null;
  let tours: TourModel[] = [];
  let loadError: string | null = error;

  if (loadError === null && seasonId != null) {
    try {
      // The roster limits and budget come from the season itself, so each league
      // is optimized against its own rules (Süper Lig allows 2 players per club
      // where the RPL allows 3).
      season = await api.getSeason(seasonId);
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
        <h1>Конструктор состава</h1>
        <p>
          Соберите состав вручную с проверкой всех ограничений, загрузите свою
          команду по ссылке Sports.ru или получите оптимальный состав от солвера
          {competition ? ` для ${competition.name}` : ""}.
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
          fantasySeasonId={season.fantasy_season_id}
          competitionSlug={competition?.slug ?? season.competition_slug ?? ""}
          tours={tours}
          defaultTourId={defaultTour.tour_id}
          rules={season.rules ?? null}
        />
      )}
    </div>
  );
}
