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
          reanalysis_meal_ids: [meals[0].id],
        },
      ];
      pendingReads = 0;
      data = { id: "capture-reanalysis", reanalysis_meal_ids: [meals[0].id] };
    } else if (path.endsWith("/refine")) {
      const option = route.request().postDataJSON().option;
      const grams = [200, 100, 300][option];
      meals[0].clarifications[0].selected = option;
      meals[0].clarifications[0].resolved = true;
      meals[0].items[0].grams = grams;
      meals[0].totals = Object.fromEntries(
        Object.entries(food.nutrients).map(([k, v]) => [k, (v * grams) / 100]),
      );
      meals[0].items[0].nutrients = meals[0].totals;
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
  await expect(page.locator("#today-meals .meal-refinement")).toHaveCount(0);
  await expect(page.locator("#today-meals .meal-macros")).toContainText(
    "P 7,8 g",
  );
  await page.reload();
  await expect(page.locator("#intake-kcal")).toContainText("390");
  await expect(page.locator("#today-meals .meal-refinement")).toHaveCount(0);

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
  await expect(values).toContainText("P 5,2 g");
  await expect(values).toContainText("G 56 g");
  await expect(values).toContainText("L 0,6 g");
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

async function savedNarrative(page) {
  await page
    .getByRole("button", { name: "Ajouter un repas · Soir", exact: true })
    .click();
  await page.locator("#meal-text").fill("Ce soir du riz");
  await page.getByRole("button", { name: "Envoyer à Luna" }).click();
  await expect(page.locator("#intake-kcal")).toContainText("260", {
    timeout: 10000,
  });
  return page.evaluate(async () =>
    (
      await fetch(
        "/api/dashboard?day=" + document.getElementById("selected-day").value,
      )
    ).json(),
  );
}
test("meal and food macros use P G L and preserve a published fat limit in the daily total", async ({
  page,
}) => {
  const baseline = await savedNarrative(page);
  const meal = baseline.meals[0];
  meal.clarifications = [];
  meal.items[0].nutrients.fat = null;
  meal.items[0].nutrient_bounds = {
    fat: { lower: 0, upper: 0.9, upper_exclusive: true },
  };
  meal.totals.fat = null;
  meal.totals_bounds = meal.items[0].nutrient_bounds;
  // Add a second food whose lipids are known, so the total has a nonzero lower bound.
  meal.items.push({
    ...structuredClone(meal.items[0]),
    name: "Autre aliment",
    nutrient_bounds: {},
    nutrients: { kcal: 180, protein: 0, carbs: 0, fat: 20, fiber: 0 },
  });
  meal.totals.kcal = 440;
  meal.totals_bounds = {
    fat: { lower: 20, upper: 20.9, upper_exclusive: true },
  };
  const data = {
    ...baseline,
    intake: meal.totals,
    intake_bounds: meal.totals_bounds,
  };
  await page.route("**/api/dashboard?**", (route) =>
    route.fulfill({ json: data }),
  );
  await page.reload();
  await expect(page.locator("#macro-cards .fat .macro-number")).toContainText(
    "20–20,9",
  );
  await expect(page.locator("#today-meals .meal-macros")).toContainText(
    "L 20–20,9 g",
  );
  await page.locator('.nav-button[data-view="journal"]').click();
  await expect(
    page.locator("#journal-meals .food-nutrients").first(),
  ).toContainText("L < 0,9 g");
  await expect(page.locator("#journal-meals .meal-macros")).toContainText(
    "P 5,2 g",
  );
  await expect(page.locator("#journal-meals .meal-macros")).toContainText(
    "G 56 g",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    ),
  ).toBe(false);
});

