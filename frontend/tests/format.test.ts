import { describe, expect, it } from "vitest";
import {
  componentLabel,
  formatPercent,
  formatPoints,
  formatPrice,
} from "@/lib/format";

describe("format helpers", () => {
  it("formats points with a dash fallback", () => {
    expect(formatPoints(7.851, 2)).toBe("7.85");
    expect(formatPoints(null)).toBe("—");
    expect(formatPoints(undefined)).toBe("—");
  });

  it("formats price to one decimal", () => {
    expect(formatPrice(10)).toBe("10.0");
    expect(formatPrice(null)).toBe("—");
  });

  it("scales probabilities and percentages", () => {
    expect(formatPercent(0.85)).toBe("85.0%");
    expect(formatPercent(47.6)).toBe("47.6%");
    expect(formatPercent(null)).toBe("—");
  });

  it("translates known component keys", () => {
    expect(componentLabel("clean_sheet")).toBe("Сухой матч");
    expect(componentLabel("unknown_key")).toBe("unknown_key");
  });
});
