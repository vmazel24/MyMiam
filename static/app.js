"use strict";
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
const fmt = (value, decimals = 0) =>
  value == null
    ? "—"
    : new Intl.NumberFormat("fr-FR", {
        maximumFractionDigits: decimals,
      }).format(value);
const slots = {
  breakfast: "Matin",
  lunch: "Midi",
  dinner: "Soir",
  snack: "Collation",
};
const symbols = { breakfast: "☀", lunch: "◉", dinner: "☾", snack: "✧" };
const macroNames = { protein: "Protéines", carbs: "Glucides", fat: "Lipides" };
const macroShort = { protein: "P", carbs: "G", fat: "L" };
// Chow & Hall (2008), doi:10.1371/journal.pcbi.1000045: rho_F = 39.5 MJ/kg.
// 1 kcal = 4.184 kJ. This is an energy equivalent, not measured fat loss.
const KCAL_PER_KG_FAT = 39500 / 4.184;
const state = {
  user: null,
  view: "today",
  day: today(),
  summary: null,
  favorites: [],
  integrations: null,
  draft: [],
  editId: null,
  requestId: null,
  profile: null,
  jobs: [],
  slotHint: null,
  balanceUnit: "fat",
  dashboardHistory: null,
  trends: null,
};
let toastTimer, integrationTimer, dashboardTimer, previewTimer, captureTimer;
const appliedCaptures = new Set();
const captureReads = new Map();
let dashboardRead = 0;
let recognition = null,
  dictationStopped = null;