async function reanalysisRoutes(page, baseline, options = {}) {
  let status = options.status || "queued",
    failures = options.failures || 0,
    started = false;
  const job = {
    id: "live-reanalysis",
    day: localDay(),
    text: "Ce soir du riz",
    reanalysis_meal_ids: [baseline.meals[0].id],
  };
  const meal = structuredClone(baseline.meals[0]);
  meal.id = "replacement-meal";
  meal.title = "Repas réanalysé";
  meal.totals = { ...meal.totals, kcal: 390 };
  meal.items[0].nutrients = meal.totals;
  const updated = { ...baseline, meals: [meal], intake: meal.totals };
  await page.route("**/api/meals/*/reanalyse", (route) => {
    started = true;
    return route.fulfill({
      json: { id: job.id, reanalysis_meal_ids: job.reanalysis_meal_ids },
      status: 202,
    });
  });
  await page.route("**/api/captures?**", (route) =>
    route.fulfill({
      json: {
        jobs: started
          ? [
              {
                ...job,
                status,
                error:
                  status === "failed"
                    ? "Service momentanément indisponible"
                    : null,
              },
            ]
          : [],
      },
    }),
  );
  await page.route("**/api/dashboard?**", (route) => {
    if (started && status === "done" && failures-- > 0)
      return route.fulfill({
        status: 503,
        json: { error: "Bilan temporairement indisponible" },
      });
    return route.fulfill({
      json: started && status === "done" ? updated : baseline,
    });
  });
  await page.route("**/api/captures/*/retry", (route) => {
    status = "queued";
    return route.fulfill({ json: { ok: true } });
  });
  return (value) => {
    status = value;
  };
}
test("reanalysis already finished on the first status read refreshes calories immediately", async ({
  page,
}) => {
  const baseline = await savedNarrative(page);
  await reanalysisRoutes(page, baseline, { status: "done" });
  await page
    .locator("#today-meals")
    .getByRole("button", { name: "Réanalyser la saisie" })
    .click();
  await expect(page.locator("#intake-kcal")).toContainText("390");
  await expect(page.locator("#today-meals")).toContainText("Repas réanalysé");
  await expect(page.locator("#today-meals .meal-card")).toHaveCount(1);
  await expect(page.locator("#today-captures .capture-job")).toHaveCount(0);
});
test("journal shows a persistent meal spinner and refreshes on return from background", async ({
  page,
}) => {
  const baseline = await savedNarrative(page);
  const setStatus = await reanalysisRoutes(page, baseline);
  await page
    .getByRole("button", { name: "Journal des repas", exact: true })
    .click();
  await page
    .locator("#journal-meals")
    .getByRole("button", { name: "Réanalyser la saisie" })
    .click();
  await expect(
    page.locator("#journal-meals .meal-analysis-status .loading-spinner"),
  ).toBeVisible();
  await expect(page.locator("#journal-meals .meal-card")).toHaveAttribute(
    "aria-busy",
    "true",
  );
  await expect(
    page
      .locator("#journal-meals")
      .getByRole("button", { name: "Réanalyser la saisie" }),
  ).toBeDisabled();
  await expect(page.locator("#journal-meals .meal-calories")).toContainText(
    "260",
  );
  await page.evaluate(() =>
    Object.defineProperty(document, "hidden", {
      value: true,
      writable: true,
      configurable: true,
    }),
  );
  setStatus("done");
  await page.evaluate(() => {
    document.hidden = false;
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.locator("#journal-meals .meal-calories")).toContainText(
    "390",
  );
  await expect(page.locator("#journal-meals .loading-spinner")).toHaveCount(0);
  await expect(page.locator("#journal-meals .meal-card")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  await expect(page.locator("#intake-kcal")).toContainText("390");
});
test("a failed balance refresh is retried automatically after processing finishes", async ({
  page,
}) => {
  const baseline = await savedNarrative(page);
  await reanalysisRoutes(page, baseline, { status: "done", failures: 1 });
  await page
    .locator("#today-meals")
    .getByRole("button", { name: "Réanalyser la saisie" })
    .click();
  await expect(page.locator("#global-error")).toContainText(
    "Bilan temporairement indisponible",
  );
  await expect(
    page.locator("#today-meals .meal-analysis-status .loading-spinner"),
  ).toBeVisible();
  await expect(page.locator("#intake-kcal")).toContainText("390", {
    timeout: 10000,
  });
  await expect(page.locator("#today-meals .loading-spinner")).toHaveCount(0);
  await expect(page.locator("#global-error")).not.toBeVisible();
});
test("a failed reanalysis keeps the saved meal and can be retried with live progress", async ({
  page,
}) => {
  const baseline = await savedNarrative(page);
  const setStatus = await reanalysisRoutes(page, baseline);
  await page
    .locator("#today-meals")
    .getByRole("button", { name: "Réanalyser la saisie" })
    .click();
  await expect(page.locator("#today-meals .loading-spinner")).toBeVisible();
  setStatus("failed");
  await expect(page.locator("#today-meals")).toContainText(
    "Réanalyse interrompue",
    { timeout: 10000 },
  );
  await expect(page.locator("#today-meals .loading-spinner")).toHaveCount(0);
  await expect(page.locator("#intake-kcal")).toContainText("260");
  await expect(
    page
      .locator("#today-meals")
      .getByRole("button", { name: "Réanalyser la saisie" }),
  ).toBeEnabled();
  await page
    .locator("#today-captures")
    .getByRole("button", { name: "Réessayer" })
    .click();
  await expect(page.locator("#today-meals .loading-spinner")).toBeVisible();
  setStatus("done");
  await expect(page.locator("#intake-kcal")).toContainText("390", {
    timeout: 10000,
  });
  await expect(page.locator("#today-meals .loading-spinner")).toHaveCount(0);
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
    "dépense à ce stade",
  );
  await expect(page.locator("#energy-caption")).toContainText(
    "Aucun repas saisi",
  );
  await expect(page.locator("#garmin-day")).toContainText(
    "automatique chaque heure",
  );
  await expect(page.locator("#energy-pct")).toHaveText("—");
});

test("daily consumption shows its actual target and an overshoot in red", async ({
  page,
}, info) => {
  const data = {
    day: localDay(),
    meals: [],
    has_meals: true,
    complete: true,
    intake: { kcal: 2986.9, protein: 110, carbs: 349, fat: 97 },
    targets: { kcal: 2227, protein: 110, carbs: 250, fat: 85 },
    resting: 1860,
    expenditure: 2427,
    deficit: -559.9,
    projected: false,
    garmin: null,
  };
  await page.route("**/api/dashboard?**", (route) =>
    route.fulfill({ json: data }),
  );
  await page.reload();
  await expect(page.locator("#intake-kcal")).toHaveText(
    /2\s987\s*\/ 2\s227kcal/,
  );
  await expect(page.locator(".energy-card")).toHaveClass(/is-over-goal/);
  await expect(page.locator("#energy-caption")).toHaveText(
    "760 kcal au-dessus de ton objectif.",
  );
  await expect(page.locator("#energy-pct")).toHaveText("134%");
  await expect(page.locator("#energy-progress")).toHaveCSS(
    "stroke",
    "rgb(255, 170, 163)",
  );
  await expect(page.locator("#intake-kcal")).toHaveCSS(
    "color",
    "rgb(255, 170, 163)",
  );
  await expect(page.locator("#deficit-value")).toHaveText("≈ 0,059 kg");
  await page
    .getByRole("combobox", { name: "Unité des bilans" })
    .selectOption("kcal");
  await expect(page.locator("#deficit-value")).toHaveText("560 kcal");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    ),
  ).toBe(false);
  await page.screenshot({
    path: info.outputPath("calorie-target.png"),
    fullPage: true,
  });
  data.intake.kcal = 2000;
  await page.locator("#previous-day").click();
  await expect(page.locator(".energy-card")).not.toHaveClass(/is-over-goal/);
  await expect(page.locator("#energy-caption")).toHaveText(
    "227 kcal jusqu’à ton objectif.",
  );
  await expect(page.locator("#energy-progress")).not.toHaveCSS(
    "stroke",
    "rgb(255, 170, 163)",
  );
  data.intake.kcal = 2227;
  await page.locator("#next-day").click();
  await expect(page.locator("#energy-caption")).toHaveText("Objectif atteint.");
  await expect(page.locator(".energy-card")).not.toHaveClass(/is-over-goal/);
  data.intake.kcal = null;
  await page.locator("#previous-day").click();
  await expect(page.locator("#intake-kcal")).toHaveText(/—\s*\/ 2\s227kcal/);
  await expect(page.locator(".energy-card")).not.toHaveClass(/is-over-goal/);
  await expect(page.locator("#energy-caption")).toContainText("manquantes");
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
  const zeroY = Number(
    await page.locator("#daily-deficit-chart .zero-line").getAttribute("y1"),
  );
  expect(Number(await daily.first().getAttribute("y"))).toBe(zeroY);
  expect(Number(await daily.nth(1).getAttribute("y"))).toBeLessThan(zeroY);
  await expect(daily.first()).toHaveCSS("fill", "rgb(91, 150, 47)");
  await expect(daily.nth(1)).toHaveCSS("fill", "rgb(193, 91, 77)");
  const cumulative = page.locator("#cumulative-deficit-chart .deficit-point");
  expect(
    await cumulative.evaluateAll((els) =>
      els.map((e) => Number(e.dataset.value)),
    ),
  ).toEqual([300, 200, 200]);
  await expect(
    page.locator("#cumulative-deficit-chart .deficit-line"),
  ).toHaveCount(1);
  await expect(page.locator("#dashboard-kpis")).toContainText("≈ 0,021 kg");
  await page
    .getByRole("combobox", { name: "Unité des bilans" })
    .selectOption("kcal");
  await expect(page.locator("#dashboard-kpis")).toContainText("200 kcal");
  const energyZeroY = Number(
    await page.locator("#daily-deficit-chart .zero-line").getAttribute("y1"),
  );
  expect(Number(await daily.first().getAttribute("y"))).toBeLessThan(
    energyZeroY,
  );
  expect(Number(await daily.nth(1).getAttribute("y"))).toBe(energyZeroY);
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

test("fat equivalents are the default across balances charts and trends and the switch is remembered", async ({
  page,
}, info) => {
  const pastDay = (offset) => {
    const date = new Date();
    date.setDate(date.getDate() - offset);
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
  };
  const days = [
    {
      day: pastDay(3),
      deficit: 944,
      has_meals: true,
      complete: true,
      intake: { kcal: 1500 },
      expenditure: 2444,
    },
    {
      day: pastDay(2),
      deficit: null,
      has_meals: false,
      complete: false,
      intake: { kcal: 0 },
      expenditure: 2100,
    },
    {
      day: pastDay(1),
      deficit: -472,
      has_meals: true,
      complete: true,
      intake: { kcal: 2500 },
      expenditure: 2028,
    },
    {
      day: pastDay(0),
      deficit: 800,
      has_meals: true,
      complete: true,
      intake: { kcal: 1500 },
      expenditure: 2300,
      projected: true,
    },
  ];
  let reads = 0;
  await page.route("**/api/trends?**", (route) => {
    reads++;
    return route.fulfill({
      json: {
        days,
        cumulative_deficit: 472,
        covered_days: 2,
        elapsed_days: 3,
        weights: [],
      },
    });
  });
  await page.route("**/api/dashboard?**", (route) =>
    route.fulfill({
      json: {
        day: new URL(route.request().url()).searchParams.get("day"),
        meals: [],
        has_meals: true,
        complete: true,
        intake: { kcal: 2500, protein: 100, carbs: 250, fat: 90 },
        targets: { kcal: 1800, protein: 100, carbs: 200, fat: 80 },
        expenditure: 2028,
        resting: 1800,
        deficit: -472,
        projected: false,
        garmin: null,
      },
    }),
  );
  await page.reload();
  const unit = page.getByRole("combobox", { name: "Unité des bilans" });
  await expect(unit).toHaveValue("fat");
  await expect(page.locator("#deficit-value")).toHaveText("≈ 0,05 kg");
  await expect(page.locator("#dashboard-kpis")).toContainText("≈ 0,05 kg");
  await expect(page.locator("#dashboard-kpis")).toContainText(
    "≈ 0,025 kg / jour",
  );
  await expect(page.locator("#daily-deficit-chart .deficit-bar")).toHaveCount(
    2,
  );
  await expect(
    page.locator("#daily-deficit-chart .deficit-bar title").first(),
  ).toHaveText(/Variation équivalente : ≈ -0,1 kg · En déficit/);
  await expect(page.locator("#daily-deficit-chart .surplus title")).toHaveText(
    /Variation équivalente : ≈ \+0,05 kg · En surplus/,
  );
  await expect(
    page.locator("#cumulative-deficit-chart .deficit-point title").last(),
  ).toHaveText(/≈ -0,05 kg/);
  await expect(page.locator("[data-balance-unit]").first()).toHaveText(
    "kg équiv. gras",
  );
  const readCount = reads;
  await unit.selectOption("kcal");
  await expect(page.locator("#deficit-value")).toHaveText("472 kcal");
  await expect(page.locator("#dashboard-kpis")).toContainText("472 kcal");
  await expect(
    page.locator("#daily-deficit-chart .deficit-bar title").first(),
  ).toHaveText(/Bilan : 944 kcal · En déficit/);
  expect(reads).toBe(readCount);
  await page.locator('.nav-button[data-view="trends"]').click();
  await expect(unit).toHaveValue("kcal");
  await expect(page.locator("#trend-kpis")).toContainText("472 kcal");
  const rawChart = await page.locator("#trend-chart").innerHTML();
  await unit.selectOption("fat");
  await expect(page.locator("#trend-kpis")).toContainText(
    "≈ 0,05 kg équiv. gras",
  );
  await expect(page.locator("#trend-table")).toContainText("≈ 0,05 kg");
  await expect(page.locator("#trend-table")).toContainText("Provisoire");
  expect(await page.locator("#trend-chart").innerHTML()).toBe(rawChart);
  await page.reload();
  await expect(unit).toHaveValue("fat");
  await expect(page.locator("#trend-kpis")).toContainText(
    "≈ 0,05 kg équiv. gras",
  );
  await unit.selectOption("kcal");
  await page.reload();
  await expect(unit).toHaveValue("kcal");
  await expect(page.locator("#trend-kpis")).toContainText("472 kcal");
  await unit.selectOption("fat");
  await page.locator('.nav-button[data-view="today"]').click();
  await expect(page.locator("#deficit-value")).toHaveText("≈ 0,05 kg");
  await page.locator(".balance-conversion summary").click();
  await expect(page.locator(".balance-conversion")).toContainText("9 440 kcal");
  await expect(page.locator(".balance-conversion a")).toHaveAttribute(
    "href",
    /10.1371\/journal.pcbi.1000045/,
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    ),
  ).toBe(false);
  await page.screenshot({
    path: info.outputPath("fat-equivalents.png"),
    fullPage: true,
  });
});

