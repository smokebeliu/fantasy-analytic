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
