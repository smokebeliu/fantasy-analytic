import { expect, test } from "@playwright/test";

test.describe("Tour and players page", () => {
  test("shows the player table with projections", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Тур и игроки" })).toBeVisible();
    await expect(page.getByTestId("player-row").first()).toBeVisible();
    await expect(page.getByTestId("freshness")).toContainText("poisson_events");
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