function balanceNumber(kcal) {
  return fmt(
    kcal == null
      ? null
      : state.balanceUnit === "fat"
        ? kcal / KCAL_PER_KG_FAT
        : kcal,
    state.balanceUnit === "fat" ? 3 : 0,
  );
}
function balanceUnitLabel() {
  return state.balanceUnit === "fat" ? "kg équiv. gras" : "kcal";
}
function balanceValue(kcal, perDay = false) {
  if (kcal == null) return "—";
  return `${state.balanceUnit === "fat" ? "≈ " : ""}${balanceNumber(kcal)} ${state.balanceUnit === "fat" ? "kg" : "kcal"}${perDay ? " / jour" : ""}`;
}
function syncBalanceUnit() {
  $("balance-unit").value = state.balanceUnit;
  $("balance-controls").hidden = !["today", "trends"].includes(state.view);
  $("balance-label").textContent =
    state.balanceUnit === "fat" ? "Bilan · équiv. gras" : "Bilan calorique";
  $("trend-balance-heading").textContent = `Déficit · ${balanceUnitLabel()}`;
  document.querySelectorAll("[data-balance-unit]").forEach((el) => {
    el.textContent = balanceUnitLabel();
  });
}
async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      credentials: "same-origin",
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(options.headers || {}),
      },
      ...(options.body === undefined
        ? {}
        : { body: JSON.stringify(options.body) }),
    });
  } catch {
    throw new Error(
      "Connexion indisponible. Ton brouillon reste ouvert ; réessaie quand le réseau revient.",
    );
  }
  const data = await response.json().catch(() => ({}));
  if (response.status === 401 && !path.startsWith("/api/auth/")) {
    showLogin();
    throw new Error("Ta session a expiré. Reconnecte-toi à Renfo.");
  }
  if (!response.ok)
    throw new Error(data.error || "Cette opération n’a pas abouti. Réessaie.");
  return data;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 7000);
}
function globalError(error) {
  $("global-error").textContent = error.message || String(error);
  $("global-error").hidden = false;
}
async function busy(button, action) {
  if (button.disabled) return;
  button.disabled = true;
  const old = button.textContent;
  button.textContent = "Un instant…";
  try {
    return await action();
  } finally {
    button.disabled = false;
    button.textContent = old;
  }
}
function showLogin() {
  if ($("meal-dialog").open) $("meal-dialog").close();
  $("app").hidden = true;
  $("login-screen").hidden = false;
  state.user = null;
  state.summary = null;
  state.profile = null;
  state.draft = [];
  state.favorites = [];
  state.integrations = null;
  $("meal-text").value = "";
  clearInterval(integrationTimer);
  clearInterval(dashboardTimer);
  clearInterval(captureTimer);
  state.jobs = [];
  state.dashboardHistory = null;
  state.trends = null;
  state.balanceUnit = "fat";
  appliedCaptures.clear();
  stopDictation();
}
async function showApp(user) {
  state.user = user;
  state.balanceUnit = "fat";
  try {
    if (localStorage.getItem(`mymiam:balance-unit:${user.id}`) === "kcal")
      state.balanceUnit = "kcal";
  } catch {
    /* Keep the default when browser storage is unavailable. */
  }
  syncBalanceUnit();
  $("login-screen").hidden = true;
  $("app").hidden = false;
  $("account-name").textContent = user.email;
  $("selected-day").value = state.day;
  $("selected-day").max = today();
  $("meal-day").max = today();
  await loadAll();
  clearInterval(integrationTimer);
  integrationTimer = setInterval(() => {
    if (state.user && !document.hidden) loadIntegrations().catch(() => {});
  }, 15000);
  clearInterval(dashboardTimer);
  dashboardTimer = setInterval(refreshVisibleDashboard, 60000);
  clearInterval(captureTimer);
  captureTimer = setInterval(() => {
    if (state.user && !document.hidden && state.jobs.some(captureLoading))
      loadCaptures().catch(globalError);
  }, 3000);
}
async function loadAll() {
  $("global-error").hidden = true;
  const results = await Promise.allSettled([
    loadDashboard(),
    loadFavorites(),
    loadProfile(),
    loadIntegrations(),
  ]);
  for (const result of results)
    if (result.status === "rejected") globalError(result.reason);
  await loadCaptures().catch(globalError);
  if (state.view === "trends") await loadTrends().catch(globalError);
}
async function loadDashboard() {
  const selected = state.day,
    user = state.user,
    read = ++dashboardRead;
  const result = await api(`/api/dashboard?day=${selected}`);
  if (
    selected !== state.day ||
    !user ||
    user !== state.user ||
    read !== dashboardRead
  )
    return false;
  state.summary = result;
  renderDashboard();
  renderMeals();
  await loadDashboardHistory();
  return (
    selected === state.day && user === state.user && read === dashboardRead
  );
}
async function loadFavorites() {
  const data = await api("/api/favorites");
  if (!state.user) return;
  state.favorites = data.favorites;
  renderFavorites();
}
async function loadProfile() {
  const data = await api("/api/profile");
  if (!state.user) return;
  state.profile = data.profile;
  if (data.profile)
    for (const [key, value] of Object.entries(data.profile)) {
      const input = $("profile-form").elements.namedItem(key);
      if (input) input.value = value;
    }
  renderMacroReference();
}
function renderMacroReference() {
  const p = state.profile;
  $("macro-reference").textContent = p
    ? `Objectifs : ${fmt(p.protein_pct)} % protéines · ${fmt(p.carbs_pct)} % glucides · ${fmt(p.fat_pct)} % lipides. Repères Anses disponibles dans ton profil.`
    : "Point de départ : 20 % protéines · 45 % glucides · 35 % lipides, dans les intervalles Anses. Complète ton profil pour calculer les grammes.";
}
async function loadIntegrations() {
  const data = await api("/api/integrations");
  if (!state.user) return;
  state.integrations = data;
  const cg = data.chatgpt,
    ga = data.garmin;
  $("chatgpt-status").textContent = cg.connected
    ? `Compte connecté${cg.email ? " · " + cg.email : ""}. ${cg.model ? "Modèle : " + cg.model + "." : "Luna reste à vérifier."}`
    : "Connecte ton forfait une première fois depuis le PC qui héberge MyMiam.";
  $("connect-chatgpt").textContent = cg.connected
    ? "Renouveler ma connexion"
    : "Continue with ChatGPT";
  $("refresh-models").hidden = !cg.connected;
  $("parse-meal").disabled = !cg.connected;
  $("parse-meal").title = cg.connected
    ? ""
    : "Connecte ton forfait depuis Profil & connexions. La saisie manuelle est disponible.";
  $("garmin-status").textContent =
    ga.error ||
    {
      connecting: "Connexion Garmin en cours…",
      mfa: "Garmin attend ton code de vérification.",
    }[ga.state] ||
    (ga.connected
      ? "Garmin est connecté · synchronisation automatique chaque heure."
      : "Pas encore connecté.");
  $("garmin-form").hidden =
    ga.connected || ga.state === "connecting" || ga.state === "mfa";
  $("garmin-mfa").hidden = ga.state !== "mfa";
  $("sync-garmin").hidden = !ga.connected;
  if (data.catalogue)
    $("catalogue-status").textContent =
      `${fmt(data.catalogue.foods)} aliments Ciqual 2025 · Anses, Licence Ouverte. Produits Open Food Facts · ODbL.`;
}
function renderDashboard() {
  const s = state.summary,
    goal = s.targets?.kcal;
  const logged = s.has_meals || s.complete;
  const ga = s.garmin;
  const intake = logged ? s.intake.kcal : null;
  const overGoal = intake != null && goal != null && intake > goal;
  document
    .querySelector(".energy-card")
    .classList.toggle("is-over-goal", overGoal);
  $("intake-kcal").innerHTML =
    `<span>${fmt(intake)}</span><span class="intake-goal">/ ${fmt(goal)}<small>kcal</small></span>`;
  $("intake-kcal").setAttribute(
    "aria-label",
    `Calories consommées : ${fmt(intake)} kcal ; objectif : ${fmt(goal)} kcal`,
  );
  $("expenditure-label").textContent = ga?.has_data
    ? ga.partial
      ? "Garmin · dépensé jusqu’ici"
      : "Garmin · dépense de la journée"
    : "Dépense estimée sur 24 h";
  const displayedExpenditure = ga?.has_data ? ga.total : s.expenditure;
  $("expenditure-value").textContent =
    displayedExpenditure == null ? "—" : fmt(displayedExpenditure) + " kcal";
  $("target-value").textContent = goal == null ? "—" : fmt(goal) + " kcal";
  $("deficit-value").textContent =
    s.deficit == null
      ? "—"
      : `${state.balanceUnit === "fat" ? "≈ " : ""}${s.deficit === 0 ? "" : s.deficit > 0 ? "−" : "+"}${balanceNumber(Math.abs(s.deficit))} ${state.balanceUnit === "fat" ? "kg" : "kcal"}`;
  const pct = goal && logged ? Math.round((s.intake.kcal / goal) * 100) : null;
  $("energy-pct").textContent =
    pct == null || s.intake.kcal == null ? "—" : pct + "%";
  $("energy-progress").setAttribute(
    "stroke-dasharray",
    `${pct == null || s.intake.kcal == null ? 0 : Math.max(0, Math.min(100, pct))} 100`,
  );
  $("energy-caption").textContent =
    s.intake.kcal == null
      ? "Certaines valeurs alimentaires sont manquantes."
      : !s.has_meals
        ? "Aucun repas saisi. Garmin renseigne la dépense, pas les apports."
        : goal == null
          ? "Complète ton profil pour définir ton objectif."
          : overGoal
            ? `${fmt(s.intake.kcal - goal, s.intake.kcal - goal < 1 ? 2 : 0)} kcal au-dessus de ton objectif.`
            : s.intake.kcal === goal
              ? "Objectif atteint."
              : `${fmt(goal - s.intake.kcal)} kcal jusqu’à ton objectif.`;
  $("day-state").textContent = s.projected
    ? "Journée en cours"
    : s.complete
      ? "Bilan actualisé"
      : "Journée non saisie";
  $("expenditure-note").textContent =
    ga?.has_data && ga.partial
      ? `Le total Garmin est provisoire. ${s.expenditure == null ? "Complète ton profil pour définir un objectif sur 24 h." : `Objectif basé sur une dépense projetée de ${fmt(s.expenditure)} kcal sur 24 h.`}`
      : s.expenditure == null
        ? "Complète ton profil pour calculer tes repères."
        : `${s.expenditure_source}${s.projected ? " · bilan provisoire" : ""}.${s.resting == null ? "" : ` Repos estimé : ${fmt(s.resting)} kcal.`}`;
  if (s.target_breakdown && goal != null) {
    const b = s.target_breakdown;
    $("expenditure-note").textContent =
      `${b.base_source} : ${fmt(b.base)} + ${fmt(b.active)} kcal actives − ${fmt(b.deficit)} kcal de déficit cible = ${fmt(goal)} kcal d’objectif.${s.projected ? " Objectif provisoire, actualisé à chaque synchronisation Garmin." : ""}`;
  }
  $("macro-cards").innerHTML = Object.entries(macroNames)
    .map(([key, name]) => {
      const value = logged ? s.intake[key] : null,
        bounds = logged ? s.intake_bounds?.[key] : null,
        target = s.targets?.[key],
        pct =
          target && (value != null || bounds)
            ? Math.min(100, ((value ?? bounds.lower) / target) * 100)
            : 0;
      return `<article class="macro-card ${key}"><div class="macro-top">${name}</div><div class="macro-number">${nutrientValue(value, bounds)} <small>g</small></div>${bounds ? '<p class="footnote">Plage calculée depuis les limites de la source.</p>' : ""}<p class="macro-target">${target == null ? "Objectif à définir" : `Objectif ${fmt(target)} g`}</p><svg class="macro-bar" viewBox="0 0 100 4" preserveAspectRatio="none" aria-label="Progression ${name}"><rect width="${pct}" height="4" rx="2"></rect></svg></article>`;
    })
    .join("");
  $("garmin-day").innerHTML = ga?.has_data
    ? `<div class="activity-values"><div><strong>${fmt(ga.total)} <small>kcal</small></strong><span>Total ${ga.partial ? "observé jusqu’ici" : "de la journée"}</span></div><div><strong>${fmt(ga.active)} <small>kcal</small></strong><span>Calories actives, déjà incluses</span></div><div><strong>${fmt(ga.resting)} <small>kcal</small></strong><span>Repos ${ga.partial ? "accumulé" : "Garmin"}</span></div></div><p class="muted footnote">${ga.partial ? (s.target_breakdown ? "Données provisoires : l’objectif associe le repos estimé sur 24 h aux calories actives Garmin du jour." : "Données provisoires : complète ton profil et synchronise tes calories actives pour ajuster l’objectif.") : "Le total Garmin prend le relais de l’estimation du profil."} Synchronisation automatique chaque heure. Dernière mise à jour : ${escapeHTML(new Date(ga.synced_at).toLocaleString("fr-FR"))}.</p>`
    : `<p class="muted">Aucune donnée Garmin pour cette journée. ${state.integrations?.garmin?.connected ? "Tu peux lancer une synchronisation depuis ton profil." : "Connecte Garmin depuis ton profil pour suivre ta dépense."}</p>`;
  if (ga?.activities?.length)
    $("garmin-day").innerHTML +=
      '<div class="spaced">' +
      ga.activities
        .map(
          (a) =>
            `<div class="section-head garmin-activity"><span>${escapeHTML(a.name)} · ${a.duration == null ? "—" : fmt(a.duration / 60)} min</span><strong>${fmt(a.calories)} kcal</strong></div>`,
        )
        .join("") +
      '<p class="muted footnote">Détail des séances ; leurs calories ne sont pas ajoutées au total une seconde fois.</p></div>';
}
function nutrientValue(value, bounds, decimals = 1) {
  if (value != null || !bounds) return fmt(value, decimals);
  if (bounds.lower === 0)
    return `${bounds.upper_exclusive ? "<" : "≤"} ${fmt(bounds.upper, 2)}`;
  return `${fmt(bounds.lower, 2)}–${fmt(bounds.upper, 2)}`;
}
function macroLine(nutrients, bounds = {}) {
  return `<div class="meal-macros" aria-label="Macros du repas">${Object.entries(
    macroShort,
  )
    .map(([key, label]) => {
      const value = nutrientValue(nutrients[key], bounds[key]);
      return `<span title="${macroNames[key]}" aria-label="${macroNames[key]} : ${escapeHTML(value)} g"><b>${label}</b> ${escapeHTML(value)} g</span>`;
    })
    .join("")}</div>`;
}
function nutrientLine(nutrients = {}, bounds = {}) {
  return `<div class="food-nutrients" aria-label="Valeurs pour cette portion">${[
    "kcal",
    "protein",
    "carbs",
    "fat",
  ]
    .map((key) => {
      const value = nutrientValue(
        nutrients[key],
        bounds[key],
        key === "kcal" ? 0 : 1,
      );
      return `<span title="${macroNames[key] || "Énergie"}" aria-label="${macroNames[key] || "Énergie"} : ${escapeHTML(value)} ${key === "kcal" ? "kcal" : "g"}"><strong>${key === "kcal" ? "" : macroShort[key] + " "}${escapeHTML(value)} ${key === "kcal" ? "kcal" : "g"}</strong></span>`;
    })
    .join("")}</div>`;
}
function captureLoading(job) {
  return (
    ["queued", "analysing"].includes(job.status) ||
    (job.status === "done" && !appliedCaptures.has(job.id))
  );
}
function mealAnalysisStatus(meal) {
  const job = [...state.jobs]
    .reverse()
    .find(
      (j) =>
        j.reanalysis_meal_ids?.includes(meal.id) && j.status !== "cancelled",
    );
  if (!job) return "";
  if (captureLoading(job))
    return `<p class="meal-analysis-status" role="status"><span class="loading-spinner" aria-hidden="true"></span>${job.status === "done" ? "Mise à jour du bilan…" : "Luna réanalyse cette saisie…"}</p>`;
  if (job.status === "failed")
    return `<p class="review-note" role="status">Réanalyse interrompue : ${escapeHTML(job.error)}. Le repas enregistré est conservé.</p>`;
  return "";
}
function renderMeals() {
  if (!state.summary || state.summary.day !== state.day) return;
  const meals = [...state.summary.meals].sort(
    (a, b) =>
      Object.keys(slots).indexOf(a.slot) - Object.keys(slots).indexOf(b.slot),
  );
  const foodMarkup = (items) =>
    items
      .map(
        (i) =>
          `<div class="meal-food"><p>${escapeHTML(i.label || i.name)} · ${fmt(i.grams, 1)} g${i.estimated ? " ≈" : ""}</p>${nutrientLine(i.nutrients, i.nutrient_bounds)}<p>${escapeHTML(i.source)}${i.composition_estimated ? " · composition estimée" : ""}${i.source_url && /^https:\/\//i.test(i.source_url) ? ` · <a href="${escapeHTML(i.source_url)}" target="_blank" rel="noopener noreferrer">Voir la source</a>` : ""}${i.note ? " · " + escapeHTML(i.note) : ""}${Object.keys(i.flags || {}).some((key) => !i.nutrient_bounds?.[key]) ? " · certaines valeurs non chiffrées" : ""}</p></div>`,
      )
      .join("");
  const mealMarkup = (m, expanded) =>
    `<article class="meal-card" data-meal-id="${m.id}" aria-busy="${state.jobs.some((j) => j.reanalysis_meal_ids?.includes(m.id) && captureLoading(j))}"><div class="meal-symbol" aria-hidden="true">${symbols[m.slot]}</div><div class="meal-info"><h3>${escapeHTML(m.title)}</h3><p>${slots[m.slot]} · ${m.items.length} aliment${m.items.length > 1 ? "s" : ""}${m.items.some((i) => i.estimated) ? " · poids approximatifs" : ""}</p>${macroLine(m.totals, m.totals_bounds)}${expanded ? foodMarkup(m.items) : `<details class="meal-food-details"><summary>Voir les aliments</summary>${foodMarkup(m.items)}</details>`}${m.items.some((i) => i.nutrients.kcal == null) ? `<p class="review-note">Bilan incomplet : ${m.items.filter((i) => i.nutrients.kcal == null).length} aliment(s) sans calories estimées.</p>` : ""}${mealAnalysisStatus(m)}${m.text?.trim().length >= 3 ? `<button class="text-button reanalyse-meal" ${state.jobs.some((j) => j.reanalysis_meal_ids?.includes(m.id) && captureLoading(j)) ? "disabled" : ""} data-reanalyse-meal="${m.id}" title="Relancer Luna sur le récit d’origine et remplacer son analyse">Réanalyser la saisie ↻</button>` : ""}</div><div class="meal-calories">${fmt(m.totals.kcal)} <small>kcal</small></div><div class="meal-actions"><button data-edit="${m.id}" aria-label="Modifier ${escapeHTML(m.title)}" title="Modifier">✎</button><button data-duplicate="${m.id}" aria-label="Réutiliser ${escapeHTML(m.title)}" title="Réutiliser">⧉</button><button data-favorite="${m.id}" aria-label="Garder comme habitude" title="Garder comme habitude">☆</button>${expanded ? `<button data-delete="${m.id}" aria-label="Supprimer ${escapeHTML(m.title)}" title="Supprimer">×</button>` : ""}</div></article>`;
  const refinements = (meal) =>
    (meal.clarifications || [])
      .map((group, index) =>
        group.resolved
          ? ""
          : `<div class="meal-refinement"><p><strong>${escapeHTML(group.label)}</strong> <span>Précision facultative</span></p><div class="choice-row">${group.options.map((option, oi) => `<button class="choice-button ${group.selected === oi ? "selected" : ""}" aria-pressed="${group.selected === oi}" data-refine-meal="${meal.id}" data-refine-group="${index}" data-refine-option="${oi}">${escapeHTML(option.label)}</button>`).join("")}</div></div>`,
      )
      .join("");
  const markup = (expanded) =>
    Object.entries(slots)
      .map(([slot, label]) => {
        const group = meals.filter((meal) => meal.slot === slot);
        if (slot === "snack" && !group.length) return "";
        const kcal = group.some((meal) => meal.totals.kcal == null)
          ? null
          : group.reduce((sum, meal) => sum + meal.totals.kcal, 0);
        return `<section class="meal-period" data-slot="${slot}" aria-label="Repas du créneau ${label}"><div class="section-head meal-period-header"><h3><span aria-hidden="true">${symbols[slot]}</span> ${label}${group.length ? `<small>${fmt(kcal)} kcal</small>` : ""}</h3><button class="text-button" data-action="new-meal" data-slot="${slot}" aria-label="Ajouter un repas · ${label}">Ajouter ＋</button></div>${group.length ? group.map((meal) => `<div class="meal-entry">${mealMarkup(meal, expanded)}${refinements(meal)}</div>`).join("") : '<p class="meal-period-empty">Aucun repas saisi.</p>'}</section>`;
      })
      .join("");
  $("today-meals").innerHTML = markup(false);
  $("journal-meals").innerHTML = markup(true);
}
// Serialize reads for each account/day so an older response cannot undo a newer status.
function loadCaptures() {
  const selected = state.day,
    user = state.user;
  if (!user) return Promise.resolve();
  const key = `${user.id}:${selected}`;
  const previous = captureReads.get(key) || Promise.resolve();
  const task = previous
    .catch(() => {})
    .then(async () => {
      if (user !== state.user || selected !== state.day) return;
      const data = await api(`/api/captures?day=${selected}`);
      if (user !== state.user || selected !== state.day) return;
      const changed = data.jobs.filter(
        (j) => j.status === "done" && !appliedCaptures.has(j.id),
      );
      const before = JSON.stringify(state.jobs);
      state.jobs = data.jobs;
      renderCaptures();
      if (before !== JSON.stringify(state.jobs)) renderMeals();
      if (changed.length && (await loadDashboard())) {
        for (const job of changed) appliedCaptures.add(job.id);
        $("global-error").hidden = true;
        renderCaptures();
        renderMeals();
        if (state.view === "trends") await loadTrends();
        toast("Repas enregistré. Le bilan est à jour.");
      }
    });
  captureReads.set(key, task);
  const cleanup = () => {
    if (captureReads.get(key) === task) captureReads.delete(key);
  };
  task.then(cleanup, cleanup);
  return task;
}
function renderCaptures() {
  const markup = state.jobs
    .filter((j) => captureLoading(j) || j.status === "failed")
    .map(
      (j) =>
        `<article class="capture-job ${j.status === "failed" ? "failed" : ""}"><div class="capture-job-head" role="status"><strong>${j.status === "failed" ? "Ce repas attend un nouvel essai" : j.status === "done" ? "Mise à jour du bilan…" : j.status === "queued" ? "Repas reçu · en attente" : "Luna analyse ton repas…"}</strong>${j.status !== "failed" ? '<span class="loading-spinner" aria-hidden="true"></span>' : ""}</div><p>${escapeHTML(j.status === "failed" ? j.error : j.text)}</p><div class="capture-job-actions">${j.status === "failed" ? `<button class="text-button" data-retry-capture="${j.id}">Réessayer ↗</button>` : ""}${j.status !== "done" ? `<button class="text-button" data-cancel-capture="${j.id}">${j.status === "failed" ? "Retirer cet envoi" : "Annuler"}</button>` : ""}</div></article>`,
    )
    .join("");
  $("today-captures").innerHTML = markup;
  $("journal-captures").innerHTML = markup;
}
function renderFavorites() {
  $("favorite-list").innerHTML =
    state.favorites
      .map(
        (f) =>
          `<article class="panel favorite-card"><p class="eyebrow">MON REPAS HABITUEL</p><h2>${escapeHTML(f.title)}</h2><p>${f.items.map((i) => escapeHTML(i.name) + " · " + fmt(i.grams, 1) + " g").join("<br>")}</p><div class="section-head"><button class="primary-button" data-use-favorite="${f.id}">Réutiliser ↗</button><button class="text-button" data-remove-favorite="${f.id}">Supprimer</button></div></article>`,
      )
      .join("") ||
    '<div class="empty-state"><strong>Un repas que tu aimes retrouver ?</strong><p>Enregistre-le comme habitude depuis le journal ou la fiche de repas.</p></div>';
}
function switchView(view) {
  if (!["today", "journal", "trends", "favorites", "profile"].includes(view))
    return;
  state.view = view;
  syncBalanceUnit();
  for (const section of document.querySelectorAll(".view"))
    section.hidden = section.id !== `view-${view}`;
  for (const button of document.querySelectorAll(".nav-button"))
    button.classList.toggle("active", button.dataset.view === view);
  $("view-title").innerHTML =
    {
      today: "Dashboard",
      journal: "Mes repas",
      trends: "Mes tendances",
      favorites: "Mes habitudes",
      profile: "Mon profil",
    }[view] + '<span class="accent">.</span>';
  $("view-eyebrow").textContent = {
    today: "MON ÉQUILIBRE",
    journal: "MON JOURNAL",
    trends: "DANS LA DURÉE",
    favorites: "MES ESSENTIELS",
    profile: "MES REPÈRES",
  }[view];
  location.hash = view;
  if (state.user && ["today", "journal"].includes(view))
    loadCaptures().catch(globalError);
  if (view === "trends") loadTrends().catch(globalError);
  if (view === "profile") loadIntegrations().catch(globalError);
}
async function changeDay(day) {
  if (day > today()) return;
  state.day = day;
  $("selected-day").value = day;
  $("next-day").disabled = day >= today();
  await loadDashboard().catch(globalError);
  await loadCaptures().catch(globalError);
  if (state.view === "trends") loadTrends().catch(globalError);
}
function shiftDay(amount) {
  const day = new Date(state.day + "T12:00:00");
  day.setDate(day.getDate() + amount);
  changeDay(
    `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`,
  );
}
function editableItem(item) {
  return {
    label: item.label || item.name || "",
    food_id: item.food_id || null,
    grams: item.grams ?? "",
    estimated: !!item.estimated,
    composition_estimated: !!item.composition_estimated,
    note: item.note || "",
    food: item.name
      ? {
          id: item.food_id,
          name: item.name,
          source: item.source,
          nutrients: Object.fromEntries(
            Object.entries(item.nutrients).map(([k, v]) => [
              k,
              v == null ? null : (v * 100) / item.grams,
            ]),
          ),
          flags: item.flags || {},
        }
      : (item.matches || []).find((f) => f.id === item.food_id) || null,
    matches: item.matches || [],
  };
}
function openMeal(meal = null, duplicate = false, slot = null) {
  abortDictation();
  if (SpeechRecognition)
    $("dictation-status").textContent =
      "La dictée utilise le service vocal du navigateur. Seul le texte est envoyé à Luna.";
  state.editId = meal && !duplicate ? meal.id : null;
  state.requestId = crypto.randomUUID();
  state.draft = (meal?.items || []).map(editableItem);
  state.slotHint = slot || meal?.slot || null;
  $("meal-form").reset();
  $("meal-text").value = meal?.text || "";
  $("meal-day").value = duplicate ? state.day : meal?.day || state.day;
  const hour = new Date().getHours();
  $("meal-slot").value =
    meal?.slot ||
    slot ||
    (hour < 11 ? "breakfast" : hour < 17 ? "lunch" : "dinner");
  $("meal-title").value = meal?.title || "";
  $("meal-dialog-title").textContent = state.editId
    ? "Ajuste ton repas"
    : "Raconte ton repas";
  $("meal-error").textContent = "";
  $("meal-questions").hidden = true;
  $("review-section").hidden = !state.draft.length;
  $("meal-slot-field").hidden = !state.draft.length;
  $("parse-meal").hidden = !!state.draft.length;
  $("manual-meal").hidden = !!state.draft.length;
  $("parse-meal").disabled = !state.integrations?.chatgpt.connected;
  renderDraft();
  $("meal-dialog").showModal();
}
function addFood(food = null) {
  $("meal-slot-field").hidden = false;
  $("parse-meal").hidden = true;
  $("manual-meal").hidden = true;
  state.draft.push({
    label: food?.name || "",
    food_id: food?.id || null,
    grams: "",
    estimated: false,
    note: "",
    food,
    matches: [],
  });
  $("review-section").hidden = false;
  $("review-confirm").checked = false;
  renderDraft();
  const input = $("meal-items").querySelector(
    ".review-row:last-child .food-query",
  );
  if (input) input.focus();
}
function renderDraft() {
  $("meal-items").innerHTML = state.draft
    .map(
      (item, index) =>
        `<div class="review-row" data-index="${index}"><div class="review-grid"><label>Aliment à vérifier<input class="food-query" value="${escapeHTML(item.label || item.food?.name || "")}" placeholder="Chercher dans Ciqual…" autocomplete="off"></label><label>Poids (g)<input class="food-grams" type="number" min="0.1" max="10000" step="0.1" value="${escapeHTML(item.grams)}" required></label><button type="button" class="remove-food" aria-label="Retirer l’aliment">×</button></div><div class="food-results">${item.matches.length && !item.food ? matchButtons(item.matches) : ""}</div><p class="review-source">${item.food ? escapeHTML(item.food.source) + " · correspondance sélectionnée" : "Choisis une correspondance alimentaire."}</p>${item.note ? `<p class="review-note">${escapeHTML(item.note)}</p>` : ""}<div class="item-nutrients"></div></div>`,
    )
    .join("");
  updatePreview();
}
function matchButtons(matches) {
  return (
    matches
      .map(
        (food, index) =>
          `<button type="button" class="food-result" data-match="${index}">${escapeHTML(food.name)} <small>· ${fmt(food.nutrients.kcal)} kcal / 100 g</small></button>`,
      )
      .join("") ||
    '<p class="muted footnote">Aucune correspondance. Essaie un nom plus court ou un code-barres.</p>'
  );
}
function updatePreview() {
  state.draft.forEach((item, index) => {
    const element = $("meal-items").querySelector(
      `[data-index="${index}"] .item-nutrients`,
    );
    if (element)
      element.innerHTML = nutrientLine(
        Object.fromEntries(
          ["kcal", "protein", "carbs", "fat"].map((key) => [
            key,
            !item.food ||
            !Number(item.grams) ||
            item.food.nutrients[key] == null
              ? null
              : (item.food.nutrients[key] * Number(item.grams)) / 100,
          ]),
        ),
      );
  });
  const values = { kcal: 0, protein: 0, carbs: 0, fat: 0 };
  for (const item of state.draft)
    for (const key of Object.keys(values)) {
      if (!item.food || !Number(item.grams) || item.food.nutrients[key] == null)
        values[key] = null;
      else if (values[key] != null)
        values[key] += (item.food.nutrients[key] * Number(item.grams)) / 100;
    }
  $("meal-preview").innerHTML = Object.entries(values)
    .map(
      ([key, v]) =>
        `<div><strong>${fmt(v, 1)} ${key === "kcal" ? "kcal" : "g"}</strong><small>${key === "kcal" ? "Énergie" : macroNames[key]}</small></div>`,
    )
    .join("");
}
function draftPayload() {
  if (!state.draft.length) throw new Error("Ajoute au moins un aliment.");
  if (
    state.draft.some(
      (i) =>
        !i.food_id || !Number.isFinite(Number(i.grams)) || Number(i.grams) <= 0,
    )
  )
    throw new Error(
      "Choisis chaque aliment et renseigne son poids en grammes.",
    );
  return {
    title: $("meal-title").value || slots[$("meal-slot").value],
    day: $("meal-day").value,
    slot: $("meal-slot").value,
    text: $("meal-text").value,
    request_id: state.requestId,
    items: state.draft.map((i) => ({
      food_id: i.food_id,
      label: i.label,
      grams: Number(i.grams),
      estimated: i.estimated,
      composition_estimated: i.composition_estimated,
      note: i.note,
    })),
  };
}
async function parseMeal() {
  await stopDictation();
  $("meal-error").textContent = "";
  const text = $("meal-text").value.trim();
  if (text.length < 3)
    throw new Error("Dicte ou écris ton repas dans le champ texte.");
  const day = $("meal-day").value;
  const data = await api("/api/captures", {
    method: "POST",
    body: {
      text,
      day,
      slot_hint: state.slotHint,
      request_id: state.requestId,
    },
  });
  $("meal-dialog").close();
  state.jobs.push({ id: data.id, status: "queued", text, day });
  if (day !== state.day) await changeDay(day);
  else renderCaptures();
  await loadCaptures().catch(globalError);
  toast("Repas envoyé. Tu peux continuer pendant que Luna l’analyse.");
}
const SpeechRecognition =
  window.SpeechRecognition || window.webkitSpeechRecognition;
