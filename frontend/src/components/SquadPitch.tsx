"use client";

import type { PlayerModel, Role } from "@/lib/types";
import type { SquadLimits } from "@/lib/squad";
import { ROLES, ROLE_SHORT } from "@/lib/squad";
import { formatPoints, formatPrice } from "@/lib/format";

// Editable formation view of the manually assembled squad. Players are laid out
// on the pitch by role (GK -> DEF -> MID -> FWD); empty role slots act as
// shortcuts that focus the pool on the missing position. Pinning a player marks
// them as locked so the optimizer has to keep them.
export function SquadPitch({
  selected,
  limits,
  onRemove,
  onEmptySlot,
  locked,
  onToggleLock,
}: {
  selected: PlayerModel[];
  limits: SquadLimits;
  onRemove: (id: number) => void;
  onEmptySlot?: (role: Role) => void;
  locked?: ReadonlySet<number>;
  onToggleLock?: (id: number) => void;
}) {
  const byRole: Record<Role, PlayerModel[]> = {
    GOALKEEPER: [],
    DEFENDER: [],
    MIDFIELDER: [],
    FORWARD: [],
  };
  for (const p of selected) byRole[p.role].push(p);

  // Only render placeholder slots when the per-role maxima add up to the full
  // roster size (the real RPL rules); otherwise the fallback default would draw
  // a nonsensical number of empty cards.
  const roleMaxSum = ROLES.reduce((sum, r) => sum + limits.roleLimits[r].max, 0);
  const showSlots = roleMaxSum === limits.totalPlayers;

  return (
    <div className="pitch pitch--editable" data-testid="squad-pitch">
      {ROLES.map((role) => {
        const players = byRole[role];
        const emptyCount = showSlots
          ? Math.max(0, limits.roleLimits[role].max - players.length)
          : 0;
        return (
          <div className="pitch-row" key={role}>
            {players.map((p) => {
              const isLocked = locked?.has(p.player_season_id) ?? false;
              return (
                <div
                  className={`pitch-player${isLocked ? " pitch-player--locked" : ""}`}
                  key={p.player_season_id}
                  title={p.club_name ?? ""}
                  data-testid="pitch-player"
                >
                  {onToggleLock && (
                    <button
                      className={`pitch-player__pin${isLocked ? " is-locked" : ""}`}
                      onClick={() => onToggleLock(p.player_season_id)}
                      aria-pressed={isLocked}
                      aria-label={`${isLocked ? "Открепить" : "Закрепить"} ${
                        p.player_name ?? ""
                      }`}
                      title={
                        isLocked
                          ? "Игрок закреплён: оптимизатор обязан его оставить"
                          : "Закрепить игрока в составе"
                      }
                    >
                      📌
                    </button>
                  )}
                  <button
                    className="pitch-player__remove"
                    onClick={() => onRemove(p.player_season_id)}
                    aria-label={`Убрать ${p.player_name ?? ""}`}
                  >
                    ×
                  </button>
                  <div className="nm">{p.player_name ?? `#${p.player_season_id}`}</div>
                  <div className="pts">
                    {formatPoints(p.projection?.expected_points, 1)}
                  </div>
                  <div className="pitch-player__price">{formatPrice(p.price)}</div>
                </div>
              );
            })}
            {Array.from({ length: emptyCount }).map((_, i) => (
              <button
                key={`empty-${role}-${i}`}
                className="pitch-player pitch-player--empty"
                onClick={() => onEmptySlot?.(role)}
                aria-label={`Добавить на позицию ${ROLE_SHORT[role]}`}
              >
                <span className="pitch-player__plus">+</span>
                <span className="pitch-player__slot-role">{ROLE_SHORT[role]}</span>
              </button>
            ))}
          </div>
        );
      })}
    </div>
  );
}
