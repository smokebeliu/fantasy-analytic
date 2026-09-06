import type { Role } from "@/lib/types";
import { ROLE_SHORT } from "@/lib/squad";

export function RoleBadge({ role }: { role: Role }) {
  return <span className={`role-badge role-${role}`}>{ROLE_SHORT[role]}</span>;
}

// Map the Sports.ru availability status to a coloured dot. Statuses are free
// text; a small allow-list decides the colour, everything else stays neutral.
const OK_STATUSES = new Set(["UNKNOWN", "FIERY", "AVAILABLE", ""]);
const BAD_STATUSES = new Set(["INJURY", "DISQUALIFICATION", "DISQUALIFIED", "SUSPENDED"]);

export function StatusDot({ status }: { status?: string | null }) {
  const value = (status ?? "").toUpperCase();
  let cls = "status-unknown";
  if (BAD_STATUSES.has(value)) cls = "status-bad";
  else if (value === "DOUBTFUL" || value === "WARNING") cls = "status-warn";
  else if (OK_STATUSES.has(value)) cls = "status-ok";
  return (
    <span
      className={`status-dot ${cls}`}
      title={status || "статус неизвестен"}
      aria-label={status || "статус неизвестен"}
    />
  );
}

// Label a projection whose numbers do not come from this season's play, so the
// analytical statistics of the previous and current season stay visually
// separated (development-plan step 14). A projection sourced from the current
// season shows nothing.
export function SourceBadge({
  statSource,
  isNewcomer,
}: {
  statSource?: string | null;
  isNewcomer?: boolean | null;
}) {
  if (isNewcomer) {
    return (
      <span
        className="source-badge source-newcomer"
        title="Новичок без истории — прогноз по прайорам позиции"
      >
        новичок
      </span>
    );
  }
  if (statSource === "prior_season") {
    return (
      <span
        className="source-badge source-prior"
        title="Прогноз построен по данным прошлого сезона"
      >
        прошлый сезон
      </span>
    );
  }
  // A cup row whose only play this season is in the player's national league
  // (step 24): the numbers are his, but from a different competition.
  if (statSource === "parallel_league") {
    return (
      <span
        className="source-badge source-parallel"
        title="Прогноз построен по текущему сезону национального чемпионата"
      >
        чемпионат
      </span>
    );
  }
  return null;
}
