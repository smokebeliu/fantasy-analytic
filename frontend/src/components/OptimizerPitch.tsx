"use client";

import type { HTMLAttributes } from "react";
import type {
  OptimizerCandidate,
  OptimizerResponse,
  OptimizerTransferPlayer,
  OptimizerTransfers,
  PlayerModel,
  Role,
} from "@/lib/types";
import { formatPoints, formatPrice } from "@/lib/format";
import { ROLE_LABELS, ROLES } from "@/lib/squad";
import { usePlayerHoverCard } from "./PlayerHoverCard";

// Read-only view of an optimizer answer: the starting eleven on the pitch, the
// ordered bench, and — in transfers mode — the swap plan.
//
// The solver reports candidates, which carry a price and a projection but not the
// season history a manager wants on hover. `playerIndex` supplies that from the
// player list the builder already loaded, keyed by player_season_id.

function signed(value: number, digits: number): string {
  return `${value >= 0 ? "+" : "−"}${Math.abs(value).toFixed(digits)}`;
}

/**
 * A signed number next to "budget" reads both ways — does +3.0 mean three more
 * spent or three more left over? Say which it is instead.
 */
function priceChange(delta: number): string {
  if (Math.abs(delta) < 0.05) return "цена та же";
  return delta > 0
    ? `дороже на ${delta.toFixed(1)}`
    : `дешевле на ${Math.abs(delta).toFixed(1)}`;
}

function describe(player: OptimizerTransferPlayer): string {
  const club = player.club_name;
  const role = player.role ? ROLE_LABELS[player.role] : null;
  const parts = [role, club].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : "нет данных за этот тур";
}

/** One "sell X, buy Y" row: the whole point of a transfer suggestion. */
function TransferPlan({
  transfers,
  playerIndex,
}: {
  transfers: OptimizerTransfers;
  playerIndex?: Map<number, PlayerModel>;
}) {
  const hover = usePlayerHoverCard();
  return (
    <div className="panel panel--pad" style={{ marginBottom: 14 }}>
      <strong>
        Замены: {transfers.made} из {transfers.allowed} доступных
      </strong>
      {transfers.made === 0 ? (
        <p className="inline-note" style={{ margin: "8px 0 0" }}>
          Состав уже оптимален для этого тура — менять никого не нужно.
        </p>
      ) : (
        <>
          <p className="inline-note" style={{ margin: "6px 0 0" }}>
            Слева — кого убрать, справа — кого взять вместо него.
          </p>
          <div className="transfer-plan" data-testid="transfer-plan">
            {transfers.pairs.map((pair) => (
              <div
                className="transfer-pair"
                key={`${pair.out.player_season_id}-${pair.in.player_season_id}`}
                data-testid="transfer-pair"
              >
                <div
                  className="transfer-pair__side"
                  {...hover.bind(playerIndex?.get(pair.out.player_season_id))}
                >
                  <span className="transfer-pair__name">
                    <span className="tag tag--out">OUT</span>{" "}
                    {pair.out.player_name ?? `#${pair.out.player_season_id}`}
                  </span>
                  <span className="transfer-pair__sub">
                    {describe(pair.out)}
                    {pair.out.price != null && ` · ${formatPrice(pair.out.price)}`}
                    {pair.out.expected_points != null &&
                      ` · прогноз ${formatPoints(pair.out.expected_points, 1)}`}
                  </span>
                </div>
                <span className="transfer-pair__arrow" aria-hidden="true">
                  →
                </span>
                <div
                  className="transfer-pair__side"
                  {...hover.bind(playerIndex?.get(pair.in.player_season_id))}
                >
                  <span className="transfer-pair__name">
                    <span className="tag tag--in">IN</span>{" "}
                    {pair.in.player_name ?? `#${pair.in.player_season_id}`}
                  </span>
                  <span className="transfer-pair__sub">
                    {describe(pair.in)}
                    {pair.in.price != null && ` · ${formatPrice(pair.in.price)}`}
                    {pair.in.expected_points != null &&
                      ` · прогноз ${formatPoints(pair.in.expected_points, 1)}`}
                  </span>
                </div>
                <div className="transfer-pair__delta">
                  <span className="gain">
                    {signed(pair.delta_expected_points, 1)} очк.
                  </span>
                  <span className="spend">{priceChange(pair.delta_price)}</span>
                </div>
              </div>
            ))}
          </div>
          {transfers.missing_from_pool.length > 0 && (
            <p className="inline-note" style={{ margin: "8px 0 0" }}>
              {transfers.missing_from_pool.length} игрок(ов) пришлось убрать
              принудительно: на этот тур у них нет матча или цены.
            </p>
          )}
          {hover.overlay}
        </>
      )}
    </div>
  );
}

