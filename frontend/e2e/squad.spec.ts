import { expect, test } from "@playwright/test";

test.describe("Squad builder", () => {
  test("builds an optimal squad via the optimizer", async ({ page }) => {
    await page.goto("/squad");
    await expect(page.getByRole("heading", { name: "Конструктор состава" })).toBeVisible();
    await page.getByRole("button", { name: /Автосостав/ }).click();
    const result = page.getByTestId("optimizer-result");
    await expect(result).toBeVisible();
    // Formation like 4-5-1 and a captain badge should render.
    await expect(result).toContainText(/\d-\d-\d/);
    await expect(result).toContainText("Капитан:");
  });

  test("validates constraints before enabling transfers", async ({ page }) => {
    await page.goto("/squad");
    await expect(page.getByTestId("pool-row").first()).toBeVisible();
    const transfers = page.getByTestId("optimize-transfers");
    await expect(transfers).toBeDisabled();

    // Add one player -> incomplete squad -> violations shown, still disabled.
    await page.getByTestId("pool-row").first().getByRole("button", { name: /Добавить/ }).click();
    await expect(page.getByTestId("violations")).toContainText("15 игроков");
    await expect(transfers).toBeDisabled();
  });
});