test("active Garmin calories update the goal ratio gauge and overshoot after refresh", async ({
  page,
}, info) => {
  let active = 100;
  await page.route("**/api/dashboard?**", (route) => {
    const expenditure = 2300 + active,
      goal = expenditure - 200;
    return route.fulfill({
      json: {
        day: new URL(route.request().url()).searchParams.get("day"),
        meals: [],
        has_meals: true,
        complete: true,
        intake: { kcal: 2300, protein: 100, carbs: 250, fat: 90 },
        targets: {
          kcal: goal,
          protein: Math.round(goal / 20),
          carbs: Math.round((goal * 0.45) / 4),
          fat: Math.round((goal * 0.35) / 9),
        },
        expenditure,
        expenditure_source: "Repos estimé sur 24 h + calories actives Garmin",
        resting: 2300,
        deficit: expenditure - 2300,
        projected: true,
        target_deficit: 200,
        target_breakdown: {
          base: 2300,
          active,
          deficit: 200,
          base_source: "Repos estimé sur 24 h",
        },
        garmin: {
          has_data: true,
          partial: true,
          active,
          resting: 450,
          total: 450 + active,
          synced_at: new Date().toISOString(),
          activities: [],
        },
      },
    });
  });
  await page.reload();
  await expect(page.locator("#intake-kcal")).toHaveText(
    /2\s300\s*\/ 2\s200kcal/,
  );
  await expect(page.locator("#energy-caption")).toHaveText(
    "100 kcal au-dessus de ton objectif.",
  );
  await expect(page.locator(".energy-card")).toHaveClass(/is-over-goal/);
  active = 600;
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await expect(page.locator("#intake-kcal")).toHaveText(
    /2\s300\s*\/ 2\s700kcal/,
  );
  await expect(page.locator("#target-value")).toHaveText(/2\s700 kcal/);
  await expect(page.locator("#target-deficit")).toBeVisible();
  await expect(page.locator("#target-deficit")).toHaveText(
    "Déficit cible : 200 kcal",
  );
  await expect(page.locator("#energy-pct")).toHaveText("85%");
  await expect(page.locator("#energy-caption")).toHaveText(
    "400 kcal jusqu’à ton objectif.",
  );
  await expect(page.locator(".energy-card")).not.toHaveClass(/is-over-goal/);
  await expect(page.locator("#expenditure-value")).toHaveText(/2\s900 kcal/);
  await expect(page.locator("#expenditure-label")).toHaveText(
    "Dépense projetée sur 24 h",
  );
  await expect(page.locator("#garmin-day")).toContainText(/1\s050/);
  await expect(page.locator("#garmin-day")).toContainText(
    "Fonctionnement du corps · Garmin",
  );
  await expect(page.locator("#expenditure-note")).toContainText(
    /2\s300 \+ 600 kcal actives − 200/,
  );
  await expect(page.locator("#macro-cards .protein .macro-target")).toHaveText(
    "Objectif 135 g",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    ),
  ).toBe(false);
  await page.screenshot({
    path: info.outputPath("active-calorie-target.png"),
    fullPage: true,
  });
});

