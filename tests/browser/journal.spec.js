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
    jobs = [],
    pendingReads = 0;
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
    else if (path === "/api/captures") {
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON();
        jobs = [{ ...body, id: "capture-demo", status: "queued" }];
        pendingReads = 0;
        data = { id: "capture-demo" };
      } else {
        if (jobs.length && ++pendingReads > 1 && jobs[0].status !== "done") {
          jobs[0].status = "done";
          const nutrients = Object.fromEntries(
            Object.entries(food.nutrients).map(([k, v]) => [k, v * 2]),
          );
          meals = [
            {
              id: "capture-demo",
              day: localDay(),
              slot: "dinner",
              title: "Mon dîner",
              text: jobs[0].text,
              items: [
                {
                  food_id: food.id,
                  name: food.name,
                  grams: 200,
                  estimated: true,
                  note: "Portion estimée",
                  source: food.source,
                  source_url: "https://example.test/menu",
                  composition_estimated: true,
                  flags: {},
                  nutrients,
                },
              ],
              totals: nutrients,
              clarifications: [
                {
                  item_index: 0,
                  label: "Taille de la portion",
                  selected: 0,
                  options: [
                    { label: "Moyenne", grams: 200 },
                    { label: "Petite", grams: 100 },
                    { label: "Grande", grams: 300 },
                  ],
                },
              ],
            },
          ];
        }
        data = { jobs };
      }
    } else if (path.endsWith("/reanalyse")) {
      jobs = [
        {
          id: "capture-reanalysis",
          text: meals[0].text,
          day: localDay(),
          status: "queued",
        },
      ];
      pendingReads = 0;
      data = { id: "capture-reanalysis" };
    } else if (path.endsWith("/refine")) {
      const option = route.request().postDataJSON().option;
      const grams = [200, 100, 300][option];
      meals[0].clarifications[0].selected = option;
      meals[0].items[0].grams = grams;
      meals[0].totals = Object.fromEntries(
        Object.entries(food.nutrients).map(([k, v]) => [k, (v * grams) / 100]),
      );
      data = { ok: true };
    } else if (path === "/api/meals" && route.request().method() === "POST") {
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
    } else if (path === "/api/dashboard")
      data = {
        day: url.searchParams.get("day"),
        meals,
        complete: !!meals.length,
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

test("capture closes immediately, inferred evening meal and optional refinement", async ({
  page,
}, info) => {
  await page
    .getByRole("button", { name: "Ajouter un repas · Midi", exact: true })
    .click();
  await expect(page.locator("#meal-slot-field")).not.toBeVisible();
  await page.locator("#meal-text").fill("Ce soir du riz");
  await page.getByRole("button", { name: "Envoyer à Luna" }).click();
  await expect(page.locator("#meal-dialog")).not.toBeVisible();
  await expect(page.locator("#today-captures")).toContainText("Repas reçu");
  await expect(page.locator("#intake-kcal")).toContainText("260", {
    timeout: 10000,
  });
  await expect(
    page.locator('#today-meals .meal-period[data-slot="dinner"]'),
  ).toContainText("Mon dîner");
  await expect(page.locator("#today-meals")).toContainText(
    "Précision facultative",
  );
  await page
    .locator("#today-meals")
    .getByRole("button", { name: "Grande", exact: true })
    .click();
  await expect(page.locator("#intake-kcal")).toContainText("390");
  await expect(
    page
      .locator("#today-meals")
      .getByRole("button", { name: "Grande", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator("#complete-day")).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    ),
  ).toBe(false);
  await page.screenshot({
    path: info.outputPath("dashboard.png"),
    fullPage: true,
  });
});

test("journal shows estimated recipe composition and its source link", async ({
  page,
}) => {
  await page
    .getByRole("button", { name: "Ajouter un repas · Soir", exact: true })
    .click();
  await page.locator("#meal-text").fill("Ce soir du riz");
  await page.getByRole("button", { name: "Envoyer à Luna" }).click();
  await expect(page.locator("#intake-kcal")).toContainText("260", {
    timeout: 10000,
  });
  await page.locator('.nav-button[data-view="journal"]').click();
  await expect(page.locator("#view-journal")).toContainText(
    "composition estimée",
  );
  const link = page
    .locator("#view-journal")
    .getByRole("link", { name: "Voir la source" });
  await expect(link).toHaveAttribute("href", "https://example.test/menu");
  await expect(link).toHaveAttribute("rel", "noopener noreferrer");
  const values = page.locator("#journal-meals .food-nutrients");
  await expect(values).toContainText("260 kcal");
  await expect(values).toContainText("5,2 g Protéines");
  await expect(values).toContainText("56 g Glucides");
  await expect(values).toContainText("0,6 g Lipides");
  await page.locator("#journal-meals [data-edit]").click();
  await expect(page.locator("#meal-items .food-nutrients")).toContainText(
    "260 kcal",
  );
  await page.getByLabel("Poids (g)", { exact: true }).fill("100");
  await expect(page.locator("#meal-items .food-nutrients")).toContainText(
    "130 kcal",
  );
  await expect(page.locator(".food-estimated")).toHaveCount(0);
  await expect(
    page.getByLabel("Poids approximatif", { exact: true }),
  ).toHaveCount(0);
  await expect(page.locator("#meal-items .food-nutrients")).toContainText(
    "130 kcal",
  );
});

test("existing meal can be reanalysed without retyping or duplicate cards", async ({
  page,
}) => {
  await page
    .getByRole("button", { name: "Ajouter un repas · Soir", exact: true })
    .click();
  await page.locator("#meal-text").fill("Ce soir du riz");
  await page.getByRole("button", { name: "Envoyer à Luna" }).click();
  await expect(page.locator("#intake-kcal")).toContainText("260", {
    timeout: 10000,
  });
  const request = page.waitForRequest(
    (r) =>
      r.url().endsWith("/api/meals/capture-demo/reanalyse") &&
      r.method() === "POST",
  );
  await page
    .locator("#today-meals")
    .getByRole("button", { name: "Réanalyser la saisie" })
    .click();
  await request;
  await expect(page.locator("#today-captures")).toContainText("Repas reçu");
  await expect(page.locator("#today-meals .meal-card")).toHaveCount(1);
  await expect(page.locator("#today-captures .capture-job")).toHaveCount(0, {
    timeout: 10000,
  });
  await expect(page.locator("#today-meals .meal-card")).toHaveCount(1);
});

async function fakeSpeech(page, error = null) {
  await page.addInitScript(
    ({ error }) => {
      window.SpeechRecognition = class {
        start() {
          this.onstart?.();
          if (error) {
            this.onerror?.({ error });
            this.onend?.();
          } else
            this.onresult?.({
              results: [[{ transcript: "Ce soir deux pizzas" }]],
            });
        }
        stop() {
          this.onend?.();
        }
        abort() {
          this.onend?.();
        }
      };
    },
    { error },
  );
  await page.reload();
  await expect(page.locator("#app")).toBeVisible();
  await page
    .getByRole("button", { name: "Ajouter un repas · Soir", exact: true })
    .click();
}

test("browser dictation fills text and submits while listening", async ({
  page,
}) => {
  await fakeSpeech(page);
  await page.getByRole("button", { name: "Dicter mon repas" }).click();
  await expect(page.locator("#meal-text")).toHaveValue("Ce soir deux pizzas");
  await expect(page.locator("#meal-text")).toHaveJSProperty("readOnly", true);
  await page.getByRole("button", { name: "Envoyer à Luna" }).click();
  await expect(page.locator("#meal-dialog")).not.toBeVisible();
  await expect(page.locator("#today-captures")).toContainText(
    "Ce soir deux pizzas",
  );
});

test("microphone permission failure keeps the typed meal editable", async ({
  page,
}) => {
  await fakeSpeech(page, "not-allowed");
  await page.locator("#meal-text").fill("Ce matin un café");
  await page.getByRole("button", { name: "Dicter mon repas" }).click();
  await expect(page.locator("#dictation-status")).toContainText(
    "Autorise le microphone",
  );
  await expect(page.locator("#meal-text")).toHaveValue("Ce matin un café");
  await expect(page.locator("#meal-text")).toBeEnabled();
});

async function fakeAndroidSpeech(page, phrases) {
  await page.addInitScript((phrases) => {
    Object.defineProperty(navigator, "userAgent", {
      configurable: true,
      get: () =>
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/142.0 Mobile Safari/537.36",
    });
    let session = 0;
    window.SpeechRecognition = class {
      constructor() {
        this.phrase = phrases[session++];
        this.results = [];
      }
      emit(transcript, isFinal) {
        const result = [{ transcript }];
        result.isFinal = isFinal;
        this.results.push(result);
        this.onresult?.({
          resultIndex: this.results.length - 1,
          results: this.results,
        });
      }
      start() {
        this.onstart?.();
        if (this.interimResults) {
          // Android Chromium promotes continuous-mode partials to final results.
          for (const text of ["alors", "alors", "alors ce", this.phrase])
            this.emit(text, this.continuous);
        }
      }
      stop() {
        this.ending = setTimeout(() => {
          this.emit(this.phrase, true);
          this.onend?.();
        }, 100);
      }
      abort() {
        clearTimeout(this.ending);
        this.onend?.();
      }
    };
  }, phrases);
  await page.reload();
  await expect(page.locator("#app")).toBeVisible();
  await page
    .getByRole("button", { name: "Ajouter un repas · Midi", exact: true })
    .click();
}

test("Android cumulative partials do not duplicate the meal sent to Luna", async ({
  page,
}) => {
  await fakeAndroidSpeech(page, ["alors ce midi"]);
  await page.getByRole("button", { name: "Dicter mon repas" }).click();
  await expect(page.locator("#meal-text")).toHaveValue("alors ce midi");
  await expect(page.locator("#meal-text")).toHaveJSProperty("readOnly", true);
  const sent = page.waitForRequest(
    (request) =>
      request.url().endsWith("/api/captures") && request.method() === "POST",
  );
  await page.getByRole("button", { name: "Envoyer à Luna" }).click();
  expect((await sent).postDataJSON().text).toBe("alors ce midi");
  await expect(page.locator("#meal-dialog")).not.toBeVisible();
  await expect(page.locator("#today-captures")).toContainText("alors ce midi");
});

test("Android dictation keeps typed text and repeated spoken words across sessions", async ({
  page,
}) => {
  await fakeAndroidSpeech(page, ["alors ce midi", "deux pizzas, deux pizzas"]);
  await page.locator("#meal-text").fill("Ce matin un café.");
  await page.getByRole("button", { name: "Dicter mon repas" }).click();
  await page.getByRole("button", { name: "Arrêter la dictée" }).click();
  await expect(page.locator("#meal-text")).toHaveValue(
    "Ce matin un café. alors ce midi",
  );
  await page.getByRole("button", { name: "Dicter mon repas" }).click();
  await page.getByRole("button", { name: "Arrêter la dictée" }).click();
  await expect(page.locator("#meal-text")).toHaveValue(
    "Ce matin un café. alors ce midi deux pizzas, deux pizzas",
  );
});

test("Android interim text replaces hypotheses and continues across natural pauses", async ({
  page,
}) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "userAgent", {
      get: () => "Android Chrome/142",
    });
    window.SpeechRecognition = class {
      constructor() {
        window.testRecognition = this;
        this.starts = 0;
      }
      start() {
        this.starts++;
        this.onstart?.();
      }
      emit(text, final = false) {
        const result = [{ transcript: text }];
        result.isFinal = final;
        this.onresult?.({ resultIndex: 0, results: [result] });
      }
      stop() {
        this.onend?.();
      }
      abort() {
        this.onend?.();
      }
    };
  });
  await page.reload();
  await page
    .getByRole("button", { name: "Ajouter un repas · Midi", exact: true })
    .click();
  await page.getByRole("button", { name: "Dicter mon repas" }).click();
  await page.evaluate(() => window.testRecognition.emit("alors"));
  await expect(page.locator("#meal-text")).toHaveValue("alors");
  await page.evaluate(() => window.testRecognition.emit("alors ce midi"));
  await expect(page.locator("#meal-text")).toHaveValue("alors ce midi");
  await page.evaluate(() => {
    window.testRecognition.emit("alors ce midi", true);
    window.testRecognition.onend();
  });
  expect(await page.evaluate(() => window.testRecognition.starts)).toBe(2);
  await expect(page.locator("#meal-text")).toHaveJSProperty("readOnly", true);
  await page.evaluate(() =>
    window.testRecognition.emit("deux pizzas, deux pizzas"),
  );
  await expect(page.locator("#meal-text")).toHaveValue(
    "alors ce midi deux pizzas, deux pizzas",
  );
  await page.getByRole("button", { name: "Arrêter la dictée" }).click();
  await expect(page.locator("#meal-text")).toHaveJSProperty("readOnly", false);
  expect(await page.evaluate(() => window.testRecognition.starts)).toBe(2);
});

