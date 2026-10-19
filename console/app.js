/* Local-only operations dashboard. Artifact text is always inserted as text. */
"use strict";

const $ = (id) => document.getElementById(id);
const state = { snapshot: null, token: null, loading: false, queued: false, timer: null, logTimer: null, dateInitialized: false, serverTicker: "", docsRequest: 0, launching: new Set() };
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

function formatRelativeTime(isoString) {
  if (!isoString) return "";
  const then = new Date(isoString).getTime();
  if (Number.isNaN(then)) return String(isoString);
  const diffSec = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (diffSec < 45) return "just now";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}d ago`;
  try {
    return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(new Date(then));
  } catch (_) {
    return `${Math.floor(diffSec / 86400)}d ago`;
  }
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

// Matches the server's _MAX_RESULTS: 65 tickers x 3 models is 195 rows per component, which the old
// 100-row table silently cut at the 33rd ticker. The ticker filter still narrows from the full set.
const FORECAST_ROW_LIMIT = 2000;

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
  // Every ticker in the run, not just the (default-universe) rows shown, so the filter reaches any of them.
  const tickers = arr(octopus?.available_tickers).length ? arr(octopus.available_tickers) : [...new Set(forecasts.map((row) => row.ticker).filter(Boolean))].sort();
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
    const available = arr(state.snapshot?.octopus?.available_tickers);
    const uniqueTickers = available.length ? available : Array.from(new Set(all.map((f) => f.ticker).filter(Boolean))).sort();
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
  if (countEl) countEl.textContent = `${rows.length} ${component} ${rows.length === 1 ? "forecast" : "forecasts"}${rows.length > FORECAST_ROW_LIMIT ? ` · showing first ${FORECAST_ROW_LIMIT}` : ""}`;
  const forecastsBody = $("forecasts-body");
  if (!rows.length) {
    if (forecastsBody) emptyRow(forecastsBody, 7, all.length ? "No forecasts match these filters." : "No forecasts are available. Run the Octopus demo or connect an existing research run.");
    const chartEl = $("forecast-chart");
    if (chartEl) chartEl.replaceChildren();
    return;
  }
  if (forecastsBody) {
    forecastsBody.replaceChildren(...rows.slice(0, FORECAST_ROW_LIMIT).map((forecast) => {
      const row = el("tr");
      const name = el("td");
      name.append(el("strong", "", forecast.ticker || "-"));
      row.append(name, el("td", "", words(forecast.model)), el("td", "", `${forecast.origin_date || forecast.as_of || "-"} → ${forecast.forecast_date || forecast.target_label || "Date unresolved"}`), ...[forecast.point, forecast.p10, forecast.p50, forecast.p90].map((value) => el("td", "numeric", usd(value))));
      return row;
    }));
  }
  renderForecastChart(rows);
}

// One shared tooltip node and ONE scroll listener for the page's lifetime. The
// chart re-renders on every console refresh; a per-render tooltip would append a new
// node and a permanent window listener each time (Copilot review, PR #1597).
let chartTooltip = null;
function getChartTooltip() {
  if (!chartTooltip || !chartTooltip.isConnected) {
    const firstUse = chartTooltip === null;
    chartTooltip = el("div", "chart-tooltip");
    chartTooltip.hidden = true;
    document.body.append(chartTooltip);
    if (firstUse) {
      window.addEventListener("scroll", () => { if (chartTooltip) chartTooltip.hidden = true; }, { passive: true });
    }
  }
  return chartTooltip;
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
  const parsed = eligible.map((forecast) => {
    const baseline = isNumber(forecast.ref_close)
      ? Number(forecast.ref_close)
      : (isNumber(forecast.p50) ? Number(forecast.p50) : Number(forecast.point));
    const p10 = Number(forecast.p10);
    const p90 = Number(forecast.p90);
    const point = Number(forecast.point);
    const base = baseline || 1;
    const p10Pct = (p10 - base) / base;
    const p90Pct = (p90 - base) / base;
    const pointPct = (point - base) / base;
    return { forecast, baseline, p10, p90, point, p10Pct, p90Pct, pointPct };
  });
  const allMinPct = Math.min(0, ...parsed.flatMap((r) => [r.p10Pct, r.pointPct]));
  const allMaxPct = Math.max(0, ...parsed.flatMap((r) => [r.p90Pct, r.pointPct]));
  const maxAbs = Math.max(Math.abs(allMinPct), Math.abs(allMaxPct), 0.02);
  const headroom = maxAbs * 1.25;
  const scaleMin = -headroom;
  const scaleMax = headroom;
  const scaleSpan = scaleMax - scaleMin || 1;
  const x = (pct) => 15 + ((pct - scaleMin) / scaleSpan) * 370;
  const zeroX = x(0);

  function showChartTip(e, title, value, detail) {
    const tip = getChartTooltip();
    tip.replaceChildren(
      el("div", "tip-title", title),
      el("div", "tip-value", value),
      el("div", "tip-detail", detail)
    );
    tip.hidden = false;
    positionChartTip(e);
  }
  function positionChartTip(e) {
    const tip = getChartTooltip();
    if (!tip || tip.hidden) return;
    const target = e.currentTarget || e.target;
    const rect = target.getBoundingClientRect();
    const tipRect = tip.getBoundingClientRect();
    const left = Math.max(10, Math.min(window.innerWidth - tipRect.width - 10, rect.left + rect.width / 2 - tipRect.width / 2));
    const top = rect.top - tipRect.height - 8 > 10 ? rect.top - tipRect.height - 8 : rect.bottom + 8;
    tip.style.left = `${left + window.scrollX}px`;
    tip.style.top = `${top + window.scrollY}px`;
  }
  function hideChartTip() {
    const tip = getChartTooltip();
    if (tip) tip.hidden = true;
  }

  const nodes = parsed.map(({ forecast, p10, p90, point, p10Pct, p90Pct, pointPct }) => {
    const row = el("div", "range-row");
    const label = el("div", "range-label");
    label.append(el("strong", "", forecast.ticker), el("span", "range-model", words(forecast.model)));
    const svg = svgNode("svg", {
      viewBox: "0 0 400 28",
      preserveAspectRatio: "none",
      class: "range-graphic",
      role: "img",
      "aria-label": `${forecast.ticker}: P10 ${usd(p10)}, point ${usd(point)}, P90 ${usd(p90)}.`
    });
    const x1 = Math.min(x(p10Pct), x(p90Pct));
    const x2 = Math.max(x(p10Pct), x(p90Pct));
    const ptX = x(pointPct);
    const bandWidth = Math.max(4, x2 - x1);

    const ref = isNumber(forecast.ref_close) ? Number(forecast.ref_close) : null;
    const baseVal = ref || (isNumber(forecast.p50) ? Number(forecast.p50) : point);
    const diff = ref ? ((point - ref) / ref) * 100 : 0;
    const p10Diff = ref ? ((p10 - ref) / ref) * 100 : 0;
    const p90Diff = ref ? ((p90 - ref) / ref) * 100 : 0;
    const p10PctStr = ref ? ` (${p10Diff >= 0 ? "+" : ""}${p10Diff.toFixed(1)}%)` : "";
    const p90PctStr = ref ? ` (${p90Diff >= 0 ? "+" : ""}${p90Diff.toFixed(1)}%)` : "";
    const ptPctStr = ref ? ` (${diff >= 0 ? "+" : ""}${diff.toFixed(1)}%)` : "";

    const track = svgNode("line", { x1: 0, x2: 400, y1: 14, y2: 14, class: "range-track" });
    const refLine = svgNode("line", { x1: zeroX, x2: zeroX, y1: 4, y2: 24, class: "range-ref" });
    const band = svgNode("rect", { x: x1, y: 7, width: bandWidth, height: 14, rx: 7, class: "range-band" });
    const cap1 = svgNode("line", { x1, x2: x1, y1: 6, y2: 22, class: "range-cap" });
    const cap2 = svgNode("line", { x1: x2, x2, y1: 6, y2: 22, class: "range-cap" });
    const pointDot = svgNode("circle", { cx: ptX, cy: 14, r: 4.5, class: "range-point" });

    const bind = (elem, title, value, detail) => {
      const t = document.createElementNS(ns, "title");
      t.textContent = `${title}: ${value} — ${detail}`;
      elem.append(t);
      elem.addEventListener("mouseenter", (e) => showChartTip(e, title, value, detail));
      elem.addEventListener("mousemove", positionChartTip);
      elem.addEventListener("mouseleave", hideChartTip);
    };

    bind(refLine, `${forecast.ticker} · Prior Close Baseline`, `${usd(baseVal)} (0.0%)`, "Observed reference close prior to forecast horizon.");
    bind(band, `${forecast.ticker} · P10–P90 Uncertainty Interval`, `${usd(p10)} — ${usd(p90)}`, `80% confidence interval spanning ${usd(p90 - p10)} (${((p90 - p10) / (baseVal || 1) * 100).toFixed(1)}% range of estimated uncertainty).`);
    bind(cap1, `${forecast.ticker} · P10 Downside Bound`, `${usd(p10)}${p10PctStr}`, "10th percentile bound: estimated 10% chance price falls below this level.");
    bind(cap2, `${forecast.ticker} · P90 Upside Bound`, `${usd(p90)}${p90PctStr}`, "90th percentile bound: estimated 90% of outcomes expected below this level.");
    bind(pointDot, `${forecast.ticker} · Point Forecast`, `${usd(point)}${ptPctStr}`, `Primary predicted close price relative to prior close ${usd(baseVal)}.`);

    svg.append(track, refLine, band, cap1, cap2, pointDot);

    const pctLabel = ref ? ` (${diff >= 0 ? "+" : ""}${diff.toFixed(1)}%)` : "";
    row.append(label, svg, el("div", "range-value", `${usd(p10)} - ${usd(p90)}${pctLabel}`));
    return row;
  });
  const noteText = eligible.length > 1
    ? `All ${eligible.length} available ranges. Centered on prior close (dashed line); hover any point to view explanation.`
    : `1 available range. Centered on prior close (dashed line); hover any point to view explanation.`;
  nodes.push(el("p", "chart-note", noteText));
  chartEl.onscroll = hideChartTip;
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
    const icon = el("span", "action-icon", /octopus/.test(action.id) ? "⌁" : /report/.test(action.id) ? "≡" : "↗");
    const title = el("div", "action-title");
    title.append(el("h3", "", action.label || words(action.id)));

    const meta = el("div", "action-meta");
    const runningJob = arr(state.snapshot?.jobs).find((job) => job.action === action.id && ["running", "queued"].includes(job.status));
    let lu = action.last_updated;
    if (state.launching.has(action.id) || runningJob) {
      lu = { status: "running", label: "Updating…", timestamp: runningJob?.started_at || new Date().toISOString() };
    }
    if (lu) {
      let label = lu.label;
      let badgeClass = "tag action-updated";
      if (lu.status === "running" || lu.status === "queued") {
        label = "Updating…";
        badgeClass = "status-pill running";
      } else if (!label && lu.timestamp) {
        label = `Updated ${formatRelativeTime(lu.timestamp)}`;
      } else if (!label) {
        label = "Never run";
      }
      const updatedEl = el("span", badgeClass, label);
      if (lu.timestamp) {
        try {
          updatedEl.title = `Last update: ${new Date(lu.timestamp).toLocaleString()}`;
        } catch (_) {}
      }
      meta.append(updatedEl);
    }
    heading.append(icon, title, meta);
    card.append(heading, el("p", "action-description", action.description || "Launch this configured program entry point."));
    if (action.reason) card.append(el("p", "action-reason", action.reason));
    const bottom = el("div", "action-bottom");
    const pending = state.launching.has(action.id) || Boolean(runningJob);
    const button = el("button", "button ghost", pending ? "Running…" : "Run workflow →");
    button.type = "button";
    button.disabled = !action.available || pending;
    button.addEventListener("click", () => startJob(action.id));
    bottom.append(el("span", "tag", action.available ? "Available" : "Setup needed"), button);
    card.append(bottom);
    return card;
  });
  $("actions").replaceChildren(...(nodes.length ? nodes : [el("div", "panel empty-state", "No workflows were advertised by the server.")]));
  const demo = arr(actions).find((action) => action.id === "demo-all");
  const running = state.launching.has("demo-all") || arr(state.snapshot?.jobs).some((job) => job.action === "demo-all" && ["running", "queued"].includes(job.status));
  $("run-demo").disabled = running || Boolean(demo && !demo.available);
  $("run-demo").textContent = running ? "Demo running…" : "▶ Run full demo";
}

function renderJobs(jobs) {
  const previousOpen = new Map([...$("jobs-list").querySelectorAll("details")].map((node) => [node.dataset.jobId, node.open]));
  const rows = arr(jobs);
  $("job-count").textContent = `${rows.length} ${rows.length === 1 ? "job" : "jobs"}`;
  $("jobs-list").replaceChildren(...(rows.length ? rows.map((job) => {
    const detail = el("details", "job");
    detail.dataset.jobId = String(job.id);
    let isOpen = false;
    if (previousOpen.has(String(job.id))) {
      isOpen = previousOpen.get(String(job.id));
    } else {
      isOpen = (String(job.id) === state.lastLaunchedJobId) || /running|queued|failed|error/.test(job.status);
    }
    detail.open = isOpen;
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

function viewJobOutput(jobId) {
  if (currentPage() !== "workflows") {
    location.hash = "workflows";
  }
  requestAnimationFrame(() => {
    setTimeout(() => {
      const details = $("jobs-list")?.querySelector(`details[data-job-id="${CSS.escape(String(jobId))}"]`);
      if (details) {
        details.open = true;
        details.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "center" });
        const pre = details.querySelector("pre");
        if (pre) {
          pre.style.outline = "2px solid var(--green)";
          setTimeout(() => { pre.style.outline = ""; }, 2500);
        }
      } else {
        $("jobs")?.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
      }
    }, 50);
  });
}
window.viewJobOutput = viewJobOutput;

function renderModules(modules) {
  const nodes = arr(modules).map((module) => {
    const card = el("article", "module-row module-card");
    const heading = el("div", "module-card-header");
    const title = el("h3", "module-card-title", module.name || words(module.id));

    const infoWrap = el("div", "module-info-wrap");
    const infoBtn = el("button", "module-info-btn", "ⓘ");
    infoBtn.type = "button";
    infoBtn.setAttribute("aria-label", `Information for ${module.name || module.id}`);
    infoBtn.title = "View description & copy command";

    const popover = el("div", "module-info-popover");
    const popoverTop = el("div", "module-info-top");
    popoverTop.append(statusPill(module.status));
    const closeBtn = el("button", "module-info-close", "×");
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      infoWrap.classList.remove("active");
    });
    popoverTop.append(closeBtn);

    const popoverDesc = el("p", "module-info-desc", module.description || "No description provided.");
    popover.append(popoverTop, popoverDesc);

    const command = Array.isArray(module.command) ? module.command.join(" ") : String(module.command || "");
    if (command && command !== "Library API; see documentation") {
      const line = el("div", "module-command");
      const copyBtn = el("button", "button secondary small", "Copy");
      copyBtn.type = "button";
      copyBtn.setAttribute("aria-label", `Copy command for ${module.name || module.id}`);
      copyBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(command);
          copyBtn.textContent = "Copied";
          setTimeout(() => { copyBtn.textContent = "Copy"; }, 2000);
        } catch (_) {
          copyBtn.textContent = "Select to copy";
        }
      });
      line.append(el("code", "", command), copyBtn);
      popover.append(line);
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
      popover.append(docs);
    }

    infoWrap.append(infoBtn, popover);
    infoBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      infoWrap.classList.toggle("active");
    });
    heading.append(title, infoWrap);

    const body = el("div", "module-card-body");
    const actionsRow = el("div", "module-actions-row");

    const actionObj = module.action_id
      ? arr(state.snapshot?.actions).find((a) => a.id === module.action_id)
      : null;
    const runningJob = module.action_id
      ? arr(state.snapshot?.jobs).find((j) => j.action === module.action_id && ["running", "queued"].includes(j.status))
      : null;
    const latestJob = module.action_id
      ? arr(state.snapshot?.jobs).find((j) => j.action === module.action_id)
      : null;
    const isPending = Boolean(runningJob || (module.action_id && state.launching.has(module.action_id)));
    const isAvailable = !actionObj || actionObj.available !== false;

    let execBtn;
    if (module.action_id) {
      if (!isAvailable) {
        execBtn = el("button", "button secondary small module-exec-btn", "Setup needed");
        execBtn.type = "button";
        execBtn.title = `${actionObj?.reason || "Setup needed"}. Click for details.`;
        execBtn.addEventListener("click", () => {
          showError(`Cannot run ${module.name}: ${actionObj?.reason || "Prerequisite setup missing"}. Supply --octopus-run when launching from.py`);
        });
      } else {
        execBtn = el("button", "button ghost small module-exec-btn", isPending ? "Running…" : "▶ Run entry point");
        execBtn.type = "button";
        execBtn.disabled = isPending;
        execBtn.addEventListener("click", () => startJob(module.action_id));
      }
    } else if (command && /<[^>]+>/.test(command)) {
      execBtn = el("button", "button secondary small module-exec-btn", "Requires CLI args");
      execBtn.type = "button";
      execBtn.title = "This entry point requires CLI arguments. Click to copy command.";
      execBtn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(command);
          execBtn.textContent = "Copied command";
          setTimeout(() => { execBtn.textContent = "Requires CLI args"; }, 2000);
        } catch (_) {}
      });
    } else {
      execBtn = el("span", "tag", "Library API");
    }

    const resultBox = el("div", "module-result");
    if (latestJob) {
      resultBox.append(statusPill(latestJob.status));
      const timeStr = `${displayTime(latestJob.finished_at || latestJob.started_at)}${latestJob.returncode !== null && latestJob.returncode !== undefined ? ` · exit ${latestJob.returncode}` : ""}`;
      resultBox.append(el("span", "module-result-time", timeStr));
      const viewBtn = el("button", "button ghost small", "View output →");
      viewBtn.type = "button";
      viewBtn.addEventListener("click", () => viewJobOutput(latestJob.id));
      resultBox.append(viewBtn);
    } else if (module.action_id) {
      if (!isAvailable && actionObj?.reason) {
        const setupTag = el("span", "tag action-updated", "Setup needed");
        setupTag.title = actionObj.reason;
        resultBox.append(setupTag);
      } else {
        resultBox.append(el("span", "tag action-updated", "Not executed yet"));
      }
    }

    actionsRow.append(execBtn, resultBox);
    body.append(actionsRow);
    card.append(heading, body);
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
  const release = snapshot.release || {};
  const sandbox = release.channel === "sandbox";
  $("mode-banner").classList.toggle("demo", demo);
  $("mode-marker").textContent = sandbox ? "SANDBOX" : demo ? "DEMO MODE" : "LOCAL MODE";
  $("channel-badge").hidden = !sandbox;
  $("console-version").textContent = release.version ? `v${release.version} · ${release.channel || "stable"}` : "v—";
  document.title = `${sandbox ? "SANDBOX · " : ""}DCA · Operations console`;
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
  if (state.serverTicker) query.set("ticker", state.serverTicker);
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
  if (state.launching.has(action)) return;
  state.launching.add(action);
  renderActions(state.snapshot?.actions);
  if (currentPage() !== "workflows") {
    location.hash = "workflows";
  }
  const jobsEl = $("jobs");
  if (jobsEl) {
    jobsEl.scrollIntoView({ behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  }
  try {
    const started = await postWithToken("/api/jobs", { action });
    if (started?.job?.id) {
      state.lastLaunchedJobId = String(started.job.id);
      if (state.snapshot) {
        state.snapshot.jobs = [started.job, ...arr(state.snapshot.jobs).filter((j) => String(j.id) !== String(started.job.id))];
        renderJobs(state.snapshot.jobs);
        pollRunningLog();
      }
      viewJobOutput(started.job.id);
    }
    await refresh();
  } catch (error) {
    showError(`Could not start ${words(action)}: ${error.message}. Check job history before trying again; a connection timeout can occur after a job was accepted.`);
  } finally {
    state.launching.delete(action);
    renderActions(state.snapshot?.actions);
  }
}
window.startJob = startJob;

async function postWithToken(path, body) {
  if (!state.token) {
    const bootstrap = await readJson(await fetch("/api/bootstrap", { cache: "no-store", signal: AbortSignal.timeout(15000) }));
    state.token = bootstrap.token;
  }
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

function initTheme() {
  const toggle = $("theme-toggle");
  const icon = $("theme-icon");
  let saved = null;
  try {
    saved = localStorage.getItem("dca_theme");
  } catch (error) {
    // Local storage access may fail in sandboxed or strict privacy contexts.
  }
  const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const currentTheme = saved || (prefersDark ? "dark" : "light");

  // Only an explicit toggle click persists: the initial and OS-driven applications
  // must stay unsaved, or the media-query listener below would see a "saved"
  // preference and ignore later system theme changes (Copilot review, PR #1597).
  function applyTheme(theme, { persist = false } = {}) {
    document.documentElement.dataset.theme = theme;
    if (icon) icon.textContent = theme === "dark" ? "☀️" : "🌙";
    if (toggle) {
      const label = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
      toggle.title = label;
      toggle.setAttribute("aria-label", label);
    }
    if (!persist) return;
    try {
      localStorage.setItem("dca_theme", theme);
    } catch (error) {
      // Ignore storage errors.
    }
  }

  applyTheme(currentTheme);

  if (toggle) {
    toggle.addEventListener("click", () => {
      const active = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(active, { persist: true });
    });
  }

  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (event) => {
      try {
        if (!localStorage.getItem("dca_theme")) {
          applyTheme(event.matches ? "dark" : "light");
        }
      } catch (error) {
        applyTheme(event.matches ? "dark" : "light");
      }
    });
  }
}

initTheme();
$("report-date").value = dateInET();
$("report-date").addEventListener("change", () => { if ($("report-date").value) { state.dateInitialized = true; refresh(); } });
$("refresh").addEventListener("click", refresh);
$("run-demo").addEventListener("click", () => startJob("demo-all"));
// The default view is the primary universe; typing an exact ticker from the run asks the server for it
// (any ticker is reachable), and clearing or typing a partial returns to the default view.
function syncServerTicker() {
  const typed = $("ticker-filter").value.trim().toUpperCase();
  const available = arr(state.snapshot?.octopus?.available_tickers);
  const wanted = typed && available.includes(typed) ? typed : "";
  if (wanted === (state.serverTicker || "")) return;
  state.serverTicker = wanted;
  refresh();
}
$("ticker-filter").addEventListener("input", () => { renderForecasts(); syncServerTicker(); });
$("component-filter").addEventListener("change", () => { renderForecasts(); renderPeriods(); });
$("period-filter").addEventListener("change", renderPeriods);
$("docs-close").addEventListener("click", () => $("docs-dialog").close());
document.addEventListener("click", (e) => {
  if (!e.target.closest(".module-info-wrap")) {
    document.querySelectorAll(".module-info-wrap.active").forEach((w) => w.classList.remove("active"));
  }
});
window.addEventListener("hashchange", route);
route();
updateClock();
setInterval(updateClock, 1000);
refresh();
