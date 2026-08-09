import type { PlayerModel, Role, SeasonRulesModel } from "./types";

export const ROLES: Role[] = ["GOALKEEPER", "DEFENDER", "MIDFIELDER", "FORWARD"];

export const ROLE_LABELS: Record<Role, string> = {
  GOALKEEPER: "Вратари",
  DEFENDER: "Защитники",
  MIDFIELDER: "Полузащитники",
  FORWARD: "Нападающие",
};

export const ROLE_SHORT: Record<Role, string> = {
  GOALKEEPER: "ВРТ",
  DEFENDER: "ЗАЩ",
  MIDFIELDER: "ПЗЩ",
  FORWARD: "НАП",
};

interface RoleConstraint {
  role?: string;
  minCount?: number | null;
  maxCount?: number | null;
}

export interface SquadLimits {
  totalBudget: number;
  totalPlayers: number;
  startingPlayers: number;
  maxSameTeam: number;
  roleLimits: Record<Role, { min: number; max: number }>;
  startingRoleLimits: Record<Role, { min: number; max: number }>;
}

// Parse the API roster-constraint list ({role, minCount, maxCount}) into a
// role -> {min, max} map, mirroring optimizer.parse_role_limits on the backend.
export function parseRoleLimits(
  constraints: unknown,
  totalPlayers: number,
): Record<Role, { min: number; max: number }> {
  const limits: Record<Role, { min: number; max: number }> = {
    GOALKEEPER: { min: 0, max: totalPlayers },
    DEFENDER: { min: 0, max: totalPlayers },
    MIDFIELDER: { min: 0, max: totalPlayers },
    FORWARD: { min: 0, max: totalPlayers },
  };
  if (!Array.isArray(constraints)) return limits;
  for (const raw of constraints as RoleConstraint[]) {
    const role = raw?.role as Role | undefined;
    if (!role || !ROLES.includes(role)) continue;
    limits[role] = {
      min: raw.minCount != null ? Number(raw.minCount) : 0,
      max: raw.maxCount != null ? Number(raw.maxCount) : totalPlayers,
    };
  }
  return limits;
}

export function resolveSquadLimits(
  rules: SeasonRulesModel | null | undefined,
  maxSameTeam: number | null | undefined,
): SquadLimits {
  const totalPlayers = rules?.total_players ?? 15;
  const startingPlayers = rules?.starting_players ?? 11;
  return {
    totalBudget: rules?.total_budget ?? 100,
    totalPlayers,
    startingPlayers,
    maxSameTeam: maxSameTeam ?? totalPlayers,
    roleLimits: parseRoleLimits(rules?.full_roster_constraints, totalPlayers),
    startingRoleLimits: parseRoleLimits(
      rules?.starting_roster_constraints,
      startingPlayers,
    ),
  };
}

// Enumerate every "defenders-midfielders-forwards" formation the season rules
// allow, mirroring optimizer.parse_formation on the backend: goalkeepers take
// whatever is left of the starting eleven.
export function formationOptions(limits: SquadLimits): string[] {
  const bound = (role: Role) => limits.startingRoleLimits[role];
  const options: string[] = [];
  for (let def = bound("DEFENDER").min; def <= bound("DEFENDER").max; def += 1) {
    for (let mid = bound("MIDFIELDER").min; mid <= bound("MIDFIELDER").max; mid += 1) {
      for (let fwd = bound("FORWARD").min; fwd <= bound("FORWARD").max; fwd += 1) {
        const keepers = limits.startingPlayers - def - mid - fwd;
        const gk = bound("GOALKEEPER");
        if (keepers < gk.min || keepers > gk.max) continue;
        options.push(`${def}-${mid}-${fwd}`);
      }
    }
  }
  return options;
}

export interface SquadValidation {
  valid: boolean;
  violations: string[];
  totalPrice: number;
  budgetRemaining: number;
  roleCounts: Record<Role, number>;
  clubCounts: Record<number, number>;
  overBudget: boolean;
}

