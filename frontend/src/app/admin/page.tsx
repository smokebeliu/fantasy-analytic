import { api, ApiError } from "@/lib/api";
import { RefreshPanel } from "@/components/RefreshPanel";
import type { IngestionStatusResponse } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function AdminPage() {
  let status: IngestionStatusResponse | null = null;
  let loadError: string | null = null;

  // The status is fetched on the server so a page reload (or a fresh tab) shows
  // an in-flight refresh immediately, without a client round-trip first.
  try {
    status = await api.getIngestionStatus();
  } catch (error) {
    loadError =
      error instanceof ApiError
        ? error.message
        : "Не удалось получить состояние обновления от API аналитики.";
  }

  return (
    <div>
      <div className="page-head">
        <h1>Обновление данных</h1>
        <p>
          Импорт сезона из Sports.ru и контроль качества перед туром. Запуск
          ручной: параллельные обновления блокируются, а состояние задания
          хранится в базе, поэтому перезагрузка страницы его не теряет.
        </p>
      </div>

      <RefreshPanel initialStatus={status} initialError={loadError} />
    </div>
  );
}
