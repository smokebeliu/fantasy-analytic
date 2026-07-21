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
import { EmptyState, ErrorState, TableSkeleton } from "./StateBlocks";

const MODELS: ForecastModel[] = ["poisson_events", "season_mean", "recent_form"];

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
    } catch (err) {
      setOptimizerError(err instanceof ApiError ? err.message : "Ошибка оптимизатора");
    } finally {
      setOptimizing(false);
    }
  };

  const fillFromResult = () => {
    if (!result) return;
    setSelected(result.solution.squad.map(candidateToPlayer));
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
        <div>
          <div className="panel toolbar" style={{ marginBottom: 12 }}>
            <div className="field">
              <label htmlFor="sq-role">Позиция</label>
              <select
                id="sq-role"
                value={roleFilter}
                onChange={(e) => setRoleFilter(e.target.value as Role | "")}
              >
                <option value="">Все</option>
                {ROLES.map((r) => (
                  <option key={r} value={r}>
                    {ROLE_LABELS[r]}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1 }}>
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

          <div className="panel">
            {poolLoading && <TableSkeleton rows={6} />}
            {!poolLoading && poolError && (
              <ErrorState message={poolError} onRetry={() => setReloadKey((k) => k + 1)} />
            )}
            {!poolLoading && !poolError && visiblePool.length === 0 && (
              <EmptyState title="Игроки не найдены" hint="Измените фильтр или поиск." />
            )}
            {!poolLoading && !poolError && visiblePool.length > 0 && (
              <div className="table-wrap">
                <table className="players">
                  <thead>
                    <tr>
                      <th>Игрок</th>
                      <th>Поз</th>
                      <th className="num">Цена</th>
                      <th className="num">Прогноз</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {visiblePool.slice(0, 80).map((p) => {
                      const check = canAddPlayer(p, selected, limits);
                      return (
                        <tr key={p.player_season_id} data-testid="pool-row">
                          <td>
                            <div className="player-name-cell">
                              <strong>{p.player_name ?? `#${p.player_season_id}`}</strong>
                              <span className="club">{p.club_name ?? "—"}</span>
                            </div>
                          </td>
                          <td>
                            <RoleBadge role={p.role} />
                          </td>
                          <td className="num">{formatPrice(p.price)}</td>
                          <td className="num">
                            <span className="proj-value">
                              {formatPoints(p.projection?.expected_points, 1)}
                            </span>
                          </td>
                          <td>
                            <button
                              className="add-btn"
                              onClick={() => addPlayer(p)}
                              disabled={!check.allowed}
                              title={check.reason ?? "Добавить"}
                              aria-label={`Добавить ${p.player_name ?? ""}`}
                            >
                              +
                            </button>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>

        <div className="squad-summary">
          <div className="panel panel--pad">
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

            {ROLES.map((role) => {
              const { min, max } = limits.roleLimits[role];
              const count = validation.roleCounts[role];
              const bad = count > max;
              return (
                <div
                  key={role}
                  className={`role-counter ${bad ? "bad" : "ok"}`}
                >
                  <span>
                    {ROLE_SHORT[role]} · {ROLE_LABELS[role]}
                  </span>
                  <span>
                    {count} <span className="inline-note">({min}–{max})</span>
                  </span>
                </div>
              );
            })}

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
            ) : (
              <p className="inline-note" style={{ marginTop: 12 }}>
                Добавьте игроков из списка слева или соберите автосостав.
              </p>
            )}

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
              {result && (
                <button className="btn btn--ghost btn--sm" onClick={fillFromResult}>
                  Загрузить состав из результата
                </button>
              )}
            </div>

            {selected.length > 0 && (
              <div className="selected-list">
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
