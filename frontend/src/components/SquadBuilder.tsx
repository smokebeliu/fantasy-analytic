"use client";

import { useEffect, useMemo, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type {
  ForecastModel,
  OptimizerCandidate,
  OptimizerResponse,
  PlayerModel,
  Role,
  SeasonRulesModel,
  TourModel,
} from "@/lib/types";
import {
  ROLE_LABELS,
  ROLE_SHORT,
  ROLES,
  canAddPlayer,
  resolveSquadLimits,
  validateSquad,
} from "@/lib/squad";
import { formatPoints, formatPrice } from "@/lib/format";
import { RoleBadge } from "./badges";
import { OptimizerPitch } from "./OptimizerPitch";
import { SquadPitch } from "./SquadPitch";
import { EmptyState, ErrorState, TableSkeleton } from "./StateBlocks";

const MODELS: ForecastModel[] = ["poisson_events", "season_mean", "recent_form"];

type SquadView = "pitch" | "list";

function candidateToPlayer(c: OptimizerCandidate): PlayerModel {
  return {
    player_season_id: c.player_season_id,
    role: c.role,
    fantasy_player_id: c.fantasy_player_id ?? null,
    player_name: c.player_name ?? null,
    club_id: c.club_id,
    club_name: c.club_name ?? null,
    price: c.price,
    projection: { model_name: "", model_version: "", expected_points: c.expected_points },
  };
}

export function SquadBuilder({
  seasonId,
  tours,
  defaultTourId,
  rules,
}: {
  seasonId: number;
  tours: TourModel[];
  defaultTourId: number;
  rules: SeasonRulesModel | null;
}) {
  const [tourId, setTourId] = useState(defaultTourId);
  const [model, setModel] = useState<ForecastModel>("poisson_events");
  const [roleFilter, setRoleFilter] = useState<Role | "">("");
  const [search, setSearch] = useState("");
  const [view, setView] = useState<SquadView>("pitch");

  const [pool, setPool] = useState<PlayerModel[]>([]);
  const [poolLoading, setPoolLoading] = useState(true);
  const [poolError, setPoolError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [selected, setSelected] = useState<PlayerModel[]>([]);

  const [result, setResult] = useState<OptimizerResponse | null>(null);
  const [optimizing, setOptimizing] = useState(false);
  const [optimizerError, setOptimizerError] = useState<string | null>(null);

  const selectedTour = useMemo(
    () => tours.find((t) => t.tour_id === tourId),
    [tours, tourId],
  );

  const limits = useMemo(
    () => resolveSquadLimits(rules, selectedTour?.max_same_team_players),
    [rules, selectedTour],
  );

  const validation = useMemo(
    () => validateSquad(selected, limits),
    [selected, limits],
  );

  useEffect(() => {
    let active = true;
    setPoolLoading(true);
    setPoolError(null);
    api
      .listPlayers({
        season_id: seasonId,
        tour_id: tourId,
        model,
        role: roleFilter || undefined,
        order: "projection",
        limit: 200,
      })
      .then((res) => {
        if (active) setPool(res.items);
      })
      .catch((err: unknown) => {
        if (active)
          setPoolError(err instanceof ApiError ? err.message : "Ошибка загрузки");
      })
      .finally(() => {
        if (active) setPoolLoading(false);
      });
    return () => {
      active = false;
    };
  }, [seasonId, tourId, model, roleFilter, reloadKey]);

  const visiblePool = useMemo(() => {
    const q = search.trim().toLowerCase();
    return pool.filter((p) =>
      q ? (p.player_name ?? "").toLowerCase().includes(q) : true,
    );
  }, [pool, search]);

  const addPlayer = (player: PlayerModel) => {
    const check = canAddPlayer(player, selected, limits);
    if (!check.allowed) return;
    setSelected((prev) => [...prev, player]);
  };
  const removePlayer = (id: number) =>
    setSelected((prev) => prev.filter((p) => p.player_season_id !== id));

  const runAutoSquad = async () => {
    if (!selectedTour) return;
    setOptimizing(true);
    setOptimizerError(null);
    setResult(null);
    try {
      const res = await api.optimizeSquad({
        tour: selectedTour.fantasy_tour_id,
        model,
      });
      setResult(res);
      // Load the optimal roster into the builder so the summary stays in sync.
      setSelected(res.solution.squad.map(candidateToPlayer));
    } catch (err) {
      setOptimizerError(err instanceof ApiError ? err.message : "Ошибка оптимизатора");
    } finally {
      setOptimizing(false);
    }
  };

  const runTransfers = async () => {
    if (!selectedTour || !validation.valid) return;
    const currentSquad = selected
      .map((p) => p.fantasy_player_id)
      .filter((id): id is string => Boolean(id));
    if (currentSquad.length !== selected.length) {
      setOptimizerError("У части выбранных игроков нет fantasy-id, трансферы недоступны.");
      return;
    }
    setOptimizing(true);
    setOptimizerError(null);
    setResult(null);
    try {
      const res = await api.optimizeTransfers({
        current_squad: currentSquad,
        tour: selectedTour.fantasy_tour_id,
        model,
      });
      setResult(res);
      setSelected(res.solution.squad.map(candidateToPlayer));
    } catch (err) {
      setOptimizerError(err instanceof ApiError ? err.message : "Ошибка оптимизатора");
    } finally {
      setOptimizing(false);
    }
  };

  return (
    <div>
      <div className="panel toolbar">
        <div className="field">
          <label htmlFor="sq-tour">Тур</label>
          <select
            id="sq-tour"
            value={tourId}
            onChange={(e) => setTourId(Number(e.target.value))}
          >
            {tours.map((t) => (
              <option key={t.tour_id} value={t.tour_id}>
                {t.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="sq-model">Модель</label>
          <select
            id="sq-model"
            value={model}
            onChange={(e) => setModel(e.target.value as ForecastModel)}
          >
            {MODELS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
        <button
          className="btn btn--primary"
          onClick={runAutoSquad}
          disabled={optimizing || !selectedTour}
        >
          {optimizing ? "Оптимизация…" : "Автосостав (оптимизатор)"}
        </button>
      </div>

      <div className="squad-layout">
        {/* Left column: the current squad as an editable pitch / list. */}
        <div className="squad-current">
          <div className="panel panel--pad">
            <div className="squad-head">
              <div className="summary-metrics">
                <div className="metric">
                  <div className="k">Игроков</div>
                  <div
                    className={`v ${
                      selected.length === limits.totalPlayers ? "" : "over"
                    }`}
                  >
                    {selected.length}/{limits.totalPlayers}
                  </div>
                </div>
                <div className="metric">
                  <div className="k">Бюджет</div>
                  <div className={`v ${validation.overBudget ? "over" : ""}`}>
                    {formatPrice(validation.totalPrice)}/{formatPrice(limits.totalBudget)}
                  </div>
                </div>
              </div>

              <div
                className="role-group view-toggle"
                role="group"
                aria-label="Вид состава"
              >
                <button
                  className={view === "pitch" ? "active" : ""}
                  aria-pressed={view === "pitch"}
                  onClick={() => setView("pitch")}
                >
                  Схема
                </button>
                <button
                  className={view === "list" ? "active" : ""}
                  aria-pressed={view === "list"}
                  onClick={() => setView("list")}
                >
                  Список
                </button>
              </div>
            </div>

            {view === "pitch" ? (
              <SquadPitch
                selected={selected}
                limits={limits}
                onRemove={removePlayer}
                onEmptySlot={(role) => setRoleFilter(role)}
              />
            ) : selected.length > 0 ? (
              <div className="selected-list selected-list--full" data-testid="squad-list">
                {selected.map((p) => (
                  <div className="selected-item" key={p.player_season_id}>
                    <RoleBadge role={p.role} />
                    <span className="grow">
                      {p.player_name ?? `#${p.player_season_id}`}
                    </span>
                    <span className="inline-note">{formatPrice(p.price)}</span>
                    <button
                      className="icon-btn"
                      style={{ width: 24, height: 24, fontSize: 14 }}
                      onClick={() => removePlayer(p.player_season_id)}
                      aria-label="Убрать игрока"
                    >
                      ×
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="Состав пуст"
                hint="Добавьте игроков из списка справа или соберите автосостав."
              />
            )}

            <div className="role-counters">
              {ROLES.map((role) => {
                const { min, max } = limits.roleLimits[role];
                const count = validation.roleCounts[role];
                const bad = count > max;
                return (
                  <div key={role} className={`role-counter ${bad ? "bad" : "ok"}`}>
                    <span>
                      {ROLE_SHORT[role]} · {ROLE_LABELS[role]}
                    </span>
                    <span>
                      {count} <span className="inline-note">({min}–{max})</span>
                    </span>
                  </div>
                );
              })}
            </div>

            {validation.violations.length > 0 ? (
              <ul className="violations" data-testid="violations">
                {validation.violations.map((v) => (
                  <li key={v}>{v}</li>
                ))}
              </ul>
            ) : selected.length > 0 ? (
              <div className="valid-note" data-testid="valid-note">
                Состав корректен — можно оптимизировать трансферы.
              </div>
            ) : null}

            <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
              <button
                className="btn btn--primary"
                onClick={runTransfers}
                disabled={!validation.valid || optimizing}
                data-testid="optimize-transfers"
              >
                Оптимизировать трансферы
              </button>
              {selected.length > 0 && (
                <button
                  className="btn btn--ghost btn--sm btn--danger"
                  onClick={() => setSelected([])}
                >
                  Очистить
                </button>
              )}
            </div>
          </div>
        </div>

        {/* Right column: search + full list of all players. */}
        <div className="squad-pool">
          <div className="panel panel--pad squad-pool__filters">
            <div
              className="role-group"
              role="group"
              aria-label="Фильтр по позиции"
              data-testid="role-filter"
            >
              <button
                className={roleFilter === "" ? "active" : ""}
                aria-pressed={roleFilter === ""}
                onClick={() => setRoleFilter("")}
              >
                Все
              </button>
              {ROLES.map((r) => (
                <button
                  key={r}
                  className={roleFilter === r ? "active" : ""}
                  aria-pressed={roleFilter === r}
                  title={ROLE_LABELS[r]}
                  onClick={() => setRoleFilter(r)}
                >
                  {ROLE_SHORT[r]}
                </button>
              ))}
            </div>
            <div className="field squad-pool__search">
              <label htmlFor="sq-search">Поиск</label>
              <input
                id="sq-search"
                type="text"
                placeholder="Имя игрока…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </div>
          </div>

          <div className="panel panel--pad">
            {poolLoading && <TableSkeleton rows={6} />}
            {!poolLoading && poolError && (
              <ErrorState message={poolError} onRetry={() => setReloadKey((k) => k + 1)} />
            )}
            {!poolLoading && !poolError && visiblePool.length === 0 && (
              <EmptyState title="Игроки не найдены" hint="Измените фильтр или поиск." />
            )}
            {!poolLoading && !poolError && visiblePool.length > 0 && (
              <div className="pool-list">
                {visiblePool.slice(0, 80).map((p) => {
                  const check = canAddPlayer(p, selected, limits);
                  return (
                    <div key={p.player_season_id} className="pool-item" data-testid="pool-row">
                      <RoleBadge role={p.role} />
                      <div className="pool-item__main">
                        <span className="pool-item__name">
                          {p.player_name ?? `#${p.player_season_id}`}
                        </span>
                        <span className="pool-item__club">{p.club_name ?? "—"}</span>
                      </div>
                      <span className="pool-item__price">{formatPrice(p.price)}</span>
                      <span className="pool-item__proj">
                        {formatPoints(p.projection?.expected_points, 1)}
                      </span>
                      <button
                        className="add-btn"
                        onClick={() => addPlayer(p)}
                        disabled={!check.allowed}
                        title={check.reason ?? "Добавить"}
                        aria-label={`Добавить ${p.player_name ?? ""}`}
                      >
                        +
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </div>

      {(optimizerError || result) && (
        <div className="panel panel--pad" style={{ marginTop: 18 }}>
          <div className="section-title" style={{ margin: "0 0 12px" }}>
            Результат оптимизатора
          </div>
          {optimizerError && <div className="error-inline">{optimizerError}</div>}
          {result && <OptimizerPitch result={result} />}
        </div>
      )}
    </div>
  );
}
