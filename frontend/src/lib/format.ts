// Small display helpers shared across the UI.

export function formatPoints(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toFixed(digits);
}

export function formatPrice(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(1);
}

export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  // Ownership arrives as a 0..100 percentage; probabilities as 0..1.
  const scaled = value <= 1 ? value * 100 : value;
  return `${scaled.toFixed(1)}%`;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return new Intl.DateTimeFormat("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(date);
}

export function relativeFreshness(iso: string | null | undefined): string {
  if (!iso) return "нет данных";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const diffMs = Date.now() - date.getTime();
  const hours = Math.floor(diffMs / 3_600_000);
  if (hours < 1) return "менее часа назад";
  if (hours < 24) return `${hours} ч назад`;
  const days = Math.floor(hours / 24);
  return `${days} дн назад`;
}

export const COMPONENT_LABELS: Record<string, string> = {
  appearance: "За выход",
  goals: "Голы",
  assists: "Ассисты",
  clean_sheet: "Сухой матч",
  saves: "Сейвы",
  recoveries: "Отборы",
  conceded: "Пропущенные",
  yellow_cards: "Жёлтые карточки",
  red_cards: "Красные карточки",
};

export function componentLabel(key: string): string {
  return COMPONENT_LABELS[key] ?? key;
}
