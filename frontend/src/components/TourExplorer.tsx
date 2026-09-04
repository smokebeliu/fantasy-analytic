"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type {
  ForecastModel,
  PlayerModel,
  PlayerOrder,
  Role,
  SnapshotMeta,
  TourModel,
} from "@/lib/types";
import { ROLE_LABELS, ROLES } from "@/lib/squad";
import { sortPlayers } from "@/lib/players";
import { PlayerTable } from "./PlayerTable";
import { PlayerDrawer } from "./PlayerDrawer";
import { CompareModal } from "./CompareModal";
import { EmptyState, ErrorState, TableSkeleton } from "./StateBlocks";
import { formatDateTime } from "@/lib/format";

const PAGE_SIZE = 50;
const MAX_COMPARE = 4;
// The read API caps a page at 200 rows, so the full season working set is loaded
// in a few requests and then sorted/paginated on the client.
const FETCH_PAGE = 200;
const MODELS: ForecastModel[] = ["poisson_events", "season_mean", "recent_form", "ridge_stack"];

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
  // Projections are tour-specific; with the tour filter removed the table shows
  // full-season stats and implicitly projects the upcoming (default) tour.
  const projectionTourId = defaultTourId;
  const [model, setModel] = useState<ForecastModel>("poisson_events");
  const [order, setOrder] = useState<PlayerOrder>("season_score");
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [offset, setOffset] = useState(0);

  const [players, setPlayers] = useState<PlayerModel[]>([]);
  const [snapshot, setSnapshot] = useState<SnapshotMeta | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [clubs, setClubs] = useState<ClubOption[]>([]);
  const [statuses, setStatuses] = useState<string[]>([]);

  const [drawerId, setDrawerId] = useState<number | null>(null);
  const [compare, setCompare] = useState<PlayerModel[]>([]);
  const [compareOpen, setCompareOpen] = useState(false);

  const projectionTour = useMemo(
    () => tours.find((t) => t.tour_id === projectionTourId),
    [tours, projectionTourId],
  );

  // Build club and status dropdowns from a single broad player fetch (all 16
  // clubs appear well within the first page). Refreshed when the season changes.
  useEffect(() => {
    let active = true;
    api
      .listPlayers({ season_id: seasonId, order: "name", limit: FETCH_PAGE })
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

  // Load the whole filtered working set once; sorting and pagination then happen
  // on the client so switching the sort column never triggers a server request.
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);

    (async () => {
      try {
        const acc: PlayerModel[] = [];
        let snap: SnapshotMeta | null = null;
        let page = 0;
        for (;;) {
          const res = await api.listPlayers({
            season_id: seasonId,
            tour_id: projectionTourId,
            model,
            order: "name",
            role: filters.role || undefined,
            club_id: filters.clubId === "" ? undefined : filters.clubId,
            status: filters.status || undefined,
            min_price: filters.minPrice ? Number(filters.minPrice) : undefined,
            max_price: filters.maxPrice ? Number(filters.maxPrice) : undefined,
            limit: FETCH_PAGE,
            offset: page * FETCH_PAGE,
          });
          acc.push(...res.items);
          if (res.snapshot) snap = res.snapshot;
          page += 1;
          if (res.items.length === 0 || acc.length >= res.pagination.total) break;
        }
        if (!active) return;
        setPlayers(acc);
        if (snap) setSnapshot(snap);
        setOffset(0);
      } catch (err: unknown) {
        if (!active) return;
        setError(err instanceof ApiError ? err.message : "Неизвестная ошибка");
        setPlayers([]);
      } finally {
        if (active) setLoading(false);
      }
    })();

    return () => {
      active = false;
    };
  }, [seasonId, projectionTourId, model, filters, reloadKey]);

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

  const sorted = useMemo(() => sortPlayers(players, order), [players, order]);
  const pageItems = useMemo(
    () => sorted.slice(offset, offset + PAGE_SIZE),
    [sorted, offset],
  );

  const compareIds = compare.map((p) => p.player_season_id);
  const total = players.length;

  return (
    <div>
      <div className="panel toolbar" data-testid="toolbar">
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

      <p className="inline-note" style={{ margin: "0 0 12px" }}>
        Полная статистика сезона на текущий момент.
        {projectionTour && (
          <> Прогноз в колонке «Прогноз» рассчитан на ближайший тур: {projectionTour.name}.</>
        )}
        {snapshot?.data_freshness && (
          <> Данные от {formatDateTime(snapshot.data_freshness)}.</>
        )}
      </p>

      <div className="panel">
        {loading && <TableSkeleton />}
        {!loading && error && (
          <ErrorState message={error} onRetry={() => setReloadKey((k) => k + 1)} />
        )}
        {!loading && !error && total === 0 && (
          <EmptyState
            title="Игроки не найдены"
            hint="Измените фильтры, чтобы увидеть больше игроков."
          />
        )}
        {!loading && !error && total > 0 && (
          <>
            <PlayerTable
              players={pageItems}
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
            <div className="pagination">
              <span>
                {offset + 1}–{offset + pageItems.length} из {total}
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
                disabled={offset + PAGE_SIZE >= total}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                Вперёд →
              </button>
            </div>
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
          tourId={projectionTourId}
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
