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

  test("filters players by position", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByTestId("player-row").first()).toBeVisible();
    await page.getByLabel("Позиция").selectOption("GOALKEEPER");
    // Every visible role badge should be a goalkeeper (ВРТ).
    await expect(page.getByTestId("player-row").first()).toBeVisible();
    const badges = page.locator(".role-badge");
    await expect(badges.first()).toHaveText("ВРТ");
  });

  test("opens a player card with the forecast explanation", async ({ page }) => {
    await page.goto("/");
    await page.getByTestId("player-row").first().getByRole("button").first().click();
    await expect(page.getByTestId("player-card")).toBeVisible();
    await expect(page.getByTestId("forecast-explanation")).toBeVisible();
    await expect(page.getByText("Из чего складывается прогноз")).toBeVisible();
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
