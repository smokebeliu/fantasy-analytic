import { describe, expect, it } from "vitest";
import {
  canAddPlayer,
  parseRoleLimits,
  resolveSquadLimits,
  validateSquad,
} from "@/lib/squad";
import { RPL_RULES, makePlayer, makeValidSquad } from "./fixtures";

describe("parseRoleLimits", () => {
  it("maps API constraints to {min,max} per role", () => {
    const limits = parseRoleLimits(RPL_RULES.full_roster_constraints, 15);
    expect(limits.GOALKEEPER).toEqual({ min: 2, max: 2 });
    expect(limits.DEFENDER).toEqual({ min: 5, max: 5 });
    expect(limits.FORWARD).toEqual({ min: 3, max: 3 });
  });

  it("defaults to unconstrained when the list is missing", () => {
    const limits = parseRoleLimits(null, 15);
    expect(limits.MIDFIELDER).toEqual({ min: 0, max: 15 });
  });
});

describe("validateSquad", () => {
  const limits = resolveSquadLimits(RPL_RULES, 3);

  it("accepts a fully valid 15-man squad", () => {
    const result = validateSquad(makeValidSquad(), limits);
    expect(result.valid).toBe(true);
    expect(result.violations).toHaveLength(0);
    expect(result.totalPrice).toBe(90);
    expect(result.budgetRemaining).toBe(10);
  });

  it("flags an incomplete squad", () => {
    const result = validateSquad(makeValidSquad().slice(0, 10), limits);
    expect(result.valid).toBe(false);
    expect(result.violations.some((v) => v.includes("15 игроков"))).toBe(true);
  });

  it("flags a broken role distribution", () => {
    const squad = makeValidSquad();
    // Swap a defender for an extra forward (6 -> too many FWD, too few DEF).
    squad[2] = makePlayer({ ...squad[2], role: "FORWARD" });
    const result = validateSquad(squad, limits);
    expect(result.valid).toBe(false);
    expect(result.violations.some((v) => v.includes("Нападающие"))).toBe(true);
  });

  it("flags exceeding the budget", () => {
    const squad = makeValidSquad().map((p) => ({ ...p, price: 10 }));
    const result = validateSquad(squad, limits);
    expect(result.overBudget).toBe(true);
    expect(result.violations.some((v) => v.includes("бюджет"))).toBe(true);
  });

  it("flags too many players from one club", () => {
    const squad = makeValidSquad().map((p) => ({ ...p, club_id: 1, club_name: "Один" }));
    const result = validateSquad(squad, limits);
    expect(result.violations.some((v) => v.includes("одного клуба"))).toBe(true);
  });

  it("flags duplicate players", () => {
    const squad = makeValidSquad();
    squad[1] = squad[0];
    const result = validateSquad(squad, limits);
    expect(result.violations.some((v) => v.includes("повторя"))).toBe(true);
  });
});

describe("canAddPlayer", () => {
  const limits = resolveSquadLimits(RPL_RULES, 3);

  it("blocks adding a duplicate", () => {
    const player = makePlayer({ role: "MIDFIELDER" });
    const check = canAddPlayer(player, [player], limits);
    expect(check.allowed).toBe(false);
    expect(check.reason).toContain("уже");
  });

  it("blocks exceeding the club cap", () => {
    const current = [
      makePlayer({ role: "DEFENDER", club_id: 7 }),
      makePlayer({ role: "MIDFIELDER", club_id: 7 }),
      makePlayer({ role: "FORWARD", club_id: 7 }),
    ];
    const check = canAddPlayer(makePlayer({ role: "DEFENDER", club_id: 7 }), current, limits);
    expect(check.allowed).toBe(false);
    expect(check.reason).toContain("клуб");
  });

  it("blocks exceeding the role cap", () => {
    const current = [
      makePlayer({ role: "GOALKEEPER", club_id: 1 }),
      makePlayer({ role: "GOALKEEPER", club_id: 2 }),
    ];
    const check = canAddPlayer(makePlayer({ role: "GOALKEEPER", club_id: 3 }), current, limits);
    expect(check.allowed).toBe(false);
  });

  it("blocks when the budget is insufficient", () => {
    const current = [makePlayer({ role: "MIDFIELDER", price: 99 })];
    const check = canAddPlayer(makePlayer({ role: "DEFENDER", price: 5 }), current, limits);
    expect(check.allowed).toBe(false);
    expect(check.reason).toContain("бюджет");
  });

  it("allows a legal addition", () => {
    const check = canAddPlayer(
      makePlayer({ role: "DEFENDER", club_id: 4, price: 5 }),
      [makePlayer({ role: "GOALKEEPER", club_id: 1 })],
      limits,
    );
    expect(check.allowed).toBe(true);
  });
});
