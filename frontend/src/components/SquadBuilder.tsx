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
  formationOptions,
  resolveSquadLimits,
  validateSquad,
} from "@/lib/squad";
import { formatPoints, formatPrice } from "@/lib/format";
import { RoleBadge } from "./badges";
import { OptimizerPitch } from "./OptimizerPitch";
import { usePlayerHoverCard } from "./PlayerHoverCard";
import { SquadPitch } from "./SquadPitch";
import { EmptyState, ErrorState, TableSkeleton } from "./StateBlocks";

const MODELS: ForecastModel[] = ["poisson_events", "season_mean", "recent_form"];

// The players endpoint caps a page at 200, and a season has a few hundred
// players; the pool is loaded whole so searching and the hover cards can reach
// every player rather than only the current position filter.
const FETCH_PAGE = 200;

type SquadView = "pitch" | "list";

// A solver candidate carries only what the objective needed: a price and a
// projection, never the season history. Loading a suggested squad into the
// builder therefore resolves each pick against the players already fetched, so
// the pitch and its hover cards keep the same numbers as the pool. The synthetic
// fallback only matters for a pick the pool does not contain.
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
  const [formation, setFormation] = useState("");
  const [locked, setLocked] = useState<Set<number>>(new Set());
  const [maxTransfers, setMaxTransfers] = useState<number | null>(null);

  const [pool, setPool] = useState<PlayerModel[]>([]);
  const [poolLoading, setPoolLoading] = useState(true);
  const [poolError, setPoolError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [selected, setSelected] = useState<PlayerModel[]>([]);

  const [result, setResult] = useState<OptimizerResponse | null>(null);
  const [optimizing, setOptimizing] = useState(false);
  const [optimizerError, setOptimizerError] = useState<string | null>(null);

  const poolHover = usePlayerHoverCard();

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

  const formations = useMemo(() => formationOptions(limits), [limits]);

  // The tour's own free-transfer allowance is the default and the maximum: the
  // solver does not price the penalty the game charges for extra transfers, so
  // suggesting more of them than the tour gives away would be misleading.
  const allowedTransfers = selectedTour?.total_transfers ?? 3;
  const transferLimit = Math.min(maxTransfers ?? allowedTransfers, allowedTransfers);

  // With nothing pinned and no formation chosen there is nothing for the
  // constrained run to preserve, so it would return the same squad as the
  // from-scratch one. Saying so is clearer than offering two identical buttons.
  const hasConstraints = locked.size > 0 || formation !== "";

  // The optimizer takes fantasy ids when they exist and internal ids otherwise.
  const lockedRefs = useMemo(
    () =>
      selected
        .filter((p) => locked.has(p.player_season_id))
        .map((p) => p.fantasy_player_id ?? String(p.player_season_id)),
    [selected, locked],
  );

  useEffect(() => {
    let active = true;
    setPoolLoading(true);
    setPoolError(null);

    const loadEveryPlayer = async () => {
      const all: PlayerModel[] = [];
      for (let page = 0; ; page += 1) {
        const res = await api.listPlayers({
          season_id: seasonId,
          tour_id: tourId,
          model,
          order: "projection",
          limit: FETCH_PAGE,
          offset: page * FETCH_PAGE,
        });
        all.push(...res.items);
        if (res.items.length < FETCH_PAGE || all.length >= res.pagination.total) {
          return all;
        }
      }
    };

    loadEveryPlayer()
      .then((items) => {
        if (active) setPool(items);
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
  }, [seasonId, tourId, model, reloadKey]);

  // Every loaded player by id: the source of the hover cards, which have to
  // describe optimizer picks and transfer candidates too, not just pool rows.
  const playerIndex = useMemo(
    () => new Map(pool.map((p) => [p.player_season_id, p])),
    [pool],
  );

  // The pool arrives asynchronously and reloads whenever the tour or the model
  // changes. Re-resolving the squad against it keeps the pitch and its hover
  // cards on the same numbers as the rest of the screen, instead of freezing
  // whatever happened to be known when the squad was assembled — a squad
  // generated before the pool finished loading would otherwise have no history at
  // all.
  useEffect(() => {
    if (playerIndex.size === 0) return;
    setSelected((prev) => {
      const next = prev.map((p) => playerIndex.get(p.player_season_id) ?? p);
      return next.some((p, i) => p !== prev[i]) ? next : prev;
    });
  }, [playerIndex]);

  const visiblePool = useMemo(() => {
    const q = search.trim().toLowerCase();
    return pool.filter(
      (p) =>
        (roleFilter ? p.role === roleFilter : true) &&
        (q ? (p.player_name ?? "").toLowerCase().includes(q) : true),
    );
  }, [pool, search, roleFilter]);

  const addPlayer = (player: PlayerModel) => {
    const check = canAddPlayer(player, selected, limits);
    if (!check.allowed) return;
    setSelected((prev) => [...prev, player]);
  };
  const removePlayer = (id: number) => {
    setSelected((prev) => prev.filter((p) => p.player_season_id !== id));
    setLocked((prev) => {
      if (!prev.has(id)) return prev;
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  };
  const toggleLock = (id: number) =>
    setLocked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const asPlayers = (squad: OptimizerCandidate[]) =>
    squad.map(
      (candidate) =>
        playerIndex.get(candidate.player_season_id) ?? candidateToPlayer(candidate),
    );

  // Keep the optimizer result loaded into the builder, preserving the pins that
  // survived (locked players always do) so the user can iterate.
  const applySolution = (res: OptimizerResponse) => {
    setResult(res);
    const players = asPlayers(res.solution.squad);
    setSelected(players);
    const stillPresent = new Set(players.map((p) => p.player_season_id));
    setLocked((prev) => new Set([...prev].filter((id) => stillPresent.has(id))));
  };

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
      setSelected(asPlayers(res.solution.squad));
      setLocked(new Set());
    } catch (err) {
      setOptimizerError(err instanceof ApiError ? err.message : "Ошибка оптимизатора");
    } finally {
      setOptimizing(false);
    }
  };

  // Step 15: keep the pinned players, fill the rest optimally and (optionally)
  // force the user's own formation.
  const runFillAroundLocked = async () => {
    if (!selectedTour) return;
    setOptimizing(true);
    setOptimizerError(null);
    setResult(null);
    try {
      const res = await api.optimizeSquad({
        tour: selectedTour.fantasy_tour_id,
        model,
        ...(lockedRefs.length > 0 ? { locked: lockedRefs } : {}),
        ...(formation ? { formation } : {}),
      });
      applySolution(res);
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
      setOptimizerError("У части выбранных игроков нет fantasy-id, замены недоступны.");
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
        max_transfers: transferLimit,
        ...(lockedRefs.length > 0 ? { locked: lockedRefs } : {}),
        ...(formation ? { formation } : {}),
      });
      // The suggested squad is *not* loaded into the builder: the point of a
      // transfer plan is to compare it with the squad the user actually owns, and
      // overwriting that squad would erase the left-hand side of the comparison.
      setResult(res);
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
        <div className="field">
          <label htmlFor="sq-formation">Схема</label>
          <select
            id="sq-formation"
            value={formation}
            onChange={(e) => setFormation(e.target.value)}
            data-testid="formation-select"
          >
            <option value="">Любая</option>
            {formations.map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="sq-transfers">Замен</label>
          <select
            id="sq-transfers"
            value={transferLimit}
            onChange={(e) => setMaxTransfers(Number(e.target.value))}
            data-testid="transfers-select"
          >
            {Array.from({ length: allowedTransfers }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
        <button
          className="btn btn--primary"
          onClick={runAutoSquad}
          disabled={optimizing || !selectedTour}
          data-testid="optimize-from-scratch"
          title="Полностью новый состав: текущий состав, закрепления и схема не учитываются"
        >
          {optimizing ? "Оптимизация…" : "Собрать состав с нуля"}
        </button>
        <button
          className="btn btn--primary"
          onClick={runFillAroundLocked}
          disabled={optimizing || !selectedTour || !hasConstraints}
          data-testid="optimize-locked"
          title={
            hasConstraints
              ? "Оставить закреплённых игроков и выбранную схему, остальные места заполнить оптимально"
              : "Закрепите игроков 📌 или выберите схему — иначе результат совпадёт с «Собрать состав с нуля»"
          }
        >
          Подобрать под мою схему
        </button>
      </div>

      {/* The two optimizer buttons differ only in what they are allowed to keep,
          which is impossible to guess from their labels alone. */}
      <p className="inline-note" style={{ margin: "0 0 14px" }} data-testid="optimizer-help">
        <strong>«Собрать состав с нуля»</strong> строит лучший состав тура заново и
        игнорирует всё, что вы выбрали.{" "}
        <strong>«Подобрать под мою схему»</strong> сохраняет закреплённых 📌 игроков
        и выбранную схему, а остальные места заполняет оптимально.{" "}
        <strong>«Оптимизировать замены»</strong> оставляет ваш состав и предлагает
        не больше {transferLimit} замен на тур, показывая кого на кого менять.
      </p>

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
                locked={locked}
                onToggleLock={toggleLock}
              />
            ) : selected.length > 0 ? (
              <div className="selected-list selected-list--full" data-testid="squad-list">
                {selected.map((p) => {
                  const isLocked = locked.has(p.player_season_id);
                  return (
                    <div className="selected-item" key={p.player_season_id}>
                      <RoleBadge role={p.role} />
                      <span className="grow">
                        {p.player_name ?? `#${p.player_season_id}`}
                      </span>
                      <span className="inline-note">{formatPrice(p.price)}</span>
                      <button
                        className={`pin-btn${isLocked ? " is-locked" : ""}`}
                        onClick={() => toggleLock(p.player_season_id)}
                        aria-pressed={isLocked}
                        aria-label={`${isLocked ? "Открепить" : "Закрепить"} ${
                          p.player_name ?? ""
                        }`}
                      >
                        📌
                      </button>
                      <button
                        className="icon-btn"
                        style={{ width: 24, height: 24, fontSize: 14 }}
                        onClick={() => removePlayer(p.player_season_id)}
                        aria-label="Убрать игрока"
                      >
                        ×
                      </button>
                    </div>
                  );
                })}
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

            <div className="locked-note" data-testid="locked-note">
              {locked.size > 0
                ? `Закреплено ${locked.size} из ${limits.totalPlayers} — «Подобрать под мою схему» оставит их и добёрет остальных.`
                : "Закрепите игроков «пином», чтобы оптимизатор оставил их и подобрал остальных."}
            </div>
            {selected.length > 0 && (
              <div className="inline-note" style={{ marginTop: 6 }}>
                Наведите курсор на игрока, чтобы увидеть его карточку: клуб, очки,
                среднее и прошлый сезон.
              </div>
            )}

            {validation.violations.length > 0 ? (
              <ul className="violations" data-testid="violations">
                {validation.violations.map((v) => (
                  <li key={v}>{v}</li>
                ))}
              </ul>
            ) : selected.length > 0 ? (
              <div className="valid-note" data-testid="valid-note">
                Состав корректен — можно подобрать замены.
              </div>
            ) : null}

            <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
              <button
                className="btn btn--primary"
                onClick={runTransfers}
                disabled={!validation.valid || optimizing}
                data-testid="optimize-transfers"
                title={`Оставить ваш состав и предложить не больше ${transferLimit} замен`}
              >
                {optimizing
                  ? "Оптимизация…"
                  : `Оптимизировать замены (${transferLimit})`}
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
                    <div
                      key={p.player_season_id}
                      className="pool-item"
                      data-testid="pool-row"
                      {...poolHover.bind(p)}
                    >
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
                {poolHover.overlay}
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
          {result && <OptimizerPitch result={result} playerIndex={playerIndex} />}
        </div>
      )}
    </div>
  );
}
