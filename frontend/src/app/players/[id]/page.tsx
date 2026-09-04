import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import { PlayerCard } from "@/components/PlayerCard";
import type { ForecastModel, PlayerDetailModel } from "@/lib/types";

export const dynamic = "force-dynamic";

export default async function PlayerPage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ tour_id?: string; model?: string }>;
}) {
  const { id } = await params;
  const { tour_id, model } = await searchParams;
  const playerSeasonId = Number(id);

  let player: PlayerDetailModel | null = null;
  let error: string | null = null;
  let notFound = false;

  try {
    player = await api.getPlayer(playerSeasonId, {
      tour_id: tour_id ? Number(tour_id) : undefined,
      model: (model as ForecastModel) || undefined,
    });
  } catch (err) {
    if (err instanceof ApiError && err.status === 404) {
      notFound = true;
    } else {
      error = err instanceof ApiError ? err.message : "Не удалось загрузить игрока.";
    }
  }

  return (
    <div>
      <div className="page-head">
        <Link className="btn btn--sm btn--ghost" href="/">
          ← К списку игроков
        </Link>
      </div>

      {notFound && (
        <div className="panel panel--pad">
          <div className="inline-note">Игрок #{playerSeasonId} не найден.</div>
        </div>
      )}
      {error && (
        <div className="panel panel--pad">
          <div className="error-inline">{error}</div>
        </div>
      )}
      {player && (
        <div className="panel">
          <PlayerCard player={player} />
        </div>
      )}
    </div>
  );
}