function resetDictationUI() {
  $("meal-text").readOnly = false;
  $("dictate-meal").textContent = "🎙 Dicter mon repas";
  $("dictate-meal").setAttribute("aria-pressed", "false");
}
function abortDictation() {
  const current = recognition;
  recognition = null;
  if (current) current.abort();
  resetDictationUI();
}
async function stopDictation() {
  if (!recognition) return;
  recognition.stopRequested = true;
  try {
    recognition.stop();
  } catch {
    abortDictation();
    return;
  }
  await Promise.race([
    dictationStopped,
    new Promise((resolve) => setTimeout(resolve, 2000)),
  ]);
  if (recognition) abortDictation();
}
$("dictate-meal").addEventListener("click", async () => {
  if (recognition) {
    await stopDictation();
    return;
  }
  if (!SpeechRecognition) return;
  const active = new SpeechRecognition();
  recognition = active;
  active.lang = "fr-FR";
  // Android continuous recognition promotes growing partials to final results.
  // Recognize one utterance at a time and restart after natural pauses instead.
  const android = /Android/i.test(navigator.userAgent);
  active.continuous = !android;
  active.interimResults = true;
  active.maxAlternatives = 1;
  const prefix = $("meal-text").value.trim();
  const completed = [];
  let utterance = "";
  let hadError = false;
  let finish;
  const limit = setTimeout(() => {
    if (recognition === active) stopDictation();
  }, 90000);
  dictationStopped = new Promise((resolve) => {
    finish = resolve;
  });
  active.onstart = () => {
    if (recognition !== active) return;
    $("meal-text").readOnly = true;
    $("dictate-meal").textContent = "■ Arrêter la dictée";
    $("dictate-meal").setAttribute("aria-pressed", "true");
    $("dictation-status").textContent =
      "Je t’écoute. Dis ton repas et son moment, puis envoie à Luna.";
  };
  active.onresult = (event) => {
    if (recognition !== active) return;
    const results = Array.from(event.results, (result) => result[0].transcript);
    // A single Android utterance is replaced by its newest hypothesis, never
    // concatenated with earlier versions. Keep genuine repeated spoken words.
    utterance = android ? results.at(-1) || "" : results.join(" ");
    $("meal-text").value = [prefix, ...completed, utterance]
      .filter(Boolean)
      .join(" ")
      .slice(0, 4000);
  };
  active.onerror = (event) => {
    if (recognition !== active) return;
    hadError = true;
    active.stopRequested = true;
    $("dictation-status").textContent =
      {
        "not-allowed":
          "Autorise le microphone pour MyMiam dans ton navigateur, puis réessaie.",
        "audio-capture":
          "Aucun microphone disponible. Vérifie le micro choisi dans ton navigateur.",
        network:
          "Le service de dictée du navigateur est indisponible. Ton texte est conservé.",
        "no-speech": "Aucune parole détectée. Réessaie ou écris ton repas.",
      }[event.error] ||
      "La dictée s’est arrêtée. Ton texte est conservé ; tu peux réessayer.";
  };
  active.onend = () => {
    if (
      android &&
      recognition === active &&
      !active.stopRequested &&
      !hadError
    ) {
      if (utterance.trim()) completed.push(utterance.trim());
      utterance = "";
      try {
        active.start();
        return;
      } catch {
        hadError = true;
        $("dictation-status").textContent =
          "La dictée s’est arrêtée. Ton texte est conservé.";
      }
    }
    clearTimeout(limit);
    if (recognition === active) {
      recognition = null;
      resetDictationUI();
      if (!hadError)
        $("dictation-status").textContent =
          "Dictée terminée. Tu peux ajuster le texte ou l’envoyer à Luna.";
    }
    finish();
  };
  try {
    active.start();
  } catch {
    clearTimeout(limit);
    recognition = null;
    finish();
    resetDictationUI();
    $("dictation-status").textContent =
      "La dictée n’a pas pu démarrer. Réessaie ou écris ton repas.";
  }
});
$("meal-dialog").addEventListener("close", abortDictation);
if (!SpeechRecognition) {
  $("dictate-meal").disabled = true;
  $("dictation-status").textContent =
    "Ce navigateur ne propose pas la dictée intégrée. Utilise Chrome, le micro du clavier du téléphone ou le texte.";
}
function refreshVisibleDashboard() {
  if (state.user && !document.hidden) {
    // Refresh captures in the journal too when returning from the phone’s background.
    (async () => {
      await loadCaptures();
      if (["today", "journal"].includes(state.view)) await loadDashboard();
    })().catch(globalError);
  }
}
document.addEventListener("visibilitychange", refreshVisibleDashboard);
async function loadDashboardHistory() {
  const selected = state.day,
    period = $("dashboard-period").value;
  const data = await api(`/api/trends?day=${selected}&days=${period}`);
  if (
    !state.user ||
    selected !== state.day ||
    period !== $("dashboard-period").value
  )
    return;
  state.dashboardHistory = { selected, period, data };
  renderDashboardHistory();
}
function renderDashboardHistory() {
  const saved = state.dashboardHistory;
  if (
    !saved ||
    saved.selected !== state.day ||
    saved.period !== $("dashboard-period").value
  )
    return;
  const data = saved.data;
  const mean = data.covered_days
    ? data.cumulative_deficit / data.covered_days
    : null;
  $("dashboard-kpis").innerHTML = [
    [
      "Déficit cumulé",
      data.covered_days ? balanceValue(data.cumulative_deficit) : "—",
    ],
    ["Déficit moyen", balanceValue(mean, true)],
    ["Jours exploitables", `${data.covered_days} / ${data.elapsed_days}`],
  ]
    .map(
      ([label, value]) =>
        `<div><span>${label}</span><strong>${value}</strong></div>`,
    )
    .join("");
  $("daily-deficit-chart").innerHTML = deficitChart(data.days, false);
  $("cumulative-deficit-chart").innerHTML = deficitChart(data.days, true);
}
function deficitChart(days, cumulative) {
  let running = 0;
  const values = days.map((d) => {
    if (d.day >= today() || d.deficit == null) return null;
    running += d.deficit;
    return cumulative ? running : d.deficit;
  });
  const known = values.filter((v) => v != null);
  if (!known.length)
    return '<div class="empty-state chart-empty"><p>Ton premier bilan apparaîtra après une journée passée avec des repas saisis.</p></div>';
  const width = 500,
    height = 220,
    left = 48,
    right = 12,
    top = 15,
    bottom = 34;
  const plotWidth = width - left - right,
    plotHeight = height - top - bottom;
  let high = Math.ceil(Math.max(0, ...known) / 100) * 100;
  let low = Math.floor(Math.min(0, ...known) / 100) * 100;
  if (high === low) {
    high = 100;
    low = -100;
  }
  const y = (v) => top + ((high - v) / (high - low)) * plotHeight;
  const step = plotWidth / days.length,
    x = (i) => left + (i + 0.5) * step;
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${cumulative ? "Déficit cumulé" : "Déficit et surplus par jour"} · ${balanceUnitLabel()}">`;
  for (let i = 0; i < 3; i++) {
    const value = high - ((high - low) * i) / 2;
    svg += `<line class="grid-line" x1="${left}" x2="${width - right}" y1="${y(value)}" y2="${y(value)}"/><text x="0" y="${y(value) + 4}">${balanceNumber(value)}</text>`;
  }
  svg += `<line class="zero-line" x1="${left}" x2="${width - right}" y1="${y(0)}" y2="${y(0)}"/>`;
  let segment = [];
  function flush() {
    if (segment.length > 1)
      svg += `<polyline class="deficit-line" points="${segment.join(" ")}"/>`;
    segment = [];
  }
  values.forEach((value, i) => {
    if (value == null) {
      flush();
      return;
    }
    const title = `${days[i].day} · ${cumulative ? "Cumul" : value >= 0 ? "Déficit" : "Surplus"} : ${balanceValue(cumulative ? value : Math.abs(value))}`;
    if (cumulative) {
      segment.push(`${x(i)},${y(value)}`);
      svg += `<circle class="deficit-point ${value < 0 ? "surplus" : ""}" data-day="${days[i].day}" data-value="${value}" cx="${x(i)}" cy="${y(value)}" r="3.5"><title>${escapeHTML(title)}</title></circle>`;
    } else {
      const barWidth = Math.max(1, Math.min(18, step * 0.62));
      const barHeight = Math.abs(y(value) - y(0));
      svg += `<rect class="deficit-bar ${value < 0 ? "surplus" : ""}" data-day="${days[i].day}" data-value="${value}" x="${x(i) - barWidth / 2}" y="${value === 0 ? y(0) - 1 : Math.min(y(value), y(0))}" width="${barWidth}" height="${Math.max(2, barHeight)}" rx="2"><title>${escapeHTML(title)}</title></rect>`;
    }
  });
  flush();
  days.forEach((d, i) => {
    if (i % Math.max(1, Math.ceil(days.length / 5)) === 0)
      svg += `<text text-anchor="middle" x="${x(i)}" y="${height - 8}">${d.day.slice(8)}/${d.day.slice(5, 7)}</text>`;
  });
  return svg + "</svg>";
}
async function loadTrends() {
  const selected = state.day,
    period = $("trend-period").value;
  const data = await api(`/api/trends?day=${selected}&days=${period}`);
  if (
    selected !== state.day ||
    !state.user ||
    period !== $("trend-period").value
  )
    return;
  state.trends = { selected, period, data };
  renderTrends();
}
function renderTrends() {
  const saved = state.trends;
  if (
    !saved ||
    saved.selected !== state.day ||
    saved.period !== $("trend-period").value
  )
    return;
  const data = saved.data;
  const complete = data.days.filter(
    (d) => d.day < today() && d.deficit != null,
  );
  const intake = complete.length
    ? complete.reduce((sum, d) => sum + d.intake.kcal, 0) / complete.length
    : null;
  $("trend-kpis").innerHTML = [
    [
      "Déficit cumulé",
      data.covered_days
        ? `${state.balanceUnit === "fat" ? "≈ " : ""}${balanceNumber(data.cumulative_deficit)}`
        : "—",
      balanceUnitLabel(),
      data.covered_days
        ? "Les surplus réduisent ce cumul."
        : "Aucune journée passée renseignée.",
    ],
    [
      "Couverture du suivi",
      `${data.covered_days} / ${data.elapsed_days}`,
      "jours",
      "Journées renseignées avec bilan calculable.",
    ],
    [
      "Apports moyens",
      fmt(intake),
      "kcal / jour",
      "Calculés sur les journées renseignées.",
    ],
  ]
    .map(
      ([title, value, unit, note]) =>
        `<article class="macro-card"><div class="macro-top">${title}</div><div class="macro-number">${value} <small>${unit}</small></div><p class="footnote">${note}</p></article>`,
    )
    .join("");
  renderEnergyChart(data.days);
  renderWeightChart(data.weights);
  $("trend-table").innerHTML = [...data.days]
    .reverse()
    .map(
      (d) =>
        `<tr><td>${escapeHTML(new Date(d.day + "T12:00:00").toLocaleDateString("fr-FR", { day: "2-digit", month: "short" }))}</td><td>${d.has_meals || d.complete ? fmt(d.intake.kcal) : "—"} kcal</td><td>${fmt(d.expenditure)} kcal</td><td>${d.day < today() ? balanceValue(d.deficit) : "Provisoire"}</td><td><span class="pill ${d.complete ? "" : "incomplete"}">${d.projected ? "Provisoire" : d.complete ? "Actualisé" : "Non saisie"}</span></td></tr>`,
    )
    .join("");
}
function renderEnergyChart(days) {
  if (!days.some((d) => d.expenditure != null || d.has_meals)) {
    $("trend-chart").innerHTML =
      '<div class="empty-state"><p>Complète ton profil et ajoute des repas pour afficher les tendances.</p></div>';
    return;
  }
  const width = 800,
    height = 205,
    left = 48,
    top = 10,
    bottom = 30,
    plot = height - top - bottom;
  const max =
    Math.max(
      1000,
      ...days.map((d) =>
        Math.max(d.has_meals ? d.intake.kcal || 0 : 0, d.expenditure || 0),
      ),
    ) * 1.12;
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Apports et dépenses sur la période">`;
  for (let i = 0; i < 4; i++) {
    const y = top + (plot * i) / 3;
    svg += `<line class="grid-line" x1="${left}" y1="${y}" x2="${width}" y2="${y}"/><text x="0" y="${y + 4}">${fmt(max * (1 - i / 3))}</text>`;
  }
  const step = (width - left) / days.length,
    bar = Math.min(13, step * 0.32);
  days.forEach((d, i) => {
    const x = left + i * step + step * 0.15;
    for (const [value, kind, shift] of [
      [d.has_meals || d.complete ? d.intake.kcal : null, "intake", 0],
      [d.expenditure, "expense", bar + 2],
    ]) {
      if (value == null) continue;
      const h = (value / max) * plot;
      svg += `<rect class="${kind}-bar" x="${x + shift}" y="${top + plot - h}" width="${bar}" height="${h}" rx="2" ${d.complete ? "" : 'opacity="0.5"'}><title>${d.day} · ${kind === "intake" ? "Apports" : "Dépense"} : ${fmt(value)} kcal${d.complete ? "" : " · sans repas saisi"}</title></rect>`;
    }
    if (i % Math.max(1, Math.floor(days.length / 7)) === 0)
      svg += `<text x="${x}" y="${height - 6}">${d.day.slice(8)}/${d.day.slice(5, 7)}</text>`;
  });
  $("trend-chart").innerHTML = svg + "</svg>";
}
function renderWeightChart(weights) {
  if (!weights.length) {
    $("weight-chart").innerHTML =
      '<div class="empty-state"><p>Ajoute une mesure pour commencer à suivre ta tendance.</p></div>';
    return;
  }
  const low = Math.min(...weights.map((w) => w.weight)) - 1,
    high = Math.max(...weights.map((w) => w.weight)) + 1;
  const points = weights.map((w, i) => ({
    x: 55 + (i * 700) / Math.max(1, weights.length - 1),
    y: 160 - ((w.weight - low) / (high - low)) * 130,
    w,
  }));
  let svg =
    '<svg viewBox="0 0 800 200" role="img" aria-label="Évolution du poids">';
  for (let i = 0; i < 3; i++) {
    const y = 30 + i * 65;
    svg += `<line class="grid-line" x1="50" y1="${y}" x2="790" y2="${y}"/><text x="0" y="${y + 4}">${fmt(high - ((high - low) * i) / 2, 1)} kg</text>`;
  }
  svg += `<polyline class="weight-line" points="${points.map((p) => p.x + "," + p.y).join(" ")}"/>`;
  for (const p of points)
    svg += `<circle class="weight-point" cx="${p.x}" cy="${p.y}" r="4"><title>${p.w.day} · ${fmt(p.w.weight, 1)} kg</title></circle>`;
  svg += `<text x="50" y="192">${weights[0].day}</text><text x="680" y="192">${weights[weights.length - 1].day}</text></svg>`;
  $("weight-chart").innerHTML = svg;
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.view) switchView(button.dataset.view);
    if (button.dataset.action === "new-meal")
      openMeal(null, false, button.dataset.slot);
    if (button.dataset.refineMeal) {
      await busy(button, () =>
        api(`/api/meals/${button.dataset.refineMeal}/refine`, {
          method: "POST",
          body: {
            group: Number(button.dataset.refineGroup),
            option: Number(button.dataset.refineOption),
          },
        }),
      );
      await loadDashboard();
      if (state.view === "trends") await loadTrends();
      toast("Estimation précisée. Le bilan est à jour.");
    }
    if (button.dataset.reanalyseMeal) {
      const day = state.day,
        user = state.user;
      const result = await busy(button, () =>
        api(`/api/meals/${button.dataset.reanalyseMeal}/reanalyse`, {
          method: "POST",
          body: {},
        }),
      );
      if (!result || user !== state.user || day !== state.day) return;
      state.jobs = state.jobs.filter((j) => j.id !== result.id);
      state.jobs.push({
        ...result,
        day,
        status: "queued",
        text:
          state.summary.meals.find((m) => m.id === button.dataset.reanalyseMeal)
            ?.text || "",
        reanalysis_meal_ids: result.reanalysis_meal_ids || [
          button.dataset.reanalyseMeal,
        ],
      });
      renderCaptures();
      renderMeals();
      await loadCaptures();
    }
    if (button.dataset.retryCapture) {
      const user = state.user,
        day = state.day;
      await busy(button, () =>
        api(`/api/captures/${button.dataset.retryCapture}/retry`, {
          method: "POST",
          body: {},
        }),
      );
      if (user !== state.user || day !== state.day) return;
      const job = state.jobs.find((j) => j.id === button.dataset.retryCapture);
      if (job) {
        job.status = "queued";
        job.error = null;
      }
      renderCaptures();
      renderMeals();
      await loadCaptures();
    }
    if (button.dataset.cancelCapture) {
      await busy(button, () =>
        api(`/api/captures/${button.dataset.cancelCapture}`, {
          method: "DELETE",
          body: {},
        }),
      );
      await loadCaptures();
    }
    if (button.dataset.edit)
      openMeal(state.summary.meals.find((m) => m.id === button.dataset.edit));
    if (button.dataset.duplicate)
      openMeal(
        state.summary.meals.find((m) => m.id === button.dataset.duplicate),
        true,
      );
    if (button.dataset.favorite) {
      const m = state.summary.meals.find(
        (m) => m.id === button.dataset.favorite,
      );
      await api("/api/favorites", {
        method: "POST",
        body: { title: m.title, items: m.items },
      });
      await loadFavorites();
      toast("Repas ajouté à tes habitudes.");
    }
    if (button.dataset.delete && confirm("Supprimer ce repas du journal ?")) {
      await api("/api/meals/" + button.dataset.delete, {
        method: "DELETE",
        body: {},
      });
      await loadDashboard();
      toast("Repas supprimé.");
    }
    if (button.dataset.useFavorite) {
      const f = state.favorites.find(
        (f) => f.id === button.dataset.useFavorite,
      );
      openMeal({ ...f, slot: "lunch", day: state.day }, true);
    }
    if (
      button.dataset.removeFavorite &&
      confirm("Supprimer ce repas habituel ?")
    ) {
      await api("/api/favorites/" + button.dataset.removeFavorite, {
        method: "DELETE",
        body: {},
      });
      await loadFavorites();
    }
  } catch (error) {
    globalError(error);
  }
});
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("login-error").textContent = "";
  const form = event.currentTarget,
    button = form.querySelector("button");
  try {
    await busy(button, async () => {
      const data = await api("/api/auth/login", {
        method: "POST",
        body: Object.fromEntries(new FormData(form)),
      });
      form.elements.password.value = "";
      await showApp(data.user);
    });
  } catch (error) {
    $("login-error").textContent = error.message;
  }
});
async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: {} });
    showLogin();
  } catch (error) {
    globalError(error);
  }
}
$("logout").addEventListener("click", logout);
$("logout-profile").addEventListener("click", logout);
$("selected-day").addEventListener("change", (event) =>
  changeDay(event.target.value),
);
$("previous-day").addEventListener("click", () => shiftDay(-1));
$("next-day").addEventListener("click", () => shiftDay(1));
$("close-meal").addEventListener("click", () => $("meal-dialog").close());
$("manual-meal").addEventListener("click", () => addFood());
$("add-food").addEventListener("click", () => addFood());
$("parse-meal").addEventListener("click", async () => {
  try {
    await busy($("parse-meal"), parseMeal);
  } catch (error) {
    $("meal-error").textContent = error.message;
  }
});
$("meal-items").addEventListener("input", (event) => {
  const row = event.target.closest(".review-row");
  if (!row) return;
  const item = state.draft[Number(row.dataset.index)];
  $("review-confirm").checked = false;
  if (event.target.matches(".food-grams")) {
    item.grams = event.target.value;
    item.estimated = false;
    item.note = "Quantité modifiée manuellement.";
    const note = row.querySelector(".review-note");
    if (note) note.textContent = item.note;
    updatePreview();
  }
  if (event.target.matches(".food-query")) {
    item.label = event.target.value;
    item.food = null;
    item.food_id = null;
    row.querySelector(".review-source").textContent = "Sélectionne un aliment.";
    updatePreview();
    clearTimeout(item.searchTimer);
    const query = item.label;
    item.searchTimer = setTimeout(async () => {
      try {
        const data = await api("/api/foods?q=" + encodeURIComponent(query));
        if (item.label !== query || !row.isConnected) return;
        item.matches = data.foods;
        row.querySelector(".food-results").innerHTML = matchButtons(
          item.matches,
        );
      } catch (error) {
        $("meal-error").textContent = error.message;
      }
    }, 250);
  }
});
$("meal-items").addEventListener("click", (event) => {
  const row = event.target.closest(".review-row");
  if (!row) return;
  const index = Number(row.dataset.index),
    item = state.draft[index];
  const match = event.target.closest("[data-match]");
  if (match) {
    const food = item.matches[Number(match.dataset.match)];
    item.food = food;
    item.food_id = food.id;
    item.label = food.name;
    item.matches = [];
    renderDraft();
    $("review-confirm").checked = false;
  }
  if (event.target.closest(".remove-food")) {
    state.draft.splice(index, 1);
    renderDraft();
    $("review-confirm").checked = false;
  }
});
$("meal-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("meal-error").textContent = "";
  try {
    if (!$("review-confirm").checked)
      throw new Error("Confirme la vérification des aliments et des portions.");
    const payload = draftPayload();
    await busy($("save-meal"), async () => {
      await api(state.editId ? "/api/meals/" + state.editId : "/api/meals", {
        method: state.editId ? "PUT" : "POST",
        body: payload,
      });
      $("meal-dialog").close();
      await loadDashboard();
      toast("Repas enregistré.");
    });
  } catch (error) {
    $("meal-error").textContent = error.message;
  }
});
$("favorite-draft").addEventListener("click", async () => {
  try {
    const payload = draftPayload();
    await busy($("favorite-draft"), () =>
      api("/api/favorites", { method: "POST", body: payload }),
    );
    await loadFavorites();
    toast("Repas ajouté à tes habitudes.");
  } catch (error) {
    $("meal-error").textContent = error.message;
  }
});
$("barcode-food").addEventListener("click", async () => {
  const barcode = prompt("Code-barres du produit (8 à 14 chiffres)");
  if (!barcode) return;
  try {
    const data = await api(
      "/api/foods/barcode/" + encodeURIComponent(barcode.trim()),
    );
    addFood(data.food);
  } catch (error) {
    $("meal-error").textContent = error.message;
  }
});
$("profile-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await busy(event.currentTarget.querySelector('button[type="submit"]'), () =>
      api("/api/profile", {
        method: "PUT",
        body: Object.fromEntries(new FormData(event.currentTarget)),
      }),
    );
    await Promise.all([loadProfile(), loadDashboard()]);
    toast("Profil et objectifs enregistrés.");
  } catch (error) {
    globalError(error);
  }
});
$("weight-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/weights", {
      method: "POST",
      body: { day: state.day, weight: $("new-weight").value },
    });
    $("new-weight").value = "";
    await Promise.all([loadTrends(), loadDashboard()]);
    toast("Poids enregistré.");
  } catch (error) {
    globalError(error);
  }
});
$("balance-unit").addEventListener("change", (event) => {
  state.balanceUnit = event.target.value === "kcal" ? "kcal" : "fat";
  if (state.user) {
    try {
      localStorage.setItem(
        `mymiam:balance-unit:${state.user.id}`,
        state.balanceUnit,
      );
    } catch {
      /* Rendering still works without storage. */
    }
  }
  syncBalanceUnit();
  if (state.summary?.day === state.day) renderDashboard();
  renderDashboardHistory();
  renderTrends();
});
$("trend-period").addEventListener("change", () =>
  loadTrends().catch(globalError),
);
$("dashboard-period").addEventListener("change", () =>
  loadDashboardHistory().catch(globalError),
);
$("reset-macros").addEventListener("click", () => {
  for (const [key, value] of Object.entries({
    protein_pct: 20,
    carbs_pct: 45,
    fat_pct: 35,
  }))
    $("profile-form").elements.namedItem(key).value = value;
  toast(
    "Repères remis à 20 / 45 / 35. Enregistre ton profil pour les appliquer.",
  );
});
$("connect-chatgpt").addEventListener("click", async () => {
  try {
    const data = await api("/api/chatgpt/connect", {
      method: "POST",
      body: {},
    });
    toast(data.message);
  } catch (error) {
    globalError(error);
  }
});
$("refresh-models").addEventListener("click", async () => {
  try {
    await busy($("refresh-models"), () =>
      api("/api/chatgpt/models", { method: "POST", body: {} }),
    );
    await loadIntegrations();
    toast("Luna est disponible sur cette connexion.");
  } catch (error) {
    globalError(error);
  }
});
$("garmin-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const form = event.currentTarget;
    await busy(form.querySelector("button"), () =>
      api("/api/garmin/connect", {
        method: "POST",
        body: Object.fromEntries(new FormData(form)),
      }),
    );
    form.elements.password.value = "";
    await loadIntegrations();
    toast("Connexion Garmin en cours.");
  } catch (error) {
    globalError(error);
  }
});
$("garmin-mfa").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/garmin/mfa", {
      method: "POST",
      body: Object.fromEntries(new FormData(event.currentTarget)),
    });
    event.currentTarget.elements.code.value = "";
    await loadIntegrations();
  } catch (error) {
    globalError(error);
  }
});
$("sync-garmin").addEventListener("click", async () => {
  try {
    const data = await busy($("sync-garmin"), () =>
      api("/api/garmin/sync", { method: "POST", body: { day: state.day } }),
    );
    await loadDashboard();
    toast(`${data.updated} journées Garmin synchronisées.`);
  } catch (error) {
    globalError(error);
  }
});
window.addEventListener("hashchange", () => {
  if (state.user && location.hash.slice(1) !== state.view)
    switchView(location.hash.slice(1));
});
window.addEventListener("online", () => {
  if (state.user) loadAll().catch(globalError);
});
(async () => {
  try {
    const data = await api("/api/auth/me");
    if (data.user) {
      await showApp(data.user);
      switchView(location.hash.slice(1) || "today");
    } else showLogin();
  } catch (error) {
    $("login-error").textContent = error.message;
  }
  if ("serviceWorker" in navigator)
    navigator.serviceWorker.register("/sw.js").catch(() => {});
})();
