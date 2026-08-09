"use client";

import type { PlayerModel, PlayerOrder } from "@/lib/types";
import { RoleBadge, SourceBadge, StatusDot } from "./badges";
import { formatPercent, formatPoints, formatPrice } from "@/lib/format";

interface Column {
  key: string;
  label: string;
  order?: PlayerOrder;
  numeric?: boolean;
}

const COLUMNS: Column[] = [
  { key: "name", label: "Игрок", order: "name" },
  { key: "role", label: "Поз" },
  { key: "price", label: "Цена", order: "price", numeric: true },
  { key: "projection", label: "Прогноз", order: "projection", numeric: true },
  { key: "form", label: "Форма", numeric: true },
  { key: "season_score", label: "Очки", order: "season_score", numeric: true },
  { key: "selected_by", label: "Выбор", order: "selected_by", numeric: true },
  { key: "compare", label: "" },
];

export function PlayerTable({
  players,
  order,
  onOrderChange,
  onOpenPlayer,
  compareIds,
  onToggleCompare,
  canCompareMore,
}: {
  players: PlayerModel[];
  order: PlayerOrder;
  onOrderChange: (order: PlayerOrder) => void;
  onOpenPlayer: (id: number) => void;
  compareIds: number[];
  onToggleCompare: (player: PlayerModel) => void;
  canCompareMore: boolean;
}) {
  return (
    <div className="table-wrap">
      <table className="players">
        <thead>
          <tr>
            {COLUMNS.map((col) => {
              const isSorted = col.order && order === col.order;
              return (
                <th
                  key={col.key}
                  className={`${col.numeric ? "num" : ""} ${
                    col.order ? "sortable" : ""
                  }`}
                  onClick={() => col.order && onOrderChange(col.order)}
                  aria-sort={isSorted ? "descending" : undefined}
                >
                  {col.label}
                  {isSorted ? " ▾" : ""}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {players.map((p) => {
            const checked = compareIds.includes(p.player_season_id);
            return (
              <tr key={p.player_season_id} data-testid="player-row">
                <td>
                  <div className="player-name-cell">
                    <button
                      className="link-name"
                      onClick={() => onOpenPlayer(p.player_season_id)}
                    >
                      {p.player_name ?? `Игрок #${p.player_season_id}`}
                    </button>
                    <span className="club">
                      <StatusDot status={p.availability_status} />{" "}
                      {p.club_name ?? "—"}
                    </span>
                  </div>
                </td>
                <td>
                  <RoleBadge role={p.role} />
                </td>
                <td className="num">{formatPrice(p.price)}</td>
                <td className="num">
                  {p.projection?.expected_points != null ? (
                    <span className="proj-value">
                      {formatPoints(p.projection.expected_points, 1)}
                      <SourceBadge
                        statSource={p.projection.stat_source}
                        isNewcomer={
                          p.projection.stat_source === "prior_season" &&
                          p.projection.has_history === false
                        }
                      />
                    </span>
                  ) : (
                    "—"
                  )}
                </td>
                <td className="num">{p.form ?? "—"}</td>
                <td className="num">{p.season_score ?? "—"}</td>
                <td className="num">{formatPercent(p.selected_by)}</td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Сравнить ${p.player_name ?? ""}`}
                    checked={checked}
                    disabled={!checked && !canCompareMore}
                    onChange={() => onToggleCompare(p)}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
