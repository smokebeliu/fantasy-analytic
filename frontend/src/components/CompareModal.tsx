"use client";

import type { PlayerModel } from "@/lib/types";
import { RoleBadge } from "./badges";
import {
  componentLabel,
  formatPercent,
  formatPoints,
  formatPrice,
} from "@/lib/format";

type NumericGetter = (p: PlayerModel) => number | null | undefined;

interface Row {
  label: string;
  get: NumericGetter;
  digits?: number;
  higherIsBetter?: boolean;
  render?: (p: PlayerModel) => string;
}

const ROWS: Row[] = [
  {
    label: "Прогноз очков",
    get: (p) => p.projection?.expected_points,
    digits: 2,
    higherIsBetter: true,
  },
  {
    label: "± неопределённость",
    get: (p) => p.projection?.uncertainty,
    digits: 2,
    higherIsBetter: false,
  },
  { label: "Цена", get: (p) => p.price, render: (p) => formatPrice(p.price) },
  {
    label: "Очки за сезон",
    get: (p) => p.season_score,
    digits: 0,
    higherIsBetter: true,
  },
  {
    label: "Средние очки",
    get: (p) => p.average_score,
    digits: 1,
    higherIsBetter: true,
  },
  { label: "Форма", get: (p) => p.form, digits: 0, higherIsBetter: true },
  {
    label: "Выбор менеджеров",
    get: (p) => p.selected_by,
    higherIsBetter: true,
    render: (p) => formatPercent(p.selected_by),
  },
  {
    label: "P(выход)",
    get: (p) => p.projection?.p_appearance,
    higherIsBetter: true,
    render: (p) => formatPercent(p.projection?.p_appearance),
  },
];

function bestIndex(players: PlayerModel[], row: Row): number | null {
  if (row.higherIsBetter === undefined) return null;
  let best: number | null = null;
  let bestVal: number | null = null;
  players.forEach((p, i) => {
    const v = row.get(p);
    if (v == null || Number.isNaN(v)) return;
    if (
      bestVal == null ||
      (row.higherIsBetter ? v > bestVal : v < bestVal)
    ) {
      bestVal = v;
      best = i;
    }
  });
  return best;
}

function componentKeys(players: PlayerModel[]): string[] {
  const keys = new Set<string>();
  for (const p of players) {
    for (const key of Object.keys(p.projection?.components ?? {})) keys.add(key);
  }
  return [...keys];
}

export function CompareModal({
  players,
  onClose,
}: {
  players: PlayerModel[];
  onClose: () => void;
}) {
  const compKeys = componentKeys(players);

  return (
    <div className="modal-overlay" onClick={onClose} role="dialog" aria-modal="true">
      <div
        className="modal"
        onClick={(e) => e.stopPropagation()}
        data-testid="compare-modal"
      >
        <div className="modal__head">
          <h2>Сравнение игроков</h2>
          <button className="icon-btn" onClick={onClose} aria-label="Закрыть">
            ×
          </button>
        </div>
        <div className="modal__body">
          <div className="table-wrap">
            <table className="compare-table">
              <thead>
                <tr>
                  <th>Показатель</th>
                  {players.map((p) => (
                    <th key={p.player_season_id}>
                      <div>{p.player_name ?? `#${p.player_season_id}`}</div>
                      <div style={{ marginTop: 4 }}>
                        <RoleBadge role={p.role} />{" "}
                        <span className="inline-note">{p.club_name ?? ""}</span>
                      </div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {ROWS.map((row) => {
                  const best = bestIndex(players, row);
                  return (
                    <tr key={row.label}>
                      <td>{row.label}</td>
                      {players.map((p, i) => {
                        const text = row.render
                          ? row.render(p)
                          : formatPoints(row.get(p), row.digits ?? 1);
                        return (
                          <td
                            key={p.player_season_id}
                            className={best === i ? "best" : ""}
                          >
                            {text}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
                {compKeys.length > 0 && (
                  <tr>
                    <td
                      colSpan={players.length + 1}
                      style={{
                        color: "var(--text-dim)",
                        textTransform: "uppercase",
                        fontSize: 11,
                        textAlign: "left",
                        paddingTop: 16,
                      }}
                    >
                      Компоненты прогноза
                    </td>
                  </tr>
                )}
                {compKeys.map((key) => {
                  const row: Row = {
                    label: componentLabel(key),
                    get: (p) => p.projection?.components?.[key],
                    digits: 2,
                  };
                  return (
                    <tr key={key}>
                      <td>{componentLabel(key)}</td>
                      {players.map((p) => (
                        <td key={p.player_season_id}>
                          {formatPoints(p.projection?.components?.[key], 2)}
                        </td>
                      ))}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
