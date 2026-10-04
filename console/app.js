/* Local-only operations dashboard. Artifact text is always inserted as text. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = { snapshot: null, token: null, loading: false, queued: false, timer: null, logTimer: null, dateInitialized: false, docsRequest: 0, launching: new Set() };
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 2 });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
const etTime = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" });
const etClock = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" });
const isNumber = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
const fmt = (value) => isNumber(value) ? number.format(Number(value)) : "—";
const usd = (value) => isNumber(value) ? money.format(Number(value)) : "—";
const pct = (value) => isNumber(value) ? `${number.format(Number(value) * 100)}%` : "—";
const arr = (value) => Array.isArray(value) ? value : [];
const words = (value) => String(value ?? "Unknown").replaceAll("_", " ").replaceAll("-", " ");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function dateInET() {
  const parts = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date());
  const value = (type) => parts.find((part) => part.type === type).value;
  return `${value("year")}-${value("month")}-${value("day")}`;
}

function displayTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? String(value) : etTime.format(parsed);
}

function statusPill(value) {
  const normalized = String(value || "unknown").toLowerCase();
  let tone = "neutral";
  if (/failed|error|degraded|refused/.test(normalized)) tone = "bad";
  else if (/block|hold|held|warn|stale|skip|budget|interrupt/.test(normalized)) tone = "warning";
  else if (/running|queued|pending/.test(normalized)) tone = "running";
  else if (/complete|success|ready|healthy|ok|available|noop|executed/.test(normalized)) tone = "good";
  return el("span", `status-pill ${tone}`, words(value));
}

function emptyRow(target, columns, message) {
  const row = el("tr");
  const cell = el("td", "empty-cell", message);
  cell.colSpan = columns;
  row.append(cell);
  target.replaceChildren(row);
}

function renderWarnings(id, warnings) {
  $(id).replaceChildren(...arr(warnings).map((warning) => el("div", "notice", warning)));
}

function renderDaily(daily = {}) {
  const metrics = daily.metrics || {};
  $("metric-runs").textContent = fmt(metrics.run_count);
  $("metric-completed").textContent = fmt(metrics.completed_count);
  $("metric-blocked").textContent = fmt(metrics.blocked_count);
  $("metric-spend").textContent = usd(metrics.confirmed_spend_usd);
  $("spend-caption").textContent = state.snapshot.mode === "demo" ? "Demo has no confirmed live spend" : `${isNumber(metrics.confirmed_fill_count) ? `${fmt(metrics.confirmed_fill_count)} confirmed fills · ` : ""}artifact-backed spend`;
  $("brief-date").textContent = daily.date || $("report-date").value;
  const summary = arr(daily.summary).map((text, index) => {
    const item = el("div", "summary-item");
    item.append(el("span", "summary-number", String(index + 1).padStart(2, "0")), el("p", "", text));
    return item;
  });
  $("daily-summary").replaceChildren(...(summary.length ? summary : [el("p", "empty-state", "No daily summary is available for this date. Run a demo or select a day with saved artifacts.")]));
  renderWarnings("daily-warnings", daily.warnings);
  renderCosts(daily.costs);
  const recent = arr(daily.recent_days).slice(0, 7).map((date) => {
    const button = el("button", `recent-day${date === daily.date ? " selected" : ""}`, date);
    button.type = "button";
    button.setAttribute("aria-label", `Show runs for ${date}`);
    button.setAttribute("aria-pressed", String(date === daily.date));
    button.addEventListener("click", () => {
      $("report-date").value = date;
      state.dateInitialized = true;
      refresh();
    });
    return button;
  });
  $("recent-days").replaceChildren(...(recent.length ? [el("span", "", "With artifacts"), ...recent] : []));
  renderSchedule(daily.schedule);
  renderRuns(daily.runs);
}

function renderCosts(costs) {
  $("cost-total").textContent = costs ? `${fmt(costs.estimated_total_proxy_units)} proxy units` : "No activity counters";
  if (!costs) {
    $("cost-details").replaceChildren(el("p", "empty-state", "The selected day's artifacts contain no usable activity counters."));
    return;
  }
  const totals = el("dl", "cost-totals");
  for (const [key, value] of Object.entries(costs.totals || {})) {
    const pair = el("div");
    pair.append(el("dt", "", words(key)), el("dd", "", fmt(value)));
    totals.append(pair);
  }
  const components = el("div", "cost-components");
  for (const component of arr(costs.components)) {
    const row = el("div");
    row.append(el("span", "", words(component.component)), el("strong", "", `${fmt(component.estimated_proxy_units)} units`));
    components.append(row);
  }
  $("cost-details").replaceChildren(el("p", "panel-footnote", `${fmt(costs.run_count)} runs with counters · ${fmt(costs.manual_recovery_runs)} manual recovery runs`), totals, el("h3", "cost-subtitle", "Weighted component activity"), components);
}

function renderSchedule(schedule) {
  const nodes = arr(schedule).map((item) => {
    const row = el("div", "schedule-row");
    const detail = el("div", "schedule-detail");
    detail.append(el("div", "", item.name || item.id || "Scheduled window"), el("small", "", words(item.status || "configured")));
    const next = el("span", "countdown");
    if (item.next_at) next.dataset.nextAt = item.next_at;
    else next.textContent = "No next run recorded";
    row.append(el("div", "schedule-time", item.time || displayTime(item.next_at)), el("div", "schedule-line"), detail, next);
    return row;
  });
  $("schedule").replaceChildren(...(nodes.length ? nodes : [el("p", "empty-state", "No configured run windows are available.")]));
  updateClock();
}

function renderRuns(runs) {
  const rows = arr(runs);
  $("run-count").textContent = `${rows.length} ${rows.length === 1 ? "run" : "runs"}`;
  if (!rows.length) return emptyRow($("runs-body"), 6, "No run artifacts were found for this date. An empty ledger does not confirm the scheduler ran.");
  $("runs-body").replaceChildren(...rows.map((run) => {
    const row = el("tr");
    const name = el("td");
    name.append(el("strong", "", words(run.stream || "DCA")), el("small", "", [run.id, run.mode ? words(run.mode) : null].filter(Boolean).join(" · ")));
    const status = el("td");
    status.append(statusPill(run.status));
    row.append(name, el("td", "", displayTime(run.time)), status, el("td", "numeric", fmt(run.candidates)), el("td", "numeric", fmt(run.submitted)), el("td", "numeric", usd(run.confirmed_spend_usd)));
    return row;
  }));
}

function normalizeForecasts(rows) {
  return arr(rows).flatMap((row) => {
    if (row.component) return [{ ...row, point: row.point ?? row.prediction ?? row.predicted, forecast_date: row.forecast_date || row.target_date }];
    return ["open", "high", "low", "close"].flatMap((component) => {
      const nested = row[component] && typeof row[component] === "object" ? row[component] : {};
      const point = row[`predicted_${component}`] ?? row[`${component}_point`] ?? nested.point ?? row[`${component}_pred`] ?? row[component];
      if (!isNumber(point)) return [];
      return [{ ...row, component, point, p10: row[`${component}_p10`] ?? nested.p10 ?? (component === "close" ? row.p10 : null), p50: row[`${component}_p50`] ?? nested.p50 ?? (component === "close" ? row.p50 : null), p90: row[`${component}_p90`] ?? nested.p90 ?? (component === "close" ? row.p90 : null), forecast_date: row.forecast_date || row.target_date }];
    });
  });
}

function renderResearch(octopus = {}) {
  const statusEl = $("research-status");
  if (statusEl) {
    const pill = statusPill(octopus?.status || "not available");
    statusEl.className = pill.className;
    statusEl.textContent = pill.textContent;
  }
  const freshnessEl = $("research-freshness");
  if (freshnessEl) {
    freshnessEl.textContent = `Latest Octopus run${octopus?.as_of ? ` · as of ${octopus.as_of}` : " · source date unavailable"}. Independent of the daily DCA date selector.`;
  }
  const summaryEl = $("research-summary");
  if (summaryEl) {
    summaryEl.replaceChildren(...arr(octopus?.summary).map((text) => el("p", "", text)));
  }
  renderWarnings("research-warnings", octopus?.warnings);
  const forecasts = normalizeForecasts(octopus?.forecasts);
  const tickers = [...new Set(forecasts.map((row) => row.ticker).filter(Boolean))].sort();
  const datalist = $("ticker-options");
  if (datalist) {
    datalist.replaceChildren(...tickers.map((ticker) => {
      const option = el("option");
      option.value = ticker;
      return option;
    }));
  }
  const periodFilter = $("period-filter");
  if (periodFilter) {
    const selectedPeriod = periodFilter.value;
    const periodNames = new Map(arr(octopus?.period_options).map((period) => [period.id, period.name]));
    const periods = [...new Set(arr(octopus?.periods).map((row) => row.id || row.period || row.regime).filter(Boolean))].sort();
    const all = el("option", "", "All periods");
    all.value = "all";
    periodFilter.replaceChildren(all, ...periods.filter((period) => period !== "all").map((period) => {
      const option = el("option", "", periodNames.get(period) || words(period));
      option.value = period;
      return option;
    }));
    periodFilter.value = periods.includes(selectedPeriod) ? selectedPeriod : "all";
  }
  const footnoteEl = $("forecast-footnote");
  if (footnoteEl) {
    footnoteEl.textContent = `${octopus?.as_of ? `Research as of ${octopus.as_of}. ` : ""}Forecasts retain their source dates. Older artifacts are not current predictions.${octopus?.truncated ? ` The source contains ${fmt(octopus.forecast_count)} rows; this snapshot is capped.` : ""}`;
  }
  renderForecasts();
  renderPeriods();
}

function renderForecasts() {
  const componentFilter = $("component-filter");
  const component = componentFilter ? componentFilter.value : "close";
  const tickerFilter = $("ticker-filter");
  const ticker = tickerFilter ? tickerFilter.value.trim().toUpperCase() : "";
  const all = normalizeForecasts(state.snapshot?.octopus?.forecasts);
  const datalist = $("ticker-options");
  if (datalist && all.length > 0) {
    const uniqueTickers = Array.from(new Set(all.map((f) => f.ticker).filter(Boolean))).sort();
    if (datalist.children.length !== uniqueTickers.length) {
      datalist.replaceChildren(...uniqueTickers.map((sym) => {
        const opt = document.createElement("option");
        opt.value = sym;
        return opt;
      }));
    }
  }
  const rows = all.filter((row) => String(row.component).toLowerCase() === component && (!ticker || String(row.ticker || "").toUpperCase().includes(ticker)));
  const countEl = $("forecast-count");
  if (countEl) countEl.textContent = `${rows.length} ${component} ${rows.length === 1 ? "forecast" : "forecasts"}${rows.length > 100 ? " · showing first 100" : ""}`;
  const forecastsBody = $("forecasts-body");
  if (!rows.length) {
    if (forecastsBody) emptyRow(forecastsBody, 7, all.length ? "No forecasts match these filters." : "No forecasts are available. Run the Octopus demo or connect an existing research run.");
    const chartEl = $("forecast-chart");
    if (chartEl) chartEl.replaceChildren();
    return;
  }
  if (forecastsBody) {
    forecastsBody.replaceChildren(...rows.slice(0, 100).map((forecast) => {
      const row = el("tr");
      const name = el("td");
      name.append(el("strong", "", forecast.ticker || "-"));
      row.append(name, el("td", "", words(forecast.model)), el("td", "", `${forecast.origin_date || forecast.as_of || "-"} → ${forecast.forecast_date || forecast.target_label || "Date unresolved"}`), ...[forecast.point, forecast.p10, forecast.p50, forecast.p90].map((value) => el("td", "numeric", usd(value))));
      return row;
    }));
  }
  renderForecastChart(rows);
}

function renderForecastChart(rows) {
  const chartEl = $("forecast-chart");
  if (!chartEl) return;
  const eligible = rows.filter((row) => isNumber(row.point) && isNumber(row.p10) && isNumber(row.p90) && Number(row.p90) >= Number(row.p10));
  if (!eligible.length) { chartEl.replaceChildren(); return; }
  const ns = "http://www.w3.org/2000/svg";
  const svgNode = (name, attributes) => {
    const node = document.createElementNS(ns, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    return node;
  };
  const nodes = eligible.map((forecast) => {
    const row = el("div", "range-row");
    const label = el("div", "range-label");
    label.append(el("strong", "", forecast.ticker), el("span", "range-model", words(forecast.model)));
    const svg = svgNode("svg", { viewBox: "0 0 400 28", preserveAspectRatio: "none", class: "range-graphic", role: "img", "aria-label": `${forecast.ticker}: P10 ${usd(forecast.p10)}, point ${usd(forecast.point)}, P90 ${usd(forecast.p90)}. Each row uses its own price scale.` });
    const low = Math.min(Number(forecast.p10), Number(forecast.point));
    const high = Math.max(Number(forecast.p90), Number(forecast.point));
    const spread = high - low || Math.max(Math.abs(low) * .01, 1);
    const x = (value) => 25 + ((Number(value) - low) / spread) * 350;
    svg.append(svgNode("line", { x1: 0, x2: 400, y1: 14, y2: 14, stroke: "#edf2ed", "stroke-width": 1 }), svgNode("rect", { x: x(forecast.p10), y: 8, width: Math.max(2, x(forecast.p90) - x(forecast.p10)), height: 12, rx: 6, fill: "#d7eadc" }), svgNode("line", { x1: x(forecast.p10), x2: x(forecast.p10), y1: 7, y2: 21, stroke: "#91b69b", "stroke-width": 1 }), svgNode("line", { x1: x(forecast.p90), x2: x(forecast.p90), y1: 7, y2: 21, stroke: "#91b69b", "stroke-width": 1 }), svgNode("circle", { cx: x(forecast.point), cy: 14, r: 4, fill: "#277451" }));
    row.append(label, svg, el("div", "range-value", `${usd(forecast.p10)} - ${usd(forecast.p90)}`));
    return row;
  });
  const noteText = eligible.length > 1
    ? `All ${eligible.length} available ranges. Each row has its own price scale; use the dollar labels to compare uncertainty.`
    : `1 available range. Each row has its own price scale; use the dollar labels to compare uncertainty.`;
  nodes.push(el("p", "chart-note", noteText));
  chartEl.replaceChildren(...nodes);
}

function renderPeriods() {
  const periodFilter = $("period-filter");
  const chosen = periodFilter ? periodFilter.value : "all";
  const componentFilter = $("component-filter");
  const component = componentFilter ? componentFilter.value : "close";
  const rows = arr(state.snapshot?.octopus?.periods).filter((row) => (chosen === "all" || (row.id || row.period || row.regime) === chosen) && (!row.component || row.component.toLowerCase() === component));
  const periodsBody = $("periods-body");
  if (!periodsBody) return;
  if (!rows.length) return emptyRow(periodsBody, 7, "No measured evaluations match this period and price component. Empty periods are not estimated.");
  periodsBody.replaceChildren(...rows.map((period) => {
    const row = el("tr");
    const skill = period.skill_vs_persistence ?? period.skill;
    row.append(el("td", "", period.name || words(period.period || period.regime || period.id)), el("td", "", words(period.model)), el("td", "", words(period.component || component)), el("td", "numeric", fmt(period.n ?? period.samples ?? period.count)), el("td", "numeric", usd(period.mae_usd ?? period.mae_price ?? period.mae_dollar)), el("td", "numeric", pct(period.coverage ?? period.coverage_p10_p90)), el("td", `numeric${isNumber(skill) && Number(skill) > 0 ? " green" : ""}`, pct(skill)));
    return row;
  }));
}

function renderActions(actions) {
  const nodes = arr(actions).map((action) => {
    const card = el("article", "action-card");
    const heading = el("div", "action-header");
    heading.append(el("span", "action-icon", /octopus/.test(action.id) ? "⌁" : /report/.test(action.id) ? "≡" : "↗"), el("h3", "", action.label || words(action.id)));
    card.append(heading, el("p", "action-description", action.description || "Launch this configured program entry point."));
    if (action.reason) card.append(el("p", "action-reason", action.reason));
    const bottom = el("div", "action-bottom");
    const pending = state.launching.has(action.id) || arr(state.snapshot?.jobs).some((job) => job.action === action.id && ["running", "queued"].includes(job.status));
    const button = el("button", "button ghost", pending ? "Running…" : "Run workflow →");
    button.type = "button";
    button.disabled = !action.available || !state.token || pending;
    button.addEventListener("click", () => startJob(action.id));
    bottom.append(el("span", "tag", action.available ? "Available" : "Setup needed"), button);
    card.append(bottom);
    return card;
  });
  $("actions").replaceChildren(...(nodes.length ? nodes : [el("div", "panel empty-state", "No workflows were advertised by the server.")]));
  const demo = arr(actions).find((action) => action.id === "demo-all");
  const running = state.launching.has("demo-all") || arr(state.snapshot?.jobs).some((job) => job.action === "demo-all" && ["running", "queued"].includes(job.status));
  $("run-demo").disabled = !state.token || running || Boolean(demo && !demo.available);
  $("run-demo").textContent = running ? "Demo running…" : "▶ Run full demo";
}

function renderJobs(jobs) {
  const previousOpen = new Map([...$("jobs-list").querySelectorAll("details")].map((node) => [node.dataset.jobId, node.open]));
  const rows = arr(jobs);
  $("job-count").textContent = `${rows.length} ${rows.length === 1 ? "job" : "jobs"}`;
  $("jobs-list").replaceChildren(...(rows.length ? rows.map((job) => {
    const detail = el("details", "job");
    detail.dataset.jobId = String(job.id);
    detail.open = previousOpen.has(String(job.id)) ? previousOpen.get(String(job.id)) : /running|queued|failed|error/.test(job.status);
    const summary = el("summary");
    const head = el("span", "job-head");
    const title = el("span", "job-title");
    title.append(el("span", "", job.label || words(job.action)), statusPill(job.status));
    const timing = `${displayTime(job.started_at)}${job.finished_at ? ` → ${displayTime(job.finished_at)}` : ""}${job.returncode !== undefined && job.returncode !== null ? ` · exit ${job.returncode}` : ""}`;
    head.append(title, el("span", "job-time", timing));
    summary.append(head);
    const log = Array.isArray(job.log) ? job.log.join("\n") : job.log;
    const output = el("pre", "", log || "Waiting for process output…");
    output.dataset.offset = String(job.log_total ?? (log ? log.length : 0));
    if (["running", "queued"].includes(job.status)) {
      const cancel = el("button", "button secondary small job-cancel", "Cancel job");
      cancel.type = "button";
      cancel.addEventListener("click", () => cancelJob(job.id));
      head.append(cancel);
    }
    detail.append(summary, output);
    return detail;
  }) : [el("p", "empty-state", "No jobs started in this console session. Run the full demo to test the complete path.")]));
}

function renderModules(modules) {
  const nodes = arr(modules).map((module) => {
    const card = el("article", "module-row");
    const heading = el("div", "module-heading");
    heading.append(el("h3", "", module.name || words(module.id)), statusPill(module.status));
    card.append(heading, el("p", "muted", module.description || ""));
    if (module.command) {
      const command = Array.isArray(module.command) ? module.command.join(" ") : String(module.command);
      const line = el("div", "module-command");
      const button = el("button", "button secondary small", "Copy");
      button.type = "button";
      button.setAttribute("aria-label", `Copy command for ${module.name || module.id}`);
      button.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(command); button.textContent = "Copied"; }
        catch { button.textContent = "Select to copy"; }
      });
      line.append(el("code", "", command), button);
      card.append(line);
    }
    if (module.docs) {
      const docs = el("p", "module-docs", "Documentation: ");
      for (const [index, path] of (Array.isArray(module.docs) ? module.docs : [module.docs]).entries()) {
        if (index) docs.append(document.createTextNode(", "));
        const link = el("a", "", path);
        link.href = `/api/docs?${new URLSearchParams({ path })}`;
        link.target = "_blank";
        link.rel = "noopener";
        link.addEventListener("click", (event) => {
          event.preventDefault();
          openDocs(path);
        });
        docs.append(link);
      }
      card.append(docs);
    }
    return card;
  });
  $("modules").replaceChildren(...(nodes.length ? nodes : [el("p", "empty-state", "No additional program modules loaded.")]));
}

async function openDocs(path) {
  const request = ++state.docsRequest;
  $("docs-title").textContent = path;
  $("docs-body").textContent = "Loading documentation…";
  if (!$("docs-dialog").open) $("docs-dialog").showModal();
  try {
    const response = await fetch(`/api/docs?${new URLSearchParams({ path })}`, { cache: "no-store", signal: AbortSignal.timeout(15000) });
    if (!response.ok) throw new Error(`Documentation unavailable (${response.status}).`);
    const content = await response.text();
    if (request === state.docsRequest) $("docs-body").textContent = content;
  } catch (error) {
    if (request === state.docsRequest) $("docs-body").textContent = error.message;
  }
}

/* Hash router. Every top-level page is a <section class="page" data-page>; the
   hash names the page and nothing else, so an unknown or empty hash shows Today.
   Pages never receive a path or a query from the hash: each page renderer reads
   the snapshot the server already produced (GUI expansion Phase 0). */
