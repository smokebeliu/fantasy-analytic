import { expect, test } from "@playwright/test";

test.describe("Squad builder", () => {
  test("builds an optimal squad via the optimizer", async ({ page }) => {
    await page.goto("/squad");
    await expect(page.getByRole("heading", { name: "Конструктор состава" })).toBeVisible();
    await page.getByTestId("optimize-from-scratch").click();
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

  test("pins a player and refills the squad under the chosen formation", async ({
    page,
  }) => {
    await page.goto("/squad");

    // Start from a full optimal squad so there is something to pin.
    await page.getByTestId("optimize-from-scratch").click();
    await expect(page.getByTestId("optimizer-result")).toBeVisible();
    await expect(page.getByTestId("locked-note")).toContainText("Закрепите игроков");

    const pitch = page.getByTestId("squad-pitch");
    const pinned = pitch.getByRole("button", { name: /^Закрепить / }).first();
    const pinnedName = await pinned
      .locator("xpath=..")
      .locator(".nm")
      .innerText();
    await pinned.click();
    await expect(page.getByTestId("locked-note")).toContainText("Закреплено 1");

    // Ask the optimizer to keep the pin and rebuild the rest as 4-4-2.
    await page.getByTestId("formation-select").selectOption("4-4-2");
    await page.getByTestId("optimize-locked").click();

    const result = page.getByTestId("optimizer-result");
    await expect(result).toBeVisible();
    await expect(result).toContainText("4-4-2");

    // The pinned player survived the round-trip and is still marked as locked.
    await expect(page.getByTestId("locked-note")).toContainText("Закреплено 1");
    await expect(pitch.locator(".pitch-player--locked")).toHaveCount(1);
    await expect(pitch.locator(".pitch-player--locked .nm")).toHaveText(pinnedName);
  });

  test("offers only playable formations and honours the chosen one", async ({
    page,
  }) => {
    await page.goto("/squad");
    const select = page.getByTestId("formation-select");

    // The RPL starting limits allow 3..5 DEF, 2..5 MID and 1..3 FWD.
    const options = await select.locator("option").allInnerTexts();
    expect(options).toContain("Любая");
    expect(options).toContain("3-5-2");
    expect(options).toContain("5-3-2");
    expect(options).not.toContain("2-5-3");

    await select.selectOption("5-3-2");
    await page.getByTestId("optimize-locked").click();

    const result = page.getByTestId("optimizer-result");
    await expect(result).toBeVisible();
    await expect(result).toContainText("5-3-2");
  });

  test("spells out how the two build buttons differ", async ({ page }) => {
    await page.goto("/squad");

    await expect(page.getByTestId("optimizer-help")).toContainText(
      "игнорирует всё, что вы выбрали",
    );
    await expect(page.getByTestId("optimizer-help")).toContainText(
      "сохраняет закреплённых",
    );
    // Nothing pinned and no formation: the constrained run would return the same
    // squad, so it is offered as unavailable with the reason on the button.
    await expect(page.getByTestId("optimize-locked")).toBeDisabled();
    await page.getByTestId("formation-select").selectOption("4-4-2");
    await expect(page.getByTestId("optimize-locked")).toBeEnabled();
  });

  test("suggests replacements for the squad the user already owns", async ({
    page,
  }) => {
    await page.goto("/squad");

    // Start from a full legal squad, then weaken it so there is something to fix:
    // three players are swapped for the cheapest ones the pool offers.
    await page.getByTestId("optimize-from-scratch").click();
    await expect(page.getByTestId("optimizer-result")).toBeVisible();

    const pitch = page.getByTestId("squad-pitch");
    for (let i = 0; i < 3; i += 1) {
      await pitch.getByRole("button", { name: /^Убрать / }).first().click();
    }
    // Refill from the pool, which is sorted by projection, from the weak end.
    const pool = page.getByTestId("pool-row");
    const total = await pool.count();
    for (let i = 0; i < 3; i += 1) {
      await pool
        .nth(total - 1 - i)
        .getByRole("button", { name: /Добавить/ })
        .click();
    }
    await expect(page.getByTestId("valid-note")).toBeVisible();

    await expect(page.getByTestId("transfers-select")).toHaveValue("3");
    await page.getByTestId("optimize-transfers").click();

    // Every suggestion names both sides of the swap.
    const plan = page.getByTestId("transfer-plan");
    await expect(plan).toBeVisible();
    const pairs = page.getByTestId("transfer-pair");
    await expect(pairs.first()).toContainText("OUT");
    await expect(pairs.first()).toContainText("IN");
    await expect(pairs.first()).toContainText("очк.");
    await expect(pairs.first()).toContainText("к бюджету");

    // The user's own squad stays on the pitch to compare against.
    await expect(pitch.getByTestId("pitch-player")).toHaveCount(15);
  });

  test("shows a player's card on hover over the pitch", async ({ page }) => {
    await page.goto("/squad");
    await page.getByTestId("optimize-from-scratch").click();
    await expect(page.getByTestId("optimizer-result")).toBeVisible();

    await page.getByTestId("squad-pitch").getByTestId("pitch-player").first().hover();

    const card = page.getByTestId("player-hover-card");
    await expect(card).toBeVisible();
    await expect(card).toContainText("Очки за сезон");
    await expect(card).toContainText("Прошлый сезон");
  });

  test("toggles the squad between pitch and list views", async ({ page }) => {
    await page.goto("/squad");
    await expect(page.getByTestId("squad-pitch")).toBeVisible();

    // Build an optimal squad, then switch to the list view.
    await page.getByTestId("optimize-from-scratch").click();
    await expect(page.getByTestId("optimizer-result")).toBeVisible();

    await page.getByRole("button", { name: "Список" }).click();
    await expect(page.getByTestId("squad-list")).toBeVisible();
    await expect(page.getByTestId("squad-pitch")).toHaveCount(0);

    await page.getByRole("button", { name: "Схема" }).click();
    await expect(page.getByTestId("squad-pitch")).toBeVisible();
  });
});