test("cumulative deficit changes color at zero and keeps gaps in both units", async ({
  page,
}, info) => {
  const deficits = [300, -600, 400, null, -200, 400];
  const days = deficits.map((deficit, i) => {
    const day = new Date();
    day.setDate(day.getDate() - deficits.length + i);
    return {
      day: `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`,
      deficit,
    };
  });
  await page.route("**/api/trends?**", (route) =>
    route.fulfill({
      json: {
        days,
        cumulative_deficit: 300,
        covered_days: 5,
        elapsed_days: 6,
        weights: [],
      },
    }),
  );
  await page.locator("#dashboard-period").selectOption("7");
  const chart = page.locator("#cumulative-deficit-chart");
  await expect(chart.locator(".deficit-line")).toHaveCount(5);
  await expect(chart.locator(".deficit-line.surplus")).toHaveCount(2);
  await expect(chart.locator(".deficit-point.surplus")).toHaveCount(2);
  const unit = page.getByRole("combobox", { name: "Unité des bilans" });
  for (const name of ["fat", "kcal"]) {
    await unit.selectOption(name);
    const zeroY = Number(await chart.locator(".zero-line").getAttribute("y1"));
    for (const line of await chart.locator(".deficit-line").all()) {
      const red = await line.evaluate((el) => el.classList.contains("surplus"));
      const coordinates = (await line.getAttribute("points"))
        .split(" ")
        .map((pair) => pair.split(",").map(Number));
      for (const [, y] of coordinates) {
        if (red === (name === "kcal"))
          expect(y).toBeGreaterThanOrEqual(zeroY - 0.001);
        else expect(y).toBeLessThanOrEqual(zeroY + 0.001);
      }
      await expect(line).toHaveCSS(
        "stroke",
        red ? "rgb(193, 91, 77)" : "rgb(91, 150, 47)",
      );
    }
    const gapX = Number(
      await page
        .locator("#daily-deficit-chart .deficit-bar")
        .nth(3)
        .getAttribute("x"),
    );
    // The cumulative lines stop before the unlogged day and resume afterwards.
    for (const line of await chart.locator(".deficit-line").all()) {
      const xs = (await line.getAttribute("points"))
        .split(" ")
        .map((pair) => Number(pair.split(",")[0]));
      expect(Math.min(...xs) < gapX && Math.max(...xs) > gapX).toBe(false);
    }
    await expect(page.locator(".deficit-legend").first()).toContainText(
      name === "fat" ? "Perte équiv. ↓" : "En déficit ↑",
    );
    if (name === "fat") {
      await expect(chart.locator(".balance-axis-deficit").last()).toHaveText(
        /^-/,
      );
      await expect(chart.locator(".balance-axis-surplus").first()).toHaveText(
        /^\+/,
      );
    }
    await page.screenshot({
      path: info.outputPath(`balance-chart-${name}.png`),
      fullPage: true,
    });
  }
});

