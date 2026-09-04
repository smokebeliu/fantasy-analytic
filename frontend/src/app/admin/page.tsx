import { api, ApiError } from "@/lib/api";
import { LEAGUE_COOKIE } from "@/lib/league";
import { FullRefreshPanel } from "@/components/FullRefreshPanel";
import { RefreshPanel } from "@/components/RefreshPanel";
import { cookies } from "next/headers";
import type {
  CompetitionModel,
  FullRefreshStatus,
  IngestionStatusResponse,
} from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function AdminPage() {
  let status: IngestionStatusResponse | null = null;
  let fullRefresh: FullRefreshStatus | null = null;
  let competitions: CompetitionModel[] = [];
  let loadError: string | null = null;

  // The admin screen offers the *whole* catalogue, not just the leagues with
  // data: importing a league for the first time is the main thing it is for.
  // Which league it starts on follows the header's switcher.
  const preferredSlug = (await cookies()).get(LEAGUE_COOKIE)?.value ?? null;

  try {
    competitions = (await api.listCompetitions({ limit: 200 })).items;
  } catch (error) {
    loadError =
      error instanceof ApiError
        ? error.message
        : "Не удалось получить список лиг от API аналитики.";
  }

  const slug =
    competitions.find((item) => item.slug === preferredSlug)?.slug ??
    competitions.find((item) => item.is_imported)?.slug ??
    competitions[0]?.slug ??
    null;

  // The status is fetched on the server so a page reload (or a fresh tab) shows
  // an in-flight refresh immediately, without a client round-trip first.
  if (loadError === null && slug !== null) {
    try {
      status = await api.getIngestionStatus(slug);
    } catch (error) {
      loadError =
        error instanceof ApiError
          ? error.message
          : "Не удалось получить состояние обновления от API аналитики.";
    }
  }
  // The one-button run lives in the API process; a page opened mid-run shows
  // it straight away. Failing to read it is not fatal: the panel re-fetches.
  if (loadError === null) {
    try {
      fullRefresh = await api.getFullRefresh();
    } catch {
      fullRefresh = null;
    }
  }

  return (
    <div>
      <div className="page-head">
        <h1>Обновление данных</h1>
        <p>
          Импорт сезона из Sports.ru и контроль качества перед туром. Запуск
          ручной и отдельный для каждой лиги: параллельные обновления одной лиги
          блокируются, а состояние задания хранится в базе, поэтому перезагрузка
          страницы его не теряет.
        </p>
      </div>

      {competitions.length === 0 && loadError === null && (
        <div className="panel panel--pad">
          <div className="inline-note">
            Список лиг пуст. Нажмите «Обновить список лиг», чтобы прочитать его у
            Sports.ru, затем выберите лигу и запустите импорт.
          </div>
        </div>
      )}

      {competitions.some((item) => item.is_imported) && (
        <FullRefreshPanel initialStatus={fullRefresh} />
      )}

      <RefreshPanel
        key={slug ?? "no-league"}
        initialStatus={status}
        initialError={loadError}
        competitions={competitions}
        initialSlug={slug ?? undefined}
      />
    </div>
  );
}
