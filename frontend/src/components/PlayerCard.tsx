import type { PlayerDetailModel } from "@/lib/types";
import { RoleBadge, StatusDot } from "./badges";
import {
  componentLabel,
  formatDate,
  formatPercent,
  formatPoints,
  formatPrice,
} from "@/lib/format";
import { ROLE_LABELS } from "@/lib/squad";

function ForecastExplanation({ player }: { player: PlayerDetailModel }) {
  const projection = player.projection;
  if (!projection) {
    return (
      <p className="inline-note" style={{ padding: "0 20px" }}>
        Прогноз для выбранного тура ещё не рассчитан.
      </p>
    );
  }
  const components = projection.components ?? {};
  const entries = Object.entries(components).filter(([, v]) => Math.abs(v) > 1e-9);
  const maxAbs = Math.max(1e-6, ...entries.map(([, v]) => Math.abs(v)));

  return (
    <div data-testid="forecast-explanation">
      <div className="stat-grid">
        <div className="stat-box">
          <div className="k">Ожидаемые очки</div>
          <div className="v" style={{ color: "var(--success)" }}>
            {formatPoints(projection.expected_points, 2)}
          </div>
        </div>
        <div className="stat-box">
          <div className="k">Неопределённость ±</div>
          <div className="v">{formatPoints(projection.uncertainty, 2)}</div>
        </div>
        <div className="stat-box">
          <div className="k">P(выход)</div>
          <div className="v">{formatPercent(projection.p_appearance)}</div>
        </div>
        <div className="stat-box">
          <div className="k">Ожид. минуты</div>
          <div className="v">{formatPoints(projection.expected_minutes, 0)}</div>
        </div>
      </div>

      <div className="section-title">Из чего складывается прогноз</div>
      <div className="component-list">
        {entries.length === 0 && (
          <p className="inline-note">Нет значимых компонентов.</p>
        )}
        {entries
          .sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
          .map(([key, value]) => {
            const width = (Math.abs(value) / maxAbs) * 50;
            return (
              <div className="component-row" key={key}>
                <span>{componentLabel(key)}</span>
                <span className="component-bar">
                  <span
                    className={value >= 0 ? "pos" : "neg"}
                    style={{ width: `${width}%` }}
                  />
                </span>
                <span
                  className="component-val"
                  style={{ color: value >= 0 ? "var(--success)" : "var(--danger)" }}
                >
                  {value >= 0 ? "+" : ""}
                  {formatPoints(value, 2)}
                </span>
              </div>
            );
          })}
      </div>
      <p className="inline-note" style={{ padding: "10px 20px 0" }}>
        Модель {projection.model_name} · v{projection.model_version} · признаки{" "}
        {projection.feature_version ?? "—"} · начисление{" "}
        {projection.scoring_version ?? "—"}
      </p>
    </div>
  );
}

// What the player actually did last season. In the opening tours this is the
// only real evidence there is, and it stays useful later as the baseline the
// current season is compared against.
function PriorSeason({ player }: { player: PlayerDetailModel }) {
  const prior = player.prior_season;
  if (!prior) {
    return (
      <>
        <div className="section-title">Прошлый сезон</div>
        <p className="inline-note" style={{ padding: "0 20px" }}>
          Нет данных за прошлый сезон — игрок не выступал в РПЛ или сезон не
          загружен.
        </p>
      </>
    );
  }
  const cells: { label: string; value: string }[] = [
    { label: "Очки", value: String(prior.points ?? "—") },
    { label: "Средние очки", value: formatPoints(prior.average_points) },
    { label: "Рейтинг", value: prior.rank ? `#${prior.rank}` : "—" },
    { label: "Матчи", value: String(prior.matches ?? "—") },
    { label: "Минуты", value: String(prior.minutes ?? "—") },
    { label: "Голы", value: String(prior.goals ?? "—") },
    { label: "Ассисты", value: String(prior.assists ?? "—") },
    {
      label: player.role === "GOALKEEPER" ? "Сейвы" : "Отборы",
      value: String(
        (player.role === "GOALKEEPER" ? prior.saves : prior.ball_recoveries) ?? "—",
      ),
    },
  ];
  return (
    <>
      <div className="section-title">
        Прошлый сезон{prior.season_name ? ` · ${prior.season_name}` : ""}
      </div>
      <div className="stat-grid" data-testid="prior-season">
        {cells.map((cell) => (
          <div className="stat-box" key={cell.label}>
            <div className="k">{cell.label}</div>
            <div className="v">{cell.value}</div>
          </div>
        ))}
      </div>
      <p className="inline-note" style={{ padding: "8px 20px 0" }}>
        {prior.club_name ? `Клуб: ${prior.club_name}. ` : ""}
        {prior.price != null
          ? `Цена на конец сезона: ${formatPrice(prior.price)}.`
          : ""}
      </p>
    </>
  );
}

function HistoryTable({ player }: { player: PlayerDetailModel }) {
  if (player.history.length === 0) {
    return (
      <p className="inline-note" style={{ padding: "0 20px 20px" }}>
        Нет истории матчей.
      </p>
    );
  }
  return (
    <div className="table-wrap" style={{ padding: "0 20px 20px" }}>
      <table className="history-table">
        <thead>
          <tr>
            <th>Дата</th>
            <th>Очки</th>
            <th>Мин</th>
            <th>Г</th>
            <th>П</th>
            <th>Сейвы</th>
            <th>Отб</th>
            <th>ЖК</th>
            <th>КК</th>
          </tr>
        </thead>
        <tbody>
          {player.history.slice(0, 20).map((h) => (
            <tr key={h.match_id}>
              <td>{formatDate(h.scheduled_at)}</td>
              <td style={{ fontWeight: 700 }}>{h.points}</td>
              <td>{h.minutes}</td>
              <td>{h.goals}</td>
              <td>{h.assists}</td>
              <td>{h.saves}</td>
              <td>{h.ball_recoveries}</td>
              <td>{h.yellow_cards}</td>
              <td>{h.red_cards}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PlayerCard({ player }: { player: PlayerDetailModel }) {
  return (
    <div data-testid="player-card">
      <div className="card-header">
        <div>
          <h2>{player.player_name ?? `Игрок #${player.player_season_id}`}</h2>
          <div className="card-meta">
            <RoleBadge role={player.role} /> {ROLE_LABELS[player.role]} ·{" "}
            {player.club_name ?? "—"}
          </div>
        </div>
        <div style={{ textAlign: "right" }}>
          <div style={{ fontSize: 22, fontWeight: 700 }}>
            {formatPrice(player.price)}
          </div>
          <div className="card-meta">
            <StatusDot status={player.availability_status} />{" "}
            {player.status_description || player.availability_status || "—"}
          </div>
        </div>
      </div>

      <div className="stat-grid">
        <div className="stat-box">
          <div className="k">Очки за сезон</div>
          <div className="v">{player.season_score ?? "—"}</div>
        </div>
        <div className="stat-box">
          <div className="k">Средние очки</div>
          <div className="v">{formatPoints(player.average_score)}</div>
        </div>
        <div className="stat-box">
          <div className="k">Форма</div>
          <div className="v">{player.form ?? "—"}</div>
        </div>
        <div className="stat-box">
          <div className="k">Выбор менеджеров</div>
          <div className="v">{formatPercent(player.selected_by)}</div>
        </div>
      </div>

      <ForecastExplanation player={player} />

      <PriorSeason player={player} />

      <div className="section-title">История матчей</div>
      <HistoryTable player={player} />
    </div>
  );
}
