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

  test("splits into pitch and pool with a group-button position filter", async ({ page }) => {
    await page.goto("/squad");

    // The left column shows the squad pitch (schema view) by default.
    await expect(page.getByTestId("squad-pitch")).toBeVisible();

    // The position filter is a group of buttons, not a <select>.
    const roleFilter = page.getByTestId("role-filter");
    await expect(roleFilter.getByRole("button", { name: "Все" })).toBeVisible();
    await roleFilter.getByRole("button", { name: "ЗАЩ" }).click();
    await expect(roleFilter.getByRole("button", { name: "ЗАЩ" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    // Filtered pool only lists defenders.
    await expect(page.getByTestId("pool-row").first()).toBeVisible();
    await expect(page.getByTestId("pool-row").first().locator(".role-badge")).toHaveText(
      "ЗАЩ",
    );
  });

  test("toggles the squad between pitch and list views", async ({ page }) => {
    await page.goto("/squad");
    await expect(page.getByTestId("squad-pitch")).toBeVisible();

    // Build an optimal squad, then switch to the list view.
    await page.getByRole("button", { name: /Автосостав/ }).click();
    await expect(page.getByTestId("optimizer-result")).toBeVisible();

    await page.getByRole("button", { name: "Список" }).click();
    await expect(page.getByTestId("squad-list")).toBeVisible();
    await expect(page.getByTestId("squad-pitch")).toHaveCount(0);

    await page.getByRole("button", { name: "Схема" }).click();
    await expect(page.getByTestId("squad-pitch")).toBeVisible();
  });
});
