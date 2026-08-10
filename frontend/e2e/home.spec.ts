import { expect, test } from "@playwright/test";

test.describe("Tour and players page", () => {
  test("shows the player table with projections", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Тур и игроки" })).toBeVisible();
    await expect(page.getByTestId("player-row").first()).toBeVisible();
    await expect(page.getByTestId("freshness")).toContainText("poisson_events");
  });

  test("has no tour filter and defaults to full-season stats", async ({ page }) => {
    // Step 12 (request 4): the per-tour dropdown is removed; the table shows the
    // full season statistics as of now.
    await page.goto("/");
    await expect(page.getByTestId("player-row").first()).toBeVisible();
    await expect(page.locator("#tour")).toHaveCount(0);
    await expect(page.getByText(/Полная статистика сезона/)).toBeVisible();
  });

  test("sorts players by season points on header click", async ({ page }) => {
    // Step 12 (request 3): the "Очки" column is sortable and sorting is instant.
    await page.goto("/");
    await expect(page.getByTestId("player-row").first()).toBeVisible();

    // The <th> cells resolve to the "cell" role inside the header row, so target
    // them by text within the table head.
    const priceHeader = page.locator("thead th", { hasText: "Цена" });
    const pointsHeader = page.locator("thead th", { hasText: "Очки" });

    // Change the sort away from points, then back via the "Очки" header.
    await priceHeader.click();
    await expect(priceHeader).toHaveAttribute("aria-sort", "descending");
    await pointsHeader.click();
    await expect(pointsHeader).toHaveAttribute("aria-sort", "descending");

    // The season-points column (6th) must be non-increasing across the page.
    const cells = await page
      .locator('[data-testid="player-row"] td:nth-child(6)')
      .allInnerTexts();
    const values = cells.map((c) => {
      const n = Number(c.replace(/[^\d.-]/g, ""));
      return Number.isNaN(n) ? 0 : n;
    });
    const descending = [...values].sort((a, b) => b - a);
    expect(values).toEqual(descending);
  });

  test("sorts players by prior season and ownership on header click", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("player-row").first()).toBeVisible();

    const priorHeader = page.locator("thead th", { hasText: "Прошлый сезон" });
    const ownershipHeader = page.locator("thead th", { hasText: "Выбор" });

    await priorHeader.click();
    await expect(priorHeader).toHaveAttribute("aria-sort", "descending");

    // Prior-season points column (7th); treat "—" as nulls last by sorting only
    // numeric cells as non-increasing among themselves, then nulls at the end.
    const priorCells = await page.getByTestId("prior-points").allInnerTexts();
    const priorValues = priorCells.map((c) => {
      const trimmed = c.trim();
      if (trimmed === "—" || trimmed === "") return null;
      const n = Number(trimmed.replace(/[^\d.-]/g, ""));
      return Number.isNaN(n) ? null : n;
    });
    const priorNumeric = priorValues.filter((v): v is number => v !== null);
    const priorDescending = [...priorNumeric].sort((a, b) => b - a);
    expect(priorNumeric).toEqual(priorDescending);
    const firstNull = priorValues.findIndex((v) => v === null);
    if (firstNull !== -1) {
      expect(priorValues.slice(firstNull).every((v) => v === null)).toBe(true);
    }

    await ownershipHeader.click();
    await expect(ownershipHeader).toHaveAttribute("aria-sort", "descending");

    // Ownership column (8th).
    const ownershipCells = await page
      .locator('[data-testid="player-row"] td:nth-child(8)')
      .allInnerTexts();
    const ownershipValues = ownershipCells.map((c) => {
      const n = Number(c.replace(/[^\d.-]/g, ""));
      return Number.isNaN(n) ? 0 : n;
    });
    const ownershipDescending = [...ownershipValues].sort((a, b) => b - a);
    expect(ownershipValues).toEqual(ownershipDescending);
  });

  test("filters players by position", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("player-row").first()).toBeVisible();
    await page.getByLabel("Позиция").selectOption("GOALKEEPER");
    // Every visible role badge should be a goalkeeper (ВРТ).
    await expect(page.getByTestId("player-row").first()).toBeVisible();
    const badges = page.locator(".role-badge");
    await expect(badges.first()).toHaveText("ВРТ");
  });

  test("fills the forecast column from the published snapshot", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("player-row").first()).toBeVisible();

    // The forecast column used to be empty because nothing ever persisted the
    // projections; at least some rows must now carry a number.
    const projections = await page
      .locator('[data-testid="player-row"] td:nth-child(4)')
      .allInnerTexts();
    expect(projections.some((cell) => /\d/.test(cell))).toBe(true);
  });

  test("shows last season's points and a card on hover", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("player-row").first()).toBeVisible();

    await expect(page.locator("thead th", { hasText: "Прошлый сезон" })).toBeVisible();
    const priorCells = await page.getByTestId("prior-points").allInnerTexts();
    expect(priorCells.some((cell) => /\d/.test(cell))).toBe(true);

    await page.getByTestId("player-row").first().locator("td").first().hover();
    const card = page.getByTestId("player-hover-card");
    await expect(card).toBeVisible();
    await expect(card).toContainText("Прошлый сезон");
  });

  test("opens a player card with the forecast explanation", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("player-row").first().getByRole("button").first().click();
    await expect(page.getByTestId("player-card")).toBeVisible();
    await expect(page.getByTestId("forecast-explanation")).toBeVisible();
    await expect(page.getByText("Из чего складывается прогноз")).toBeVisible();
    await expect(page.getByTestId("prior-season")).toBeVisible();
  });

  test("compares two players", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("player-row").first()).toBeVisible();
    const checkboxes = page.getByRole("checkbox");
    await checkboxes.nth(0).check();
    await checkboxes.nth(1).check();
    await page.getByRole("button", { name: /Сравнить/ }).click();
    await expect(page.getByTestId("compare-modal")).toBeVisible();
    await expect(page.getByText("Прогноз очков")).toBeVisible();
  });
});
