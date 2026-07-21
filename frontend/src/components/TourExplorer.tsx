"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type {
  ForecastModel,
  PlayerListResponse,
  PlayerModel,
  PlayerOrder,
  Role,
  SnapshotMeta,
  TourModel,
} from "@/lib/types";
import { ROLE_LABELS, ROLES } from "@/lib/squad";
import { PlayerTable } from "./PlayerTable";
import { PlayerDrawer } from "./PlayerDrawer";
import { CompareModal } from "./CompareModal";
import { EmptyState, ErrorState, TableSkeleton } from "./StateBlocks";
import { formatDateTime } from "@/lib/format";

const PAGE_SIZE = 50;
const MAX_COMPARE = 4;
const MODELS: ForecastModel[] = ["poisson_events", "season_mean", "recent_form"];

interface Filters {
  role: Role | "";
  clubId: number | "";
  status: string;
  minPrice: string;
  maxPrice: string;
}

const EMPTY_FILTERS: Filters = {
  role: "",
  clubId: "",
  status: "",
  minPrice: "",
  maxPrice: "",
};

interface ClubOption {
  id: number;
  name: string;
}

export function TourExplorer({
  seasonId,
  tours,
  defaultTourId,
}: {
  seasonId: number;
  tours: TourModel[];
  defaultTourId: number;
}) {
  const [tourId, setTourId] = useState(defaultTourId);
  const [model, setModel] = useState<ForecastModel>("poisson_events");
  const [order, setOrder] = useState<PlayerOrder>("projection");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);

  const [data, setData] = useState<PlayerListResponse | null>(null);
  const [snapshot, setSnapshot] = useState<SnapshotMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [clubs, setClubs] = useState<ClubOption[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);

  const [drawerId, setDrawerId] = useState<number | null>(null);
  const [compare, setCompare] = useState<PlayerModel[]>([]);
  const [compareOpen, setCompareOpen] = useState(false);

  const selectedTour = useMemo(
    () => tours.find((t) => t.tour_id === tourId),
    [tours, tourId],
  );

  // Build club and status dropdowns from a single broad player fetch (all 16
  // clubs appear well within the first page). Refreshed when the tour changes.
  useEffect(() => {
    let active = true;
    api
      .listPlayers({ season_id: seasonId, order: "name", limit: 200 })
      .then((res) => {
        if (!active) return;
        const clubMap = new Map<number, string>();
        const statusSet = new Set<string>();
        for (const p of res.items) {
          if (p.club_id != null) clubMap.set(p.club_id, p.club_name ?? `#${p.club_id}`);
          if (p.availability_status) statusSet.add(p.availability_status);
        }
        setClubs(
          [...clubMap.entries()]
            .map(([id, name]) => ({ id, name }))
            .sort((a, b) => a.name.localeCompare(b.name, "ru")),
        );
        setStatuses([...statusSet].sort());
      })
      .catch(() => {
        /* dropdowns are best-effort; failures fall back to empty lists */
      });
    return () => {
      active = false;
    };
  }, [seasonId]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    api
      .listPlayers({
        season_id: seasonId,
        tour_id: tourId,
        model,
        order,
        role: filters.role || undefined,
        club_id: filters.clubId === "" ? undefined : filters.clubId,
        status: filters.status || undefined,
        min_price: filters.minPrice ? Number(filters.minPrice) : undefined,
        max_price: filters.maxPrice ? Number(filters.maxPrice) : undefined,
        limit: PAGE_SIZE,
        offset,
      })
      .then((res) => {
        if (!active) return;
        setData(res);
        if (res.snapshot) setSnapshot(res.snapshot);
      })
      .catch((err: unknown) => {
        if (active) {
          setError(err instanceof ApiError ? err.message : "Неизвестная ошибка");
          setData(null);
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [seasonId, tourId, model, order, filters, offset, reloadKey]);

  const updateFilter = useCallback(<K extends keyof Filters>(key: K, value: Filters[K]) => {
    setFilters((prev) => ({ ...prev, [key]: value }));
    setOffset(0);
  }, []);

  const toggleCompare = useCallback((player: PlayerModel) => {
    setCompare((prev) => {
      const exists = prev.some((p) => p.player_season_id === player.player_season_id);
      if (exists) {
        return prev.filter((p) => p.player_season_id !== player.player_season_id);
      }
      if (prev.length >= MAX_COMPARE) return prev;
      return [...prev, player];
    });
  }, []);

  const compareIds = compare.map((p) => p.player_season_id);
  const items = data?.items ?? [];
  const pagination = data?.pagination;

  return (
    <div>
      <div className="panel toolbar" data-testid="toolbar">
        <div className="field">
          <label htmlFor="tour">Тур</label>
          <select
            id="tour"
            value={tourId}
            onChange={(e) => {
              setTourId(Number(e.target.value));
              setOffset(0);
            }}
          >
            {tours.map((t) => (
              <option key={t.tour_id} value={t.tour_id}>
                {t.name} {t.status === "FINISHED" ? "✓" : ""}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="model">Модель</label>
          <select
            id="model"
            value={model}
            onChange={(e) => {
              setModel(e.target.value as ForecastModel);
              setOffset(0);
            }}
          >
            {MODELS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="role">Позиция</label>
          <select
            id="role"
            value={filters.role}
            onChange={(e) => updateFilter("role", e.target.value as Role | "")}
          >
            <option value="">Все</option>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {ROLE_LABELS[r]}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="club">Клуб</label>
          <select
            id="club"
            value={filters.clubId}
            onChange={(e) =>
              updateFilter("clubId", e.target.value ? Number(e.target.value) : "")
            }
          >
            <option value="">Все</option>
            {clubs.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="status">Статус</label>
          <select
            id="status"
            value={filters.status}
            onChange={(e) => updateFilter("status", e.target.value)}
          >
            <option value="">Любой</option>
            {statuses.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className="field field--price">
          <label htmlFor="minPrice">Цена от</label>
          <input
            id="minPrice"
            type="number"
            min={0}
            step={0.5}
            value={filters.minPrice}
            onChange={(e) => updateFilter("minPrice", e.target.value)}
          />
        </div>
        <div className="field field--price">
          <label htmlFor="maxPrice">Цена до</label>
          <input
            id="maxPrice"
            type="number"
            min={0}
            step={0.5}
            value={filters.maxPrice}
            onChange={(e) => updateFilter("maxPrice", e.target.value)}
          />
        </div>
        <button
          className="btn btn--ghost"
          onClick={() => {
            setFilters(EMPTY_FILTERS);
            setOffset(0);
          }}
        >
          Сбросить
        </button>
      </div>

      {selectedTour && (
        <p className="inline-note" style={{ margin: "0 0 12px" }}>
          Дедлайн трансферов:{" "}
          {formatDateTime(selectedTour.transfers_deadline_at ?? selectedTour.starts_at)}{" "}
          · лимит из клуба: {selectedTour.max_same_team_players ?? "—"} · трансферов:{" "}
          {selectedTour.total_transfers ?? "—"}
          {snapshot?.data_freshness && (
            <> · данные от {formatDateTime(snapshot.data_freshness)}</>
          )}
        </p>
      )}

      <div className="panel">
        {loading && <TableSkeleton />}
        {!loading && error && (
          <ErrorState message={error} onRetry={() => setReloadKey((k) => k + 1)} />
        )}
        {!loading && !error && items.length === 0 && (
          <EmptyState
            title="Игроки не найдены"
            hint="Измените фильтры или выберите другой тур."
          />
        )}
        {!loading && !error && items.length > 0 && (
          <>
            <PlayerTable
              players={items}
              order={order}
              onOrderChange={(o) => {
                setOrder(o);
                setOffset(0);
              }}
              onOpenPlayer={setDrawerId}
              compareIds={compareIds}
              onToggleCompare={toggleCompare}
              canCompareMore={compare.length < MAX_COMPARE}
            />
            {pagination && (
              <div className="pagination">
                <span>
                  {pagination.offset + 1}–
                  {pagination.offset + pagination.count} из {pagination.total}
                </span>
                <button
                  className="btn btn--sm"
                  disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  ← Назад
                </button>
                <button
                  className="btn btn--sm"
                  disabled={offset + pagination.count >= pagination.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Вперёд →
                </button>
              </div>
            )}
          </>
        )}
      </div>

      {compare.length > 0 && (
        <div className="compare-bar" data-testid="compare-bar">
          <div className="pills">
            {compare.map((p) => (
              <span className="pill" key={p.player_season_id}>
                {p.player_name ?? `#${p.player_season_id}`}
                <button
                  onClick={() => toggleCompare(p)}
                  aria-label={`Убрать ${p.player_name ?? ""}`}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
          <button className="btn btn--ghost btn--sm" onClick={() => setCompare([])}>
            Очистить
          </button>
          <button
            className="btn btn--primary"
            disabled={compare.length < 2}
            onClick={() => setCompareOpen(true)}
          >
            Сравнить ({compare.length})
          </button>
        </div>
      )}

      {drawerId != null && (
        <PlayerDrawer
          playerSeasonId={drawerId}
          tourId={tourId}
          model={model}
          onClose={() => setDrawerId(null)}
        />
      )}
      {compareOpen && compare.length >= 2 && (
        <CompareModal players={compare} onClose={() => setCompareOpen(false)} />
      )}
    </div>
  );
}
