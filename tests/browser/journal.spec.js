const { test, expect } = require("@playwright/test");

function localDay() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
const food = {
  id: "ciqual:demo",
  name: "Riz blanc cuit",
  source: "Ciqual 2025 · Anses",
  nutrients: { kcal: 130, protein: 2.6, carbs: 28, fat: 0.3, fiber: 0.4 },
  flags: {},
};

test.beforeEach(async ({ page }) => {
  let logged = false,
    meals = [],
    complete = false;
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()),
      path = url.pathname;
    let data = {};
    if (path === "/api/auth/me")
      data = {
        user: logged
          ? {
              id: "browser-test",
              email: "demo@example.test",
              legacyOwner: true,
            }
          : null,
      };
    else if (path === "/api/auth/login") {
      logged = true;
      data = {
        user: {
          id: "browser-test",
          email: "demo@example.test",
          legacyOwner: true,
        },
      };
    } else if (path === "/api/profile") data = { profile: null };
    else if (path === "/api/favorites") data = { favorites: [] };
    else if (path === "/api/integrations")
      data = {
        chatgpt: {
          connected: true,
          email: "demo@example.test",
          model: "gpt-6-luna",
        },
        garmin: { connected: false, state: "idle" },
        catalogue: { foods: 3484 },
      };
    else if (path === "/api/foods") data = { foods: [food] };
    else if (path === "/api/meals/parse")
      data = {
        title: "Mon déjeuner",
        questions: ["Vérifie le poids comestible."],
        items: [
          {
            label: "Riz blanc cuit",
            grams: 200,
            estimated: false,
            note: "",
            food_id: food.id,
            matches: [food],
          },
        ],
      };
    else if (path === "/api/meals" && route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      const item = {
        ...body.items[0],
        name: food.name,
        source: food.source,
        flags: {},
        nutrients: Object.fromEntries(
          Object.entries(food.nutrients).map(([k, v]) => [
            k,
            (v * body.items[0].grams) / 100,
          ]),
        ),
      };
      meals.push({
        ...body,
        id: "meal-demo",
        items: [item],
        totals: item.nutrients,
      });
      data = { id: "meal-demo" };
    } else if (path === "/api/day") {
      complete = route.request().postDataJSON().complete;
      data = { ok: true };
    } else if (path === "/api/dashboard")
      data = {
        day: url.searchParams.get("day"),
        meals,
        complete,
        has_meals: !!meals.length,
        intake: meals.length
          ? meals[0].totals
          : { kcal: 0, protein: 0, carbs: 0, fat: 0, fiber: 0 },
        resting: 1800,
        expenditure: 2520,
        expenditure_source: "Estimation du profil",
        projected: true,
        garmin: null,
        targets: { kcal: 2270, protein: 114, carbs: 255, fat: 88 },
        deficit: null,
      };
    else if (path === "/api/trends")
      data = {
        days: [],
        weights: [],
        cumulative_deficit: 0,
        covered_days: 0,
        elapsed_days: 29,
      };
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(data),
    });
  });
  await page.goto("/");
  await page.getByLabel("Email", { exact: true }).fill("demo@example.test");
  await page.getByLabel("Mot de passe", { exact: true }).fill("test-only");
  await page.getByRole("button", { name: "Ouvrir mon dashboard" }).click();
  await expect(page.locator("#app")).toBeVisible();
});

test("dictated text, Luna review, edit quantity, save and confirm day", async ({
  page,
}, info) => {
  await page
    .locator("#view-today")
    .getByRole("button", { name: "Ajouter mon repas" })
    .first()
    .click();
  await page.locator("#meal-text").fill("200 grammes de riz blanc cuit");
  await page.getByRole("button", { name: "Préparer avec Luna" }).click();
  await expect(page.locator("#meal-questions")).toContainText(
    "Vérifie le poids",
  );
  await page.getByLabel("Poids (g)", { exact: true }).fill("250");
  await expect(page.locator("#meal-preview")).toContainText("325");
  await page
    .getByLabel("J’ai vérifié les aliments, portions et hypothèses.")
    .check();
  await page.getByRole("button", { name: "Enregistrer mon repas" }).click();
  await expect(page.locator("#meal-dialog")).not.toBeVisible();
  await expect(page.locator("#intake-kcal")).toContainText("325");
  await expect(page.locator("#today-meals")).toContainText("Mon déjeuner");
  await page.getByRole("button", { name: "Confirmer la journée" }).click();
  await expect(
    page.getByRole("button", { name: "Rouvrir la journée" }),
  ).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1,
  );
  expect(overflow).toBe(false);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: info.outputPath("dashboard.png"),
    fullPage: true,
  });
});

test("manual food search and saving without an AI request", async ({
  page,
}) => {
  let parseCalls = 0;
  page.on("request", (r) => {
    if (r.url().includes("/api/meals/parse")) parseCalls++;
  });
  await page
    .locator("#view-today")
    .getByRole("button", { name: "Ajouter mon repas" })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Ajouter un aliment", exact: false })
    .first()
    .click();
  await page.getByLabel("Aliment à vérifier").fill("riz");
  await page.getByRole("button", { name: /Riz blanc cuit/ }).click();
  await page.getByLabel("Poids (g)", { exact: true }).fill("200");
  await page
    .getByLabel("J’ai vérifié les aliments, portions et hypothèses.")
    .check();
  await page.getByRole("button", { name: "Enregistrer mon repas" }).click();
  await expect(page.locator("#intake-kcal")).toContainText("260");
  expect(parseCalls).toBe(0);
});

test("profile, favorites and trends stay usable on small screens", async ({
  page,
}) => {
  await page.locator('.nav-button[data-view="profile"]').click();
  await expect(page.locator("#profile-form")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    ),
  ).toBe(false);
  await page.locator('.nav-button[data-view="favorites"]').click();
  await expect(page.locator("#favorite-list")).toContainText(
    "Un repas que tu aimes retrouver",
  );
  await page.locator('.nav-button[data-view="trends"]').click();
  await expect(page.locator("#trend-kpis")).toContainText(
    "Couverture du suivi",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > window.innerWidth + 1,
    ),
  ).toBe(false);
});