// Validate a manually assembled squad against every roster rule. Returns the
// full breakdown so the UI can both block submission and explain each gap.
export function validateSquad(
  players: PlayerModel[],
  limits: SquadLimits,
): SquadValidation {
  const violations: string[] = [];
  const roleCounts: Record<Role, number> = {
    GOALKEEPER: 0,
    DEFENDER: 0,
    MIDFIELDER: 0,
    FORWARD: 0,
  };
  const clubCounts: Record<number, number> = {};
  let totalPrice = 0;

  const seen = new Set<number>();
  for (const player of players) {
    if (seen.has(player.player_season_id)) {
      violations.push("В составе есть повторяющиеся игроки.");
      continue;
    }
    seen.add(player.player_season_id);
    roleCounts[player.role] += 1;
    if (player.club_id != null) {
      clubCounts[player.club_id] = (clubCounts[player.club_id] ?? 0) + 1;
    }
    totalPrice += player.price ?? 0;
  }

  totalPrice = Math.round(totalPrice * 100) / 100;
  const budgetRemaining = Math.round((limits.totalBudget - totalPrice) * 100) / 100;
  const overBudget = totalPrice > limits.totalBudget + 1e-9;

  if (players.length !== limits.totalPlayers) {
    violations.push(
      `Нужно ровно ${limits.totalPlayers} игроков (выбрано ${players.length}).`,
    );
  }

  for (const role of ROLES) {
    const { min, max } = limits.roleLimits[role];
    const count = roleCounts[role];
    if (count > max) {
      violations.push(`${ROLE_LABELS[role]}: не более ${max} (выбрано ${count}).`);
    } else if (players.length === limits.totalPlayers && count < min) {
      violations.push(`${ROLE_LABELS[role]}: не менее ${min} (выбрано ${count}).`);
    }
  }

  if (overBudget) {
    violations.push(
      `Превышен бюджет: ${totalPrice.toFixed(1)} из ${limits.totalBudget.toFixed(1)}.`,
    );
  }

  for (const [clubId, count] of Object.entries(clubCounts)) {
    if (count > limits.maxSameTeam) {
      violations.push(
        `Из одного клуба не более ${limits.maxSameTeam} игроков (клуб ${clubId}: ${count}).`,
      );
    }
  }

  return {
    valid: violations.length === 0,
    violations,
    totalPrice,
    budgetRemaining,
    roleCounts,
    clubCounts,
    overBudget,
  };
}

// Whether a candidate can still be added without breaking a hard cap. Used to
// disable "add" buttons proactively so the user never assembles an invalid squad.
export function canAddPlayer(
  player: PlayerModel,
  current: PlayerModel[],
  limits: SquadLimits,
): { allowed: boolean; reason?: string } {
  if (current.some((p) => p.player_season_id === player.player_season_id)) {
    return { allowed: false, reason: "Игрок уже в составе" };
  }
  if (current.length >= limits.totalPlayers) {
    return { allowed: false, reason: "Состав уже заполнен" };
  }
  const roleCount = current.filter((p) => p.role === player.role).length;
  if (roleCount >= limits.roleLimits[player.role].max) {
    return { allowed: false, reason: `Лимит на позицию ${ROLE_SHORT[player.role]}` };
  }
  if (player.club_id != null) {
    const clubCount = current.filter((p) => p.club_id === player.club_id).length;
    if (clubCount >= limits.maxSameTeam) {
      return { allowed: false, reason: "Лимит игроков из клуба" };
    }
  }
  const price = player.price ?? 0;
  const spent = current.reduce((sum, p) => sum + (p.price ?? 0), 0);
  if (spent + price > limits.totalBudget + 1e-9) {
    return { allowed: false, reason: "Не хватает бюджета" };
  }
  return { allowed: true };
}
