"use client";

import { useCallback, useState } from "react";
import type { HTMLAttributes, ReactNode } from "react";
import type { PlayerModel } from "@/lib/types";
import { formatPoints, formatPrice } from "@/lib/format";
import { ROLE_LABELS } from "@/lib/squad";
import { RoleBadge } from "./badges";

// Hovering a player anywhere — on the pitch, in the pool, in the table — shows a
// compact card with the numbers a manager decides on: who he is, what he costs,
// what he is projected to score, and what he actually did this season and last.
// Last season matters most in the opening tours, when the current-season columns
// are still nearly empty.
//
// The card is opened from a hook rather than by wrapping each player in a
// container element: the pitch and the table both lay their players out with
// flex and table cells, and an extra wrapper would disturb that. Callers spread
// `bind(player)` onto the element they already render and drop `overlay`
// somewhere inside the same component.

const CARD_WIDTH = 250;
const CARD_MAX_HEIGHT = 260;
const GAP = 10;

interface Anchor {
  player: PlayerModel;
  top: number;
  left: number;
  below: boolean;
}

function anchorFor(player: PlayerModel, element: HTMLElement): Anchor {
  const rect = element.getBoundingClientRect();
  // Prefer opening upwards; fall back to below when the viewport has no room,
  // and keep the card inside the horizontal edges either way.
  const below = rect.top < CARD_MAX_HEIGHT + GAP;
  const left = Math.min(
    Math.max(GAP, rect.left + rect.width / 2 - CARD_WIDTH / 2),
    Math.max(GAP, window.innerWidth - CARD_WIDTH - GAP),
  );
  return {
    player,
    left,
    top: below ? rect.bottom + GAP : rect.top - GAP,
    below,
  };
}

export function usePlayerHoverCard(): {
  bind: (player: PlayerModel | null | undefined) => HTMLAttributes<HTMLElement>;
  overlay: ReactNode;
} {
  const [anchor, setAnchor] = useState<Anchor | null>(null);

  const open = useCallback((player: PlayerModel, element: HTMLElement) => {
    setAnchor(anchorFor(player, element));
  }, []);
  const close = useCallback(() => setAnchor(null), []);

  const bind = useCallback(
    (player: PlayerModel | null | undefined): HTMLAttributes<HTMLElement> => {
      if (!player) return {};
      return {
        onMouseEnter: (event) => open(player, event.currentTarget as HTMLElement),
        onMouseLeave: close,
        // Keyboard users reach a player by tabbing to its controls, so the card
        // follows focus as well as the pointer.
        onFocus: (event) => open(player, event.currentTarget as HTMLElement),
        onBlur: close,
      };
    },
    [open, close],
  );

  return {
    bind,
    overlay: anchor ? <PlayerHoverCard anchor={anchor} /> : null,
  };
}

function PlayerHoverCard({ anchor }: { anchor: Anchor }) {
  const { player } = anchor;
  const prior = player.prior_season;
  return (
    <div
      className="hover-card"
      role="tooltip"
      data-testid="player-hover-card"
      style={{
        width: CARD_WIDTH,
        left: anchor.left,
        top: anchor.top,
        transform: anchor.below ? undefined : "translateY(-100%)",
      }}
    >
      <div className="hover-card__head">
        <div className="hover-card__name">
          {player.player_name ?? `Игрок #${player.player_season_id}`}
        </div>
        <div className="hover-card__meta">
          <RoleBadge role={player.role} /> {ROLE_LABELS[player.role]} ·{" "}
          {player.club_name ?? "клуб неизвестен"}
        </div>
      </div>

      <div className="hover-card__grid">
        <div>
          <span className="k">Цена</span>
          <span className="v">{formatPrice(player.price)}</span>
        </div>
        <div>
          <span className="k">Прогноз</span>
          <span className="v v--good">
            {formatPoints(player.projection?.expected_points, 1)}
          </span>
        </div>
        <div>
          <span className="k">Очки за сезон</span>
          <span className="v">{player.season_score ?? "—"}</span>
        </div>
        <div>
          <span className="k">В среднем</span>
          <span className="v">{formatPoints(player.average_score, 1)}</span>
        </div>
      </div>

      <div className="hover-card__section">
        Прошлый сезон{prior?.season_name ? ` · ${prior.season_name}` : ""}
      </div>
      {prior ? (
        <div className="hover-card__grid" data-testid="hover-card-prior">
          <div>
            <span className="k">Очки</span>
            <span className="v">{prior.points ?? "—"}</span>
          </div>
          <div>
            <span className="k">В среднем</span>
            <span className="v">{formatPoints(prior.average_points, 1)}</span>
          </div>
          <div>
            <span className="k">Рейтинг</span>
            <span className="v">{prior.rank ? `#${prior.rank}` : "—"}</span>
          </div>
          <div>
            <span className="k">Матчи</span>
            <span className="v">{prior.matches ?? "—"}</span>
          </div>
        </div>
      ) : (
        <p className="hover-card__empty">
          Нет данных за прошлый сезон — игрок в РПЛ впервые.
        </p>
      )}
    </div>
  );
}