const PAGE_TITLES = {
  today: "Today", runs: "Runs", schedule: "Schedule", state: "State & ledgers",
  alerts: "Alerts & health", config: "Config", tickers: "Tickers", reports: "Reports",
  research: "Research", training: "Training", workflows: "Workflows & jobs",
  environment: "Environment", docs: "Docs"
};
const PAGES = [...document.querySelectorAll("section.page[data-page]")].map((section) => section.dataset.page);

function currentPage() {
  const name = location.hash.replace(/^#/, "");
  return PAGES.includes(name) ? name : "today";
}

function route() {
  const page = currentPage();
  for (const section of document.querySelectorAll("section.page[data-page]")) section.hidden = section.dataset.page !== page;
  for (const link of document.querySelectorAll("#page-nav a[data-page]")) {
    const active = link.dataset.page === page;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("breadcrumb-page").textContent = PAGE_TITLES[page] || page;
  window.scrollTo({ top: 0, behavior: "auto" });
  if (window.ConsolePages) window.ConsolePages.render(page, state.snapshot);
  if (page === "research" && state.snapshot) renderResearch(state.snapshot.octopus);
}

function renderSnapshot(snapshot) {
  state.snapshot = snapshot;
  const demo = snapshot.mode === "demo";
  $("mode-banner").classList.toggle("demo", demo);
  $("mode-marker").textContent = demo ? "DEMO MODE" : "LOCAL MODE";
  $("mode-message").textContent = demo ? "Synthetic demo data · simulated DCA activity · research results are illustrative. No live orders are placed." : "Reading local run artifacts. Workflow launches are limited to the actions listed below.";
  renderWarnings("global-warnings", snapshot.warnings);
  renderDaily(snapshot.daily);
  renderResearch(snapshot.octopus);
  renderJobs(snapshot.jobs);
  pollRunningLog();
  renderActions(snapshot.actions);
  renderModules(snapshot.modules);
  $("updated-at").textContent = `Snapshot refreshed ${etTime.format(new Date())} ET`;
  if (window.ConsolePages) window.ConsolePages.render(currentPage(), snapshot);
  $("connection-label").textContent = "Local workspace connected";
  $("connection-dot").className = "connection-dot connected";
  $("error-banner").hidden = true;
}

function showError(message) {
  $("error-banner").textContent = message;
  $("error-banner").hidden = false;
}

async function readJson(response) {
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.message || `Request failed (${response.status})`);
  return data;
}

async function refresh() {
  if (state.loading) { state.queued = true; return; }
  clearTimeout(state.timer);
  state.loading = true;
  $("refresh").disabled = true;
  const date = $("report-date").value;
  const query = new URLSearchParams(state.dateInitialized ? { date } : {});
  $("export").href = `/api/export?${query}`;
  try {
    if (!state.token) {
      const bootstrap = await readJson(await fetch("/api/bootstrap", { cache: "no-store", signal: AbortSignal.timeout(15000) }));
      state.token = bootstrap.token;
    }
    const snapshot = await readJson(await fetch(`/api/snapshot?${query}`, { cache: "no-store", signal: AbortSignal.timeout(25000) }));
    if (date === $("report-date").value) {
      if (!state.dateInitialized) {
        $("report-date").value = snapshot.default_date || snapshot.daily?.date || date;
        state.dateInitialized = true;
        $("export").href = `/api/export?${new URLSearchParams({ date: $("report-date").value })}`;
      }
      renderSnapshot(snapshot);
    }
    else state.queued = true;
  } catch (error) {
    showError(`Could not refresh the workspace: ${error.message}. ${state.snapshot ? "The last successful snapshot remains visible and may be stale." : "Check the terminal running from.py, then refresh."}`);
    $("connection-label").textContent = "Connection needs attention";
    $("connection-dot").className = "connection-dot error";
  } finally {
    state.loading = false;
    $("refresh").disabled = false;
    if (state.queued) { state.queued = false; state.timer = setTimeout(refresh, 0); }
    else state.timer = setTimeout(refresh, 15000);
  }
}

async function startJob(action) {
  if (!state.token || state.launching.has(action)) return;
  state.launching.add(action);
  renderActions(state.snapshot?.actions);
  try {
    await postWithToken("/api/jobs", { action });
    await refresh();
    // Job output lives on the Workflows page; a launch from Today lands there.
    if (currentPage() === "today") {
      location.hash = "workflows";
      $("jobs").scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    }
  } catch (error) {
    showError(`Could not start ${words(action)}: ${error.message}. Check job history before trying again; a connection timeout can occur after a job was accepted.`);
  } finally {
    state.launching.delete(action);
    renderActions(state.snapshot?.actions);
  }
}
window.startJob = startJob;

async function postWithToken(path, body) {
  const submit = () => fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Console-Token": state.token }, body: JSON.stringify(body), signal: AbortSignal.timeout(15000) });
  let response = await submit();
  if (response.status === 403) {
    // A launchd restart rotates the token; a 403 proves nothing was accepted, so one retry is safe (Same Page round 1).
    state.token = null;
    const bootstrap = await readJson(await fetch("/api/bootstrap", { cache: "no-store", signal: AbortSignal.timeout(15000) }));
    state.token = bootstrap.token;
    response = await submit();
  }
  return readJson(response);
}

async function cancelJob(id) {
  if (!state.token) return;
  try {
    await postWithToken("/api/jobs/cancel", { job: id });
    await refresh();
  } catch (error) {
    showError(`Could not cancel the job: ${error.message}`);
  }
}

/* While a job runs, poll only its log delta every 1.5 s instead of the whole
   snapshot: the server hands back the text appended after the offset the page
   already holds (GUI expansion Phase 5). */
async function pollRunningLog() {
  clearTimeout(state.logTimer);
  const running = arr(state.snapshot?.jobs).find((job) => ["running", "queued"].includes(job.status));
  if (!running) return;
  const output = $("jobs-list").querySelector(`details[data-job-id="${CSS.escape(String(running.id))}"] pre`);
  if (output) {
    try {
      const query = new URLSearchParams({ job: running.id, offset: output.dataset.offset || "0" });
      const delta = await readJson(await fetch(`/api/jobs/log?${query}`, { cache: "no-store", signal: AbortSignal.timeout(10000) }));
      if (delta.text) {
        if (output.textContent === "Waiting for process output…") output.textContent = "";
        output.textContent += delta.text;
        output.dataset.offset = String(delta.offset);
      }
      if (!["running", "queued"].includes(delta.status)) { refresh(); return; }
    } catch (error) {
      // The next full snapshot repairs any missed delta; nothing to surface here.
    }
  }
  state.logTimer = setTimeout(pollRunningLog, 1500);
}

function updateClock() {
  $("clock").textContent = etClock.format(new Date());
  for (const node of document.querySelectorAll("[data-next-at]")) {
    const next = new Date(node.dataset.nextAt);
    const remaining = next.getTime() - Date.now();
    if (Number.isNaN(remaining)) node.textContent = "Next time unavailable";
    else if (remaining <= 0) node.textContent = "Window reached";
    else {
      const minutes = Math.ceil(remaining / 60000);
      const hours = Math.floor(minutes / 60);
      const days = Math.floor(hours / 24);
      node.textContent = days ? `In ${days}d ${hours % 24}h` : hours ? `In ${hours}h ${minutes % 60}m` : `In ${minutes}m`;
      node.title = `${next.toLocaleString("en-US", { timeZone: "America/New_York" })} ET`;
    }
  }
}

$("report-date").value = dateInET();
$("report-date").addEventListener("change", () => { if ($("report-date").value) { state.dateInitialized = true; refresh(); } });
$("refresh").addEventListener("click", refresh);
$("run-demo").addEventListener("click", () => startJob("demo-all"));
$("ticker-filter").addEventListener("input", renderForecasts);
$("component-filter").addEventListener("change", () => { renderForecasts(); renderPeriods(); });
$("period-filter").addEventListener("change", renderPeriods);
$("docs-close").addEventListener("click", () => $("docs-dialog").close());
window.addEventListener("hashchange", route);
route();
updateClock();
setInterval(updateClock, 1000);
refresh();
