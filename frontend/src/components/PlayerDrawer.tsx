"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, ApiError } from "@/lib/api";
import type { ForecastModel, PlayerDetailModel } from "@/lib/types";
import { PlayerCard } from "./PlayerCard";
import { ErrorState, LoadingState } from "./StateBlocks";

export function PlayerDrawer({
  playerSeasonId,
  tourId,
  model,
  onClose,
}: {
  playerSeasonId: number;
  tourId?: number | null;
  model: ForecastModel;
  onClose: () => void;
}) {
  const [player, setPlayer] = useState<PlayerDetailModel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api
      .getPlayer(playerSeasonId, { tour_id: tourId, model })
      .then((data) => {
        if (active) setPlayer(data);
      })
      .catch((err: unknown) => {
        if (active) {
          setError(err instanceof ApiError ? err.message : "Неизвестная ошибка");
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [playerSeasonId, tourId, model, reloadKey]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="drawer-overlay"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
    >
      <div className="drawer" onClick={(e) => e.stopPropagation()}>
        <div className="modal__head">
          <h2 style={{ fontSize: 16 }}>Карточка игрока</h2>
          <div style={{ display: "flex", gap: 8 }}>
            <Link className="btn btn--sm" href={`/players/${playerSeasonId}`}>
              Открыть страницу
            </Link>
            <button className="icon-btn" onClick={onClose} aria-label="Закрыть">
              ×
            </button>
          </div>
        </div>
        {loading && <LoadingState label="Загрузка карточки…" />}
        {!loading && error && (
          <ErrorState message={error} onRetry={() => setReloadKey((k) => k + 1)} />
        )}
        {!loading && !error && player && <PlayerCard player={player} />}
      </div>
    </div>
  );
}