function PitchPlayer({
  player,
  hoverProps,
}: {
  player: OptimizerCandidate;
  hoverProps?: HTMLAttributes<HTMLElement>;
}) {
  return (
    <div
      className={`pitch-player${player.is_locked ? " pitch-player--locked" : ""}`}
      tabIndex={0}
      {...hoverProps}
    >
      {player.is_locked && (
        <span className="pitch-player__pin is-locked" title="Закреплён пользователем">
          📌
        </span>
      )}
      <div className="nm">{player.player_name ?? `#${player.player_season_id}`}</div>
      <div className="pts">{formatPoints(player.expected_points, 1)}</div>
      {player.is_captain && <span className="badge-c">К</span>}
      {player.is_vice_captain && !player.is_captain && (
        <span className="badge-c" style={{ background: "var(--text-dim)" }}>
          ВК
        </span>
      )}
    </div>
  );
}

export function OptimizerPitch({
  result,
  playerIndex,
}: {
  result: OptimizerResponse;
  playerIndex?: Map<number, PlayerModel>;
}) {
  const hover = usePlayerHoverCard();
  const solution = result.solution;
  const starters = solution.starting;
  const byRole: Record<Role, OptimizerCandidate[]> = {
    GOALKEEPER: [],
    DEFENDER: [],
    MIDFIELDER: [],
    FORWARD: [],
  };
  for (const p of starters) byRole[p.role].push(p);
  for (const role of ROLES) {
    byRole[role].sort((a, b) => b.expected_points - a.expected_points);
  }

  const hoverProps = (candidate: OptimizerCandidate) =>
    hover.bind(playerIndex?.get(candidate.player_season_id));

  return (
    <div data-testid="optimizer-result">
      <div className="summary-metrics" style={{ gridTemplateColumns: "repeat(4, 1fr)" }}>
        <div className="metric">
          <div className="k">Ожид. очки</div>
          <div className="v" style={{ color: "var(--success)" }}>
            {formatPoints(solution.objective_expected_points, 1)}
          </div>
        </div>
        <div className="metric">
          <div className="k">Схема</div>
          <div className="v">{solution.formation}</div>
        </div>
        <div className="metric">
          <div className="k">Бюджет</div>
          <div className="v">{formatPrice(solution.total_price)}</div>
        </div>
        <div className="metric">
          <div className="k">Остаток</div>
          <div className="v">{formatPrice(solution.unused_budget)}</div>
        </div>
      </div>

      {solution.transfers && (
        <TransferPlan transfers={solution.transfers} playerIndex={playerIndex} />
      )}

      {solution.fixtures && solution.fixtures.clashes.length > 0 && (
        <div className="panel panel--pad" style={{ marginBottom: 14 }} data-testid="optimizer-clashes">
          <strong>
            Очные встречи в составе: {solution.fixtures.clashes.length}
            {typeof solution.fixture_penalty === "number" &&
              ` · штраф ${formatPoints(solution.fixture_penalty, 2)} очк.`}
          </strong>
          <div style={{ marginTop: 8 }}>
            {solution.fixtures.clashes.map((clash) => (
              <div key={`${clash.player_season_id}-${clash.opponent_player_season_id}`}>
                {clash.player_name ?? `#${clash.player_season_id}`} ({clash.club_name}) —{" "}
                {clash.opponent_player_name ?? `#${clash.opponent_player_season_id}`} (
                {clash.opponent_club_name}): −{formatPoints(clash.penalty, 2)}
              </div>
            ))}
          </div>
          <p className="inline-note" style={{ marginTop: 8 }}>
            Эти игроки играют друг против друга: голы одной стороны отнимают «сухарь» у
            другой, поэтому их совместный апсайд частично гасится.
          </p>
        </div>
      )}

      <div className="pitch">
        {ROLES.map((role) => (
          <div className="pitch-row" key={role}>
            {byRole[role].map((p) => (
              <PitchPlayer
                key={p.player_season_id}
                player={p}
                hoverProps={hoverProps(p)}
              />
            ))}
          </div>
        ))}
      </div>

      <div className="section-title" style={{ marginLeft: 0 }}>
        Скамейка
      </div>
      <div className="bench-strip">
        {solution.bench.map((p) => (
          <PitchPlayer
                key={p.player_season_id}
                player={p}
                hoverProps={hoverProps(p)}
              />
        ))}
      </div>
      <p className="inline-note" style={{ marginTop: 12 }}>
        Капитан: {solution.captain.player_name} · вице: {solution.vice_captain.player_name}{" "}
        · модель {result.model} · оптимизатор v{result.optimizer_version}
      </p>
      {solution.proven_optimal === false && (
        <p className="inline-note" data-testid="not-proven-optimal">
          Солвер не успел доказать оптимальность за отведённое время — это лучший
          найденный состав, возможно есть чуть лучше.
        </p>
      )}
      {hover.overlay}
    </div>
  );
}