test("morning midday and evening buttons select their meal period", async ({
  page,
}) => {
  for (const [label, value] of [
    ["Matin", "breakfast"],
    ["Midi", "lunch"],
    ["Soir", "dinner"],
  ]) {
    await page
      .getByRole("button", { name: `Ajouter un repas · ${label}`, exact: true })
      .click();
    await expect(page.locator("#meal-slot")).toHaveValue(value);
    await page.locator("#close-meal").click();
  }
  await expect(page.locator("#complete-day")).toHaveCount(0);
});

test("manual food search and saving without an AI request", async ({
  page,
}) => {
  let parseCalls = 0;
  page.on("request", (r) => {
    if (
      r.url().includes("/api/meals/parse") ||
      (r.url().includes("/api/captures") && r.method() === "POST")
    )
      parseCalls++;
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

test("Garmin expenditure is distinct from unlogged food calories", async ({
  page,
}) => {
  await page.route("**/api/dashboard?**", async (route) => {
    await route.fulfill({
      json: {
        day: localDay(),
        meals: [],
        has_meals: false,
        complete: false,
        intake: { kcal: 0, protein: 0, carbs: 0, fat: 0 },
        resting: null,
        expenditure: null,
        targets: null,
        deficit: null,
        projected: true,
        garmin: {
          has_data: true,
          total: 2700,
          active: 800,
          resting: 1900,
          partial: true,
          activities: [],
          synced_at: new Date().toISOString(),
        },
      },
    });
  });
  await page.locator("#previous-day").click();
  await expect(page.locator("#view-title")).toContainText("Dashboard");
  await expect(page.locator("#intake-kcal")).toContainText("—");
  await expect(page.locator("#expenditure-value")).toContainText("2");
  expect(
    (await page.locator("#expenditure-value").textContent()).replace(/\s/g, ""),
  ).toBe("2700kcal");
  await expect(page.locator("#expenditure-label")).toContainText(
    "dépensé jusqu’ici",
  );
  await expect(page.locator("#energy-caption")).toContainText(
    "Aucun repas saisi",
  );
  await expect(page.locator("#garmin-day")).toContainText(
    "automatique chaque heure",
  );
  await expect(page.locator("#energy-pct")).toHaveText("—");
});

test("dashboard deficit charts preserve gaps, surplus and provisional days", async ({
  page,
}, info) => {
  const days = [300, null, -100, 0, 900].map((deficit, i) => {
    const day = new Date();
    day.setDate(day.getDate() - 4 + i);
    const iso = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    return { day: iso, deficit };
  });
  await page.route("**/api/trends?**", (route) =>
    route.fulfill({
      json: {
        days,
        cumulative_deficit: 200,
        covered_days: 3,
        elapsed_days: 4,
        weights: [],
      },
    }),
  );
  await page.locator("#dashboard-period").selectOption("7");
  const daily = page.locator("#daily-deficit-chart .deficit-bar");
  await expect(daily).toHaveCount(3);
  expect(
    await daily.evaluateAll((els) => els.map((e) => Number(e.dataset.value))),
  ).toEqual([300, -100, 0]);
  await expect(page.locator("#daily-deficit-chart .surplus")).toHaveCount(1);
  const cumulative = page.locator("#cumulative-deficit-chart .deficit-point");
  expect(
    await cumulative.evaluateAll((els) =>
      els.map((e) => Number(e.dataset.value)),
    ),
  ).toEqual([300, 200, 200]);
  await expect(
    page.locator("#cumulative-deficit-chart .deficit-line"),
  ).toHaveCount(1);
  await expect(page.locator("#dashboard-kpis")).toContainText("200 kcal");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    ),
  ).toBe(false);
  await page.evaluate(() => scrollTo(0, 0));
  await page.screenshot({
    path: info.outputPath("dashboard-charts.png"),
    fullPage: true,
  });
});