test("energy balances use unsigned amounts and explicit tags for deficit surplus equilibrium and missing days", async ({
  page,
}, info) => {
  let balance = -600;
  const days = [300, -900, 0, null].map((deficit, i) => {
    const day = new Date();
    day.setDate(day.getDate() - 4 + i);
    return {
      day: `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`,
      deficit,
      expenditure: 2000,
      intake: { kcal: deficit == null ? 0 : 2000 - deficit },
      has_meals: deficit != null,
      complete: deficit != null,
      projected: false,
    };
  });
  await page.route("**/api/dashboard?**", (route) =>
    route.fulfill({
      json: {
        day: new URL(route.request().url()).searchParams.get("day"),
        meals: [],
        has_meals: balance != null,
        complete: balance != null,
        intake: {
          kcal: balance == null ? 0 : 2000 - balance,
          protein: 80,
          carbs: 200,
          fat: 70,
        },
        targets: { kcal: 1800, protein: 90, carbs: 203, fat: 70 },
        expenditure: 2000,
        resting: 2000,
        projected: true,
        garmin: null,
        deficit: balance,
        target_deficit: 200,
      },
    }),
  );
  await page.route("**/api/trends?**", (route) =>
    route.fulfill({
      json: {
        days,
        cumulative_deficit: -600,
        covered_days: 3,
        elapsed_days: 4,
        weights: [],
      },
    }),
  );
  await page.reload();
  const unit = page.getByRole("combobox", { name: "Unité des bilans" });
  const value = page.locator("#deficit-value");
  const tag = page.locator("#balance-status .balance-tag");
  for (const mode of ["fat", "kcal"]) {
    await unit.selectOption(mode);
    for (const [amount, label, kind] of [
      [600, "En déficit", "deficit"],
      [-600, "En surplus", "surplus"],
      [0, "À l’équilibre", "even"],
    ]) {
      balance = amount;
      await page.evaluate(() =>
        document.dispatchEvent(new Event("visibilitychange")),
      );
      await expect(tag).toHaveClass(new RegExp(`balance-${kind}`));
      await expect(tag).toContainText(label);
      await expect(value).toHaveText(
        mode === "fat"
          ? amount === 0
            ? "≈ 0 kg"
            : "≈ 0,064 kg"
          : `${Math.abs(amount)} kcal`,
      );
    }
    balance = null;
    await page.evaluate(() =>
      document.dispatchEvent(new Event("visibilitychange")),
    );
    await expect(value).toHaveText("—");
    await expect(tag).toHaveCount(0);
    await expect(page.locator("#dashboard-kpis .balance-surplus")).toHaveCount(
      2,
    );
    await expect(page.locator("#dashboard-kpis")).toContainText(
      mode === "fat" ? "≈ 0,064 kg" : "600 kcal",
    );
    await page.locator('.nav-button[data-view="trends"]').click();
    await expect(page.locator("#trend-kpis .balance-surplus")).toHaveCount(1);
    await expect(page.locator("#trend-table .balance-deficit")).toHaveCount(1);
    await expect(page.locator("#trend-table .balance-surplus")).toHaveCount(1);
    await expect(page.locator("#trend-table .balance-even")).toHaveCount(1);
    await expect(
      page.locator("#trend-table tr").first().locator(".balance-tag"),
    ).toHaveCount(0);
    await page.locator('.nav-button[data-view="today"]').click();
  }
  balance = -600;
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await expect(tag).toContainText("En surplus");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > innerWidth + 1,
    ),
  ).toBe(false);
  await page.screenshot({
    path: info.outputPath("energy-balance-status.png"),
    fullPage: true,
  });
});
