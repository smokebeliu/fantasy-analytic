"use client";

import type { OptimizerCandidate, OptimizerResponse, Role } from "@/lib/types";
import { formatPoints, formatPrice } from "@/lib/format";
import { ROLES } from "@/lib/squad";

function PitchPlayer({ player }: { player: OptimizerCandidate }) {
  return (
    <div className="pitch-player" title={`${player.club_name ?? ""}`}>
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

export function OptimizerPitch({ result }: { result: OptimizerResponse }) {
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
        <div className="panel panel--pad" style={{ marginBottom: 14 }}>
          <strong>
            Трансферы: {solution.transfers.made} из {solution.transfers.allowed}
          </strong>
          <div style={{ marginTop: 8, display: "flex", gap: 16, flexWrap: "wrap" }}>
            <div>
              {solution.transfers.in.map((p) => (
                <div key={p.player_season_id}>
                  <span className="tag tag--in">IN</span>{" "}
                  {p.player_name ?? `#${p.player_season_id}`}
                </div>
              ))}
            </div>
            <div>
              {solution.transfers.out.map((id) => (
                <div key={id}>
                  <span className="tag tag--out">OUT</span> #{id}
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      <div className="pitch">
        {ROLES.map((role) => (
          <div className="pitch-row" key={role}>
            {byRole[role].map((p) => (
              <PitchPlayer key={p.player_season_id} player={p} />
            ))}
          </div>
        ))}
      </div>

      <div className="section-title" style={{ marginLeft: 0 }}>
        Скамейка
      </div>
      <div className="bench-strip">
        {solution.bench.map((p) => (
          <div className="pitch-player" key={p.player_season_id}>
            <div className="nm">{p.player_name ?? `#${p.player_season_id}`}</div>
            <div className="pts">{formatPoints(p.expected_points, 1)}</div>
          </div>
        ))}
      </div>
      <p className="inline-note" style={{ marginTop: 12 }}>
        Капитан: {solution.captain.player_name} · вице: {solution.vice_captain.player_name}{" "}
        · модель {result.model} · оптимизатор v{result.optimizer_version}
      </p>
    </div>
  );
}
