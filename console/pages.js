/* Per-page renderers for the multi-page console.
   Phase 0 of the GUI expansion registers the page registry and the placeholder
   pages; later phases replace each placeholder with its real renderer. Every
   renderer builds DOM from text nodes only, never from markup strings: artifact text is
   data. */
"use strict";

window.ConsolePages = (() => {
  const renderers = new Map();

  function register(page, renderer) {
    renderers.set(page, renderer);
  }

  function render(page, snapshot) {
    const renderer = renderers.get(page);
    if (!renderer) return;
    try {
      renderer(snapshot);
    } catch (error) {
      // One broken panel must not take the router or the other pages down.
      console.error(`Page "${page}" failed to render`, error);
    }
  }

  function text(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (value !== undefined) node.textContent = String(value);
    return node;
  }

  /* Shared building blocks for every page renderer. All of them take data and
     return DOM built from text nodes; none of them accepts markup. */
  const numberFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
  const isNumber = (value) => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
  const show = (value) => value === null || value === undefined || value === "" ? "—" : typeof value === "boolean" ? (value ? "yes" : "no") : isNumber(value) && typeof value !== "string" ? numberFormat.format(Number(value)) : String(value);

  function badge(value) {
    const normalized = String(value ?? "unknown").toLowerCase();
    let tone = "neutral";
    if (/failed|error|corrupt|refused|abort|blocked|at.risk|missing|no.evidence/.test(normalized)) tone = "bad";
    else if (/warn|stale|unverified|recovery|incomplete|skip|partial|unregistered|disabled|not_registered|unknown/.test(normalized)) tone = "warning";
    else if (/running|queued|pending|loading/.test(normalized)) tone = "running";
    else if (/valid|complete|success|ready|healthy|ok|available|configured|present|yes|true|registered|enabled|installed|set$|indexed|^verified$/.test(normalized)) tone = "good";
    return text("span", `status-pill ${tone}`, String(value ?? "unknown").replaceAll("_", " "));
  }

  function table(columns, rows, emptyMessage) {
    const wrap = text("div", "table-scroll");
    const node = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const column of columns) headRow.append(text("th", column.numeric ? "numeric" : "", column.label));
    head.append(headRow);
    const body = document.createElement("tbody");
    if (!rows.length) {
      const row = document.createElement("tr");
      const cell = text("td", "empty-cell", emptyMessage || "Nothing to show.");
      cell.colSpan = columns.length;
      row.append(cell);
      body.append(row);
    }
    for (const item of rows) {
      const row = document.createElement("tr");
      for (const column of columns) {
        const value = typeof column.get === "function" ? column.get(item) : item[column.key];
        const cell = text("td", column.numeric ? "numeric" : "");
        if (column.badge) cell.append(badge(value));
        else if (value instanceof Node) cell.append(value);
        else cell.textContent = show(value);
        row.append(cell);
      }
      if (typeof rows.onRow === "function") rows.onRow(row, item);
      body.append(row);
    }
    node.append(head, body);
    wrap.append(node);
    return wrap;
  }

  function keyValues(pairs) {
    const list = document.createElement("dl");
    list.className = "kv-list";
    for (const [key, value] of pairs) {
      const item = text("div", "kv-item");
      item.append(text("dt", "", key));
      const definition = text("dd", "");
      if (value instanceof Node) definition.append(value);
      else definition.textContent = show(value);
      item.append(definition);
      list.append(item);
    }
    return list;
  }

  function panel(title, body, meta) {
    const section = text("section", "panel section-space");
    const top = text("div", "section-top");
    const heading = text("div", "");
    heading.append(text("h3", "", title));
    top.append(heading);
    if (meta !== undefined) top.append(meta instanceof Node ? meta : text("span", "tag", meta));
    section.append(top);
    for (const part of Array.isArray(body) ? body : [body]) if (part) section.append(part instanceof Node ? part : text("p", "muted", part));
    return section;
  }

  function warnings(list) {
    const box = text("div", "notice-list");
    for (const item of Array.isArray(list) ? list : []) box.append(text("div", "notice", item));
    return box;
  }

  const cache = new Map();
  /* Fetch one page route once per snapshot refresh cycle; a page re-renders
     from the same data until the next refresh invalidates the cache. */
  async function fetchJson(path, { ttl = 12000 } = {}) {
    const cached = cache.get(path);
    if (cached && Date.now() - cached.at < ttl) return cached.data;
    const response = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(25000) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
    cache.set(path, { at: Date.now(), data });
    return data;
  }

  function mount(section, ...children) {
    const target = section.querySelector("[data-page-body]") || section;
    target.replaceChildren(...children.filter(Boolean));
  }

  function loading(section, message) {
    mount(section, text("div", "panel empty-state", message || "Loading…"));
  }

  function failure(section, error) {
    mount(section, text("div", "error-banner", `Could not load this page: ${error.message}`));
  }

  function copyButton(command) {
    const button = text("button", "button secondary small", "Copy");
    button.type = "button";
    button.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(command); button.textContent = "Copied"; }
      catch { button.textContent = "Select to copy"; }
    });
    return button;
  }

  /* The placeholder pages already show which observed roots the console will
     read, so the router and the labelled-root contract can be verified before
     any page-specific reader exists. */
  function renderRoots(section, snapshot) {
    const target = section.querySelector("[data-roots]");
    if (!target) return;
    const roots = Array.isArray(snapshot?.roots) ? snapshot.roots : [];
    target.replaceChildren(...(roots.length
      ? roots.map((root) => text("span", "tag", `${root.kind}: ${root.label}`))
      : [text("span", "muted", "No observed roots reported yet.")]));
  }

  for (const section of document.querySelectorAll("section.page[data-page]")) {
    if (section.hasAttribute("data-placeholder")) {
      register(section.dataset.page, (snapshot) => renderRoots(section, snapshot));
    }
  }

  /* Docs page: the catalog with registration status, and a text-only reader. */
  const docsSection = document.querySelector('section.page[data-page="docs"]');
  if (docsSection && docsSection.querySelector("[data-doc-list]")) {
    let selected = null;
    let renderedIndex = "";

    async function openDoc(path) {
      selected = path;
      const reader = docsSection.querySelector("[data-doc-reader]");
      reader.replaceChildren(text("h3", "", path), text("pre", "doc-body", "Loading…"));
      try {
        const response = await fetch(`/api/docs?${new URLSearchParams({ path })}`, { cache: "no-store", signal: AbortSignal.timeout(15000) });
        if (!response.ok) throw new Error(`Documentation unavailable (${response.status}).`);
        reader.querySelector("pre").textContent = await response.text();
      } catch (error) {
        reader.querySelector("pre").textContent = error.message;
      }
    }

    register("docs", async () => {
      const list = docsSection.querySelector("[data-doc-list]");
      try {
        const index = await fetchJson("/api/docs/index", { ttl: 60000 });
        // The snapshot loop re-renders every 15 s; rebuilding an unchanged
        // table would only drop the reader's scroll position and focus.
        const fingerprint = JSON.stringify(index);
        if (fingerprint === renderedIndex) return;
        renderedIndex = fingerprint;
        const rows = index.docs;
        rows.onRow = (row, item) => {
          row.classList.add("clickable-row");
          row.tabIndex = 0;
          row.addEventListener("click", () => openDoc(item.path));
          row.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openDoc(item.path); } });
        };
        const registration = (item) => item.group !== "docs" ? "n/a" : item.in_readme ? (item.in_claude_md ? "indexed, in CLAUDE.md" : "indexed") : "unregistered";
        list.replaceChildren(
          warnings(index.warnings),
          table([
            { label: "Page", get: (item) => item.title },
            { label: "Path", key: "path" },
            { label: "Group", get: (item) => item.mirror ? "live mirror (source of truth is outside this repo)" : item.group },
            { label: "Index status", get: registration, badge: true },
            { label: "In program directory", get: (item) => item.in_catalog, badge: true },
          ], rows, "No documentation pages were listed."),
        );
        if (selected) openDoc(selected);
      } catch (error) {
        list.replaceChildren(text("div", "error-banner", `Could not load the documentation index: ${error.message}`));
      }
    });
  }

  /* Environment page. */
  const envSection = document.querySelector('section.page[data-page="environment"]');
  if (envSection && envSection.querySelector("[data-page-body]")) {
    let renderedEnv = "";
    register("environment", async () => {
      try {
        const data = await fetchJson("/api/env", { ttl: 30000 });
        const fingerprint = JSON.stringify(data);
        if (fingerprint === renderedEnv) return;
        renderedEnv = fingerprint;
        const session = data.session;
        const gateRows = data.gates.map((gate) => {
          const line = text("div", "module-command");
          line.append(text("code", "", gate.command), copyButton(gate.command));
          return { name: gate.name, command: line };
        });
        const packageKeys = Object.keys(Object.assign({}, ...data.interpreters.map((row) => row.packages))).filter((key) => key !== "error");
        mount(envSection,
          warnings(data.warnings),
          panel("Session", keyValues([
            ["Mode", session.mode], ["Day", session.day || "rolling"], ["Day rule", session.day_rule],
            ["Console interpreter", session.python], ["Research interpreter", session.research_python],
            ["Config directory", session.config_dir], ["Octopus run", session.octopus_run],
            ["Observed roots", session.roots.map((root) => `${root.kind}: ${root.label}`).join(" · ")],
          ])),
          panel("Interpreters and packages", table([
            { label: "Interpreter", key: "name" }, { label: "Python", key: "python" },
            ...packageKeys.map((key) => ({ label: key, get: (row) => row.packages[key] === true ? "installed" : row.packages[key] === false ? "missing" : "unknown", badge: true })),
          ], data.interpreters, "No interpreters probed."), "Probed with importlib.find_spec; the research interpreter is the one --research-python names."),
          panel("Environment variables", table([
            { label: "Variable", key: "name" }, { label: "Set", get: (row) => row.set ? "set" : "unset", badge: true },
            { label: "Sensitive", get: (row) => row.sensitive ? "value never shown" : "path or setting" }, { label: "Purpose", key: "purpose" },
          ], data.env, "No variables known."), "Names and presence only. Values are never read into a response."),
          panel("Roots and mounts", table([
            { label: "Root", key: "name" }, { label: "Label", key: "label" },
            { label: "Present", get: (row) => row.present === null ? "not configured" : row.present ? "present" : "missing", badge: true },
            { label: "Symlink", get: (row) => row.symlink }, { label: "Note", key: "note" },
          ], data.roots, "No roots configured."), "Labels are a basename plus a short digest; the ticker archive is inspected with lstat only."),
          panel("Access policy (this listener)", keyValues([
            ["Bind", data.access.bind], ["Port", data.access.port], ["Trusted hosts", (data.access.trusted_hosts || []).join(", ") || "(none)"],
            ["Auth", data.access.auth], ["Behind proxy", data.access.behind_proxy],
            ["Transport", data.access.plaintext_warning ? "plain HTTP on a non-loopback bind: credential and token visible on the network" : "loopback"],
          ])),
          panel("Repository gates", table([
            { label: "Gate", key: "name" }, { label: "Command", key: "command" },
          ], gateRows, "No gates listed."), "Run from the checkout; the console never runs these on a page view."),
          panel("Stray root-level files", table([
            { label: "File", key: "name" }, { label: "Present", get: (row) => row.present ? "present" : "absent", badge: true },
            { label: "Size", key: "size", numeric: true }, { label: "Note", key: "note" },
          ], data.stray_files, "Nothing stray at the repository root.")),
        );
      } catch (error) {
        failure(envSection, error);
      }
    });
  }

  /* Workflows page: the Gate 5 replay bench and the artifacts recent launchers left behind. */
  const workflowsSection = document.querySelector('section.page[data-page="workflows"]');
  const bench = workflowsSection ? workflowsSection.querySelector("[data-launch-bench]") : null;
  if (bench) {
    let renderedBench = "";
    register("workflows", async (snapshot) => {
      try {
        const data = await fetchJson("/api/launch", { ttl: 12000 });
        const fingerprint = JSON.stringify([data, arr(snapshot?.jobs).map((job) => [job.id, job.status])]);
        if (fingerprint === renderedBench) return;
        renderedBench = fingerprint;
        const actions = new Map(data.actions.map((action) => [action.id, action]));
        const running = new Set(arr(snapshot?.jobs).filter((job) => ["running", "queued"].includes(job.status)).map((job) => job.action));
        const slotRows = Object.entries(data.payloads).map(([slot, payload]) => {
          const action = actions.get(`gate5-replay-${slot}`) || {};
          const button = text("button", "button ghost", running.has(action.id) ? "Running…" : "Replay");
          button.type = "button";
          button.disabled = !action.available || running.size > 0 || typeof window.startJob !== "function";
          button.title = action.available ? "Re-run the generator on this saved payload (Gate 5 only)" : (action.reason || "");
          button.addEventListener("click", () => window.startJob(action.id));
          return {
            slot: `${slot.slice(0, 2)}:${slot.slice(2)} ET`,
            payload: payload ? payload.relative_path : "none found",
            root: payload ? `${payload.root_label}${payload.at_risk ? " (at risk)" : ""}` : "—",
            day: payload ? `${payload.trading_day || "unknown"} (${payload.day_source})` : "—",
            figure: action.needs_deployed_figure ? "risk evidence required" : "not needed",
            status: action.available ? "ready" : "unavailable",
            reason: action.available ? "" : action.reason,
            launch: button,
          };
        });
        const artifactLink = (job, item) => {
          const link = text("a", "", `${item.name} (${item.kind}, ${show(item.bytes)} bytes)`);
          link.href = `/api/artifact?${new URLSearchParams({ job, name: item.name })}`;
          link.target = "_blank";
          link.rel = "noopener";
          return link;
        };
        const launchRows = data.launches.map((entry) => {
          const links = text("div", "artifact-links");
          for (const item of entry.artifacts || []) links.append(artifactLink(entry.job, item));
          if (!(entry.artifacts || []).length) links.append(text("span", "muted", "no artifact was produced"));
          const inputs = Object.entries(entry.inputs || {}).map(([key, value]) => `${key}: ${typeof value === "object" && value ? JSON.stringify(value) : show(value)}`).join(" · ");
          return { label: entry.label || entry.action, job: entry.job, when: entry.written_at, inputs, artifacts: links };
        });
        bench.replaceChildren(
          panel("Gate 5 replay bench", [
            text("p", "muted", "Each replay re-runs the slot's candidate generator on the newest saved market-data payload found under the observed roots. It is a replay of saved inputs, labelled as such in its document; no broker is contacted and no order is placed. Slots that need the 7-day deployment figure refuse to run unless the primary root's risk evidence is complete."),
            table([
              { label: "Slot", key: "slot" }, { label: "Payload", key: "payload" }, { label: "Root", key: "root" },
              { label: "Trading day (source)", key: "day" }, { label: "Deployment figure", key: "figure" },
              { label: "Readiness", key: "status", badge: true }, { label: "Reason", key: "reason" }, { label: "", key: "launch" },
            ], slotRows, "No slots."),
          ], "replay of saved inputs"),
          panel("Launcher artifacts", [
            text("p", "muted", data.boundary),
            table([
              { label: "Launcher", key: "label" }, { label: "Job", key: "job" }, { label: "Written", key: "when" },
              { label: "Inputs", key: "inputs" }, { label: "Artifacts", key: "artifacts" },
            ], launchRows, "No launcher has produced artifacts yet. Start one from the cards above."),
          ], `${launchRows.length} recent`),
        );
      } catch (error) {
        bench.replaceChildren(text("div", "error-banner", `Could not load the launch bench: ${error.message}`));
      }
    });
  }

  function arr(value) {
    return Array.isArray(value) ? value : [];
  }

  /* Generic renderer for a JSON-safe section whose exact shape is open-ended:
     scalars become key/values, a list of objects becomes a table over the
     first columns seen, and nested objects become sub-panels. Every reader
     module's output is JSON-safe and already redacted, so this can never
     receive markup. */
  function auto(value, title, depth = 0) {
    if (Array.isArray(value)) {
      if (!value.length) return text("p", "muted", `${title}: none`);
      if (value.every((item) => item && typeof item === "object" && !Array.isArray(item))) {
        const columns = [];
        for (const item of value.slice(0, 50)) for (const key of Object.keys(item)) if (!columns.includes(key) && columns.length < 10) columns.push(key);
        return table(columns.map((key) => ({ label: key.replaceAll("_", " "), get: (row) => typeof row[key] === "object" && row[key] !== null ? JSON.stringify(row[key]).slice(0, 160) : row[key] })), value.slice(0, 200), "empty");
      }
      return text("p", "", value.map(show).join(", "));
    }
    if (value && typeof value === "object") {
      const scalars = Object.entries(value).filter(([, item]) => item === null || typeof item !== "object");
      const nested = Object.entries(value).filter(([, item]) => item !== null && typeof item === "object");
      const parts = [];
      if (scalars.length) parts.push(keyValues(scalars.map(([key, item]) => [key.replaceAll("_", " "), item])));
      for (const [key, item] of nested) {
        if (key === "warnings") { parts.push(warnings(item)); continue; }
        const child = auto(item, key, depth + 1);
        if (depth < 2) {
          const block = text("div", "auto-block");
          block.append(text("h3", "auto-title", key.replaceAll("_", " ")), child);
          parts.push(block);
        } else parts.push(child);
      }
      const wrap = text("div", "auto-section");
      wrap.append(...parts);
      return wrap;
    }
    return text("p", "", show(value));
  }

  function dateParam(snapshot) {
    const day = snapshot?.daily?.date || document.getElementById("report-date")?.value;
    return day ? `?${new URLSearchParams({ date: day })}` : "";
  }

  function fingerprinted(section, render) {
    let last = "";
    return async (snapshot) => {
      try {
        const [data, key] = await render(snapshot);
        const fingerprint = JSON.stringify([key, data]);
        if (fingerprint === last) return;
        last = fingerprint;
        return data;
      } catch (error) {
        failure(section, error);
        return undefined;
      }
    };
  }

  /* Today: the truth strip above the daily picture. */
  const strip = document.querySelector("[data-today-strip]");
  if (strip) {
    let lastStrip = "";
    register("today", async (snapshot) => {
      try {
        const data = await fetchJson(`/api/today${dateParam(snapshot)}`);
        const fingerprint = JSON.stringify(data);
        if (fingerprint === lastStrip) return;
        lastStrip = fingerprint;
        const gates = text("div", "gate-list");
        for (const gate of arr(data.gates?.gates)) {
          const row = text("div", "");
          const name = text("span", "", gate.name.replaceAll("_", " "));
          name.append(text("small", "", gate.detail || ""));
          row.append(name, badge(gate.state));
          gates.append(row);
        }
        const window = data.window || {};
        const countdown = text("div", `countdown-big${window.expired || window.days_remaining !== null && window.days_remaining <= 14 ? " bad" : ""}`,
          window.expired ? "window expired" : window.days_remaining === null || window.days_remaining === undefined ? "—" : `${window.days_remaining} days left`);
        const pointers = text("div", "");
        for (const stream of arr(data.pointers?.streams).filter((row) => !row.alias)) {
          const row = text("div", "pointer-row");
          row.append(text("span", "", `${stream.stream.replaceAll("_", " ")}${stream.at_risk ? " (at risk)" : ""}`), badge(stream.evidence ? "scheduled evidence" : stream.evidence_label || stream.status));
          pointers.append(row);
        }
        const gatePanel = text("section", "panel");
        gatePanel.append(text("h3", "", "Live-money gates"), text("p", "muted compact", data.gates?.banner || ""), gates, warnings(data.gates?.warnings));
        const windowPanel = text("section", "panel");
        windowPanel.append(text("h3", "", "Approved trading window"), countdown,
          text("p", "muted compact", `${window.start || "?"} to ${window.end || "?"} · calendar covers ${(window.calendar_years || []).join(", ") || "?"}${window.calendar_covers_day === false ? " · this day is NOT covered" : ""}`),
          warnings(window.warnings));
        const pointerPanel = text("section", "panel");
        pointerPanel.append(text("h3", "", `Same-day evidence · ${data.date}`), pointers, text("p", "muted compact", "Only a valid pointer counts as scheduled evidence; recovery context and unverified pointers do not."), warnings(data.pointers?.warnings));
        strip.replaceChildren(gatePanel, windowPanel, pointerPanel);
      } catch (error) {
        strip.replaceChildren(text("div", "error-banner", `Could not load today's evidence strip: ${error.message}`));
      }
    });
  }

  /* Runs. */
  const runsSection = document.querySelector('section.page[data-page="runs"]');
  if (runsSection && runsSection.querySelector("[data-page-body]")) {
    let month = "";
    let lastRuns = "";
    let openDetail = null;

    async function showDetail(id) {
      openDetail = id;
      const host = runsSection.querySelector("[data-run-detail]");
      if (!host) return;
      host.replaceChildren(text("div", "panel empty-state", `Loading ${id}…`));
      try {
        const detail = await fetchJson(`/api/runs/detail?${new URLSearchParams({ run: id })}`, { ttl: 60000 });
        const run = detail.run || {};
        const sections = [
          panel(`Run ${id}`, keyValues([
            ["Stream", run.stream], ["Slot", run.slot], ["Day", run.day], ["Time", run.time], ["Status", badge(run.status)],
            ["Mode", run.mode], ["Origin", badge(run.origin)], ["Root", `${run.root_label}${run.at_risk ? " (at risk)" : ""}`], ["Directory", run.relative_dir],
          ]), run.incomplete ? "incomplete: never wrote run_state.json" : "run_state.json present"),
          warnings(detail.warnings),
        ];
        if (detail.identity && Object.keys(detail.identity).length) sections.push(panel("Identity (allowlisted run_state keys)", auto(detail.identity, "identity")));
        if (detail.contract) sections.push(panel("Artifact contract", auto(detail.contract, "contract"), detail.contract.ok ? "ok" : "violations"));
        if (detail.drift) sections.push(panel("Drift (signals vs orders)", auto(detail.drift, "drift"), detail.drift.drifted === true ? "drifted" : detail.drift.drifted === false ? "no drift" : "unknown"));
        if (detail.post_trade) sections.push(panel("Post-trade", detail.post_trade.available ? auto(detail.post_trade, "post_trade") : text("p", "muted", detail.post_trade.reason || "unavailable")));
        if (detail.close_window) sections.push(panel("Close window: what would have fired (reporting cap 3x)", detail.close_window.available ? auto(detail.close_window, "close_window") : text("p", "muted", detail.close_window.reason || "unavailable")));
        for (const [name, tableData] of Object.entries(detail.tables || {})) {
          const columns = arr(tableData.columns).map((key) => ({ label: key, key }));
          sections.push(panel(name, columns.length ? table(columns, arr(tableData.rows), "no rows") : text("p", "muted", "no allowlisted columns")));
        }
        if (arr(detail.inputs).length) sections.push(panel("Saved inputs (names only)", table([{ label: "File", key: "name" }, { label: "Bytes", key: "bytes", numeric: true }], detail.inputs)));
        if (detail.abort_explanation) sections.push(panel("Why this run aborted", auto(detail.abort_explanation, "abort")));
        host.replaceChildren(...sections);
      } catch (error) {
        host.replaceChildren(text("div", "error-banner", `Could not load the run: ${error.message}`));
      }
    }

    register("runs", async (snapshot) => {
      try {
        const day = snapshot?.daily?.date || "";
        month = month || day.slice(0, 7);
        const [tree, index] = await Promise.all([
          fetchJson("/api/runs/tree", { ttl: 60000 }),
          fetchJson(month ? `/api/runs?${new URLSearchParams({ month })}` : "/api/runs"),
        ]);
        const fingerprint = JSON.stringify([month, tree, index.runs.map((row) => [row.id, row.status]), openDetail]);
        if (fingerprint === lastRuns) return;
        lastRuns = fingerprint;
        const picker = text("div", "filter-group");
        const label = text("label", "", "Month");
        const input = document.createElement("input");
        input.type = "month";
        input.value = month;
        input.addEventListener("change", () => { month = input.value; lastRuns = ""; render("runs", snapshot); });
        label.append(input);
        picker.append(label);
        const rows = index.runs;
        rows.onRow = (row, item) => {
          row.classList.add("clickable-row");
          row.addEventListener("click", () => showDetail(item.id));
        };
        const indexPanel = panel(`Run index · ${month || "all"}`, [
          picker,
          warnings(index.warnings),
          table([
            { label: "Day", key: "day" }, { label: "Time (ET)", get: (row) => row.time ? new Date(row.time).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "numeric", minute: "2-digit" }) : "—" },
            { label: "Stream", key: "stream" }, { label: "Slot", key: "slot" },
            { label: "Status", get: (row) => row.incomplete ? "incomplete" : row.status, badge: true },
            { label: "Mode", key: "mode" }, { label: "Origin", key: "origin", badge: true },
            { label: "Candidates", get: (row) => row.candidates === null ? "unavailable" : row.candidates, numeric: true },
            { label: "Submitted", get: (row) => row.submitted === null ? "unavailable" : row.submitted, numeric: true },
            { label: "Root", get: (row) => `${row.root_label}${row.at_risk ? " (at risk)" : ""}` },
          ], rows, "No runs were found for this month under the observed roots."),
        ], `${rows.length} runs`);
        const treeCards = tree.roots.map((root) => panel(`${root.kind} root · ${root.label}`, [
          root.at_risk ? text("p", "notice", "Gitignored tree, not managed by dca_paths: artifacts here are at risk of being lost.") : null,
          table([
            { label: "Directory", key: "name" }, { label: "Present", get: (row) => row.present ? "present" : "absent", badge: true },
            { label: "Entries", get: (row) => row.present ? row.entries : "—", numeric: true }, { label: "Newest", key: "newest" },
          ], root.directories, "nothing probed"),
        ], root.exists ? "exists" : "missing"));
        const detailHost = text("div", "");
        detailHost.dataset.runDetail = "";
        detailHost.append(text("div", "panel empty-state", "Select a run to open its detail."));
        mount(runsSection, warnings(tree.warnings), indexPanel, detailHost, ...treeCards);
        if (openDetail) showDetail(openDetail);
      } catch (error) {
        failure(runsSection, error);
      }
    });
  }

  /* Schedule. */
  const scheduleSection = document.querySelector('section.page[data-page="schedule"]');
  if (scheduleSection && scheduleSection.querySelector("[data-page-body]")) {
    let lastSchedule = "";
    const dot = (value, title) => {
      const node = text("span", `matrix-dot${value === true ? " on" : value === null || value === undefined ? " na" : ""}`);
      node.title = title || (value === true ? "yes" : value === false ? "no" : "layer unavailable");
      node.setAttribute("aria-label", node.title);
      return node;
    };
    register("schedule", async (snapshot) => {
      try {
        const data = await fetchJson(`/api/schedule${dateParam(snapshot)}`, { ttl: 30000 });
        const fingerprint = JSON.stringify(data);
        if (fingerprint === lastSchedule) return;
        lastSchedule = fingerprint;
        const matrix = panel("Configured vs deployable vs observed", [
          text("p", "muted", "Configured: the manifest names this slot. Deployable: deploy/ cron or systemd mentions it. Observed: a run_state.json exists for the day. Scheduled evidence: the same-day pointer is valid. Slot verified: the slot's own dated run_state.json was finalized as scheduled evidence (python -m scripts.verify_schedule), which proves it fired, not that any order filled."),
          table([
            { label: "Time", key: "time_et" }, { label: "Stream", key: "stream" },
            { label: "Configured", get: (row) => dot(row.configured, "manifest task present") },
            { label: "Deployable", get: (row) => dot(row.deployable, "cron/systemd entry present") },
            { label: "Observed", get: (row) => dot(row.observed, row.observed_note || "run_state.json present today") },
            { label: "Scheduled evidence", get: (row) => dot(row.scheduled_evidence, "same-day pointer valid") },
            { label: "Slot verified", key: "slot_evidence", badge: true },
            { label: "Why", get: (row) => arr(row.slot_evidence_reasons).join("; ") },
            { label: "Manifest tasks", get: (row) => arr(row.manifest_task_ids).join(", ") }, { label: "Note", key: "observed_note" },
          ], arr(data.matrix), "No roster slots."),
        ], "per roster slot");
        const tasks = panel("Manifest tasks (config/scheduled_tasks.json)", [
          warnings(data.manifest?.warnings),
          table([
            { label: "Time (ET)", key: "time" }, { label: "Task", key: "name" }, { label: "Type", key: "type" },
            { label: "Trades", get: (row) => row.trades }, { label: "Status today", key: "status", badge: true },
            { label: "Manifest status", key: "manifest_status", badge: true }, { label: "Registered", get: (row) => row.scheduler_registered === null ? "unknown" : row.scheduler_registered ? "yes" : "not registered", badge: true },
            { label: "Cron (UTC)", key: "cron_utc" }, { label: "Next", key: "next_at" }, { label: "Id", key: "id" },
          ], arr(data.manifest?.tasks), "No tasks in the manifest."),
          text("p", "panel-footnote", `Policy blocks in the manifest: ${arr(data.manifest?.policy_keys).length}. Select one below to view it.`),
        ], `${arr(data.manifest?.tasks).length} tasks`);
        const policySelect = document.createElement("select");
        for (const key of arr(data.manifest?.policy_keys)) { const option = text("option", "", key); option.value = key; policySelect.append(option); }
        const policyView = text("pre", "subtle-json", "Select a policy block.");
        policySelect.addEventListener("change", async () => {
          policyView.textContent = "Loading…";
          try { const block = await fetchJson(`/api/schedule/policy?${new URLSearchParams({ key: policySelect.value })}`, { ttl: 60000 }); policyView.textContent = JSON.stringify(block.value, null, 2); }
          catch (error) { policyView.textContent = error.message; }
        });
        const policyPanel = panel("Manifest policy blocks", [policySelect, policyView]);
        const deploy = panel("Deployable layer: deploy/ cron and systemd", [
          warnings(data.deploy?.warnings),
          table([{ label: "Schedule", key: "schedule" }, { label: "TZ", key: "timezone" }, { label: "Script", key: "script" }, { label: "Arguments", get: (row) => arr(row.arguments).join(" ") }, { label: "Slots", get: (row) => arr(row.slots).join(", ") }], arr(data.deploy?.cron), "No crontab entries."),
          table([{ label: "Unit", key: "file" }, { label: "Kind", key: "kind" }, { label: "Description", key: "description" }, { label: "User", key: "user" }, { label: "Env file", key: "environment_file" }, { label: "Script", key: "script" }, { label: "Slots", get: (row) => arr(row.slots).join(", ") }], arr(data.deploy?.systemd), "No systemd units."),
        ], "backup drift checks");
        const workflows = panel("Deployable layer: GitHub workflows", table([{ label: "Workflow", key: "name" }, { label: "File", key: "file" }, { label: "Triggers", get: (row) => arr(row.triggers).join(", ") }, { label: "Uses secrets", get: (row) => row.has_secrets_reference }], arr(data.workflows?.workflows), "No workflows."), "CI, self-heal, drift self-test");
        const launchd = panel("Deployable layer: launchd console service", [
          keyValues([["Plist present", data.launchd?.plist_present], ["Plist modified", data.launchd?.plist_modified], ["Log root", data.launchd?.log_root]]),
          ...arr(data.launchd?.logs).map((log) => { const pre = text("pre", "subtle-json", arr(log.tail).join("\n") || "(empty)"); const block = text("div", ""); block.append(text("h3", "auto-title", `${log.name} (${show(log.size)} bytes, last lines)`), pre); return block; }),
          warnings(data.launchd?.warnings),
        ]);
        const roster = panel("Roster identity", table([
          { label: "Slot", key: "slot_id" }, { label: "Time", key: "time_et" }, { label: "Stream", key: "stream" }, { label: "Executor", key: "executor_class" },
          { label: "Cron id", key: "cron_id" }, { label: "Live execution", get: (row) => row.live_execution_enabled === null ? "unknown" : row.live_execution_enabled ? "enabled" : "disabled", badge: true },
          { label: "run_dca entry", key: "run_dca_entry", badge: true }, { label: "Artifact dir", key: "artifact_dir" }, { label: "Pointer", key: "pointer_name" }, { label: "Config", key: "config_file" },
        ], arr(data.roster?.slots), "No roster."), "from the executor profiles");
        mount(scheduleSection, warnings(data.warnings), matrix, tasks, policyPanel, roster, deploy, workflows, launchd);
      } catch (error) {
        failure(scheduleSection, error);
      }
    });
  }

  /* State & ledgers. */
  const stateSection = document.querySelector('section.page[data-page="state"]');
  if (stateSection && stateSection.querySelector("[data-page-body]")) {
    let lastState = "";
    register("state", async (snapshot) => {
      try {
        const data = await fetchJson(`/api/state${dateParam(snapshot)}`);
        const fingerprint = JSON.stringify(data);
        if (fingerprint === lastState) return;
        lastState = fingerprint;
        const stateCard = panel("Strategy state (durable budget ledger)", [
          keyValues([["Status", badge(data.strategy_state?.status)], ["File", data.strategy_state?.path_label], ["Modified", data.strategy_state?.modified], ["Lock file", data.strategy_state?.lock_file_present ? "present (never acquired by the console)" : "absent"]]),
          Object.keys(data.strategy_state?.fields || {}).length ? auto(data.strategy_state.fields, "fields") : text("p", "muted", "No fields: absent means a fresh state; corrupt means the live loader would refuse to run."),
          warnings(data.strategy_state?.warnings),
        ], data.strategy_state?.status === "corrupt" ? "ALARM" : "read-only");
        const ledgerRows = Object.entries(data.ledgers || {}).map(([kind, summary]) => ({ kind, ...summary }));
        const ledgerHost = text("div", "");
        const ledgerPanel = panel("Accumulation ledgers", [
          table([
            { label: "Ledger", key: "kind" }, { label: "Entries", key: "entries", numeric: true }, { label: "Listed", key: "listed", numeric: true },
            { label: "Corrupt", key: "corrupt", numeric: true }, { label: "Lock files present", key: "lock_files_present", numeric: true },
            { label: "Pending USD", get: (row) => row.pending_total_usd === null ? "unavailable" : row.pending_total_usd, numeric: true },
            { label: "", get: (row) => { const button = text("button", "button ghost", "Open"); button.type = "button"; button.addEventListener("click", async () => { ledgerHost.replaceChildren(text("div", "panel empty-state", "Loading…")); try { const view = await fetchJson(`/api/state/ledger?${new URLSearchParams({ kind: row.kind })}`, { ttl: 30000 }); ledgerHost.replaceChildren(panel(`${row.kind} ledger`, auto(view, row.kind))); } catch (error) { ledgerHost.replaceChildren(text("div", "error-banner", error.message)); } }); return button; } },
          ], ledgerRows, "No ledgers."),
          text("p", "muted", "Read directly from ledger.json / tracker.json through the bounded reader; the ledger classes (which lock even on read) are never instantiated here."),
          ledgerHost,
        ]);
        const risk = data.risk || {};
        const riskPanel = panel("Risk report and kill switch", [
          keyValues([["Usable", badge(risk.usable ? "usable" : "evidence incomplete")], ["Scope", risk.scope?.note], ["Root", risk.scope?.root_label]]),
          risk.evidence ? keyValues(Object.entries(risk.evidence).map(([key, value]) => [key.replaceAll("_", " "), value])) : text("p", "muted", "No evidence block."),
          risk.report ? auto(risk.report, "report") : null,
          warnings(risk.warnings),
        ], risk.usable ? "evidence complete" : "not usable");
        const purchasesHost = text("div", "");
        const purchasesButton = text("button", "button ghost", "Load confirmed purchases");
        purchasesButton.type = "button";
        purchasesButton.addEventListener("click", async () => { purchasesHost.replaceChildren(text("div", "panel empty-state", "Loading…")); try { const view = await fetchJson("/api/state/purchases", { ttl: 30000 }); purchasesHost.replaceChildren(auto(view, "purchases")); } catch (error) { purchasesHost.replaceChildren(text("div", "error-banner", error.message)); } });
        const purchasesPanel = panel("Confirmed purchases", [keyValues([["Records", data.purchases?.record_count], ["Total USD", data.purchases?.total_usd], ["Last trading day", data.purchases?.last_trading_day]]), purchasesButton, purchasesHost], "confirmed fills only");
        const maintenancePanel = panel("Maintenance events", auto(data.maintenance || {}, "maintenance"), "archive updates and calibration");
        mount(stateSection, warnings(data.warnings), stateCard, riskPanel, ledgerPanel, purchasesPanel, maintenancePanel);
      } catch (error) {
        failure(stateSection, error);
      }
    });
  }

  /* Config. */
  const configSection = document.querySelector('section.page[data-page="config"]');
  if (configSection && configSection.querySelector("[data-page-body]")) {
    let lastConfig = "";
    register("config", async (snapshot) => {
      try {
        const data = await fetchJson(`/api/config${dateParam(snapshot)}`, { ttl: 60000 });
        const fingerprint = JSON.stringify(data);
        if (fingerprint === lastConfig) return;
        lastConfig = fingerprint;
        const viewer = text("pre", "subtle-json", "Select a config file to view it (read-only).");
        const files = arr(data.catalog?.files);
        files.onRow = (row, item) => {
          row.classList.add("clickable-row");
          row.addEventListener("click", async () => {
            viewer.textContent = `Loading ${item.stem}…`;
            try { const view = await fetchJson(`/api/config/file?${new URLSearchParams({ stem: item.stem })}`, { ttl: 60000 }); viewer.textContent = (view.text || "") + (view.truncated ? "\n… truncated at 200 KB" : ""); }
            catch (error) { viewer.textContent = error.message; }
          });
        };
        const catalog = panel("Config catalog", [
          warnings(data.catalog?.warnings),
          table([
            { label: "File", get: (row) => `${row.stem}.json` }, { label: "Exists", get: (row) => row.exists }, { label: "Size", key: "size", numeric: true },
            { label: "Parses", get: (row) => row.parse_ok === null ? "absent" : row.parse_ok ? "ok" : "rejected", badge: true },
            { label: "Schema", get: (row) => row.schema_stem ? (row.schema_exists ? row.schema_stem : "schema missing") : "no schema" },
            { label: "Loader", key: "loader" }, { label: "Fail mode", key: "fail_mode", badge: true },
            { label: "Publisher-only", get: (row) => row.publisher_only ? "publisher-only" : "" },
          ], files, "No config files."),
          viewer,
        ], `${files.length} files`);
        const parity = panel("Parity checks (config vs constants)", [
          table([{ label: "Check", key: "name" }, { label: "Result", get: (row) => row.ok === null ? "unknown" : row.ok ? "ok" : "mismatch", badge: true }, { label: "Detail", key: "detail" }], arr(data.parity?.checks), "No checks."),
          text("p", "panel-footnote", data.parity?.note || ""),
          warnings(data.parity?.warnings),
        ]);
        const streams = panel("Executor streams and authorization", [
          table([
            { label: "Stream", key: "stream" }, { label: "File", key: "file" }, { label: "Found", get: (row) => row.found },
            { label: "Live execution", get: (row) => row.live_execution_enabled === null ? "unknown" : row.live_execution_enabled ? "enabled" : "disabled", badge: true },
            { label: "Slots", get: (row) => arr(row.slots).join(", ") }, { label: "Trend bars", key: "trend_min_bars" }, { label: "Note", key: "note" },
          ], arr(data.streams?.streams), "No stream files."),
          data.streams?.run_classification ? auto(data.streams.run_classification, "run_classification") : null,
        ]);
        const sizing = panel("Sizing adjustment sources (publisher-only)", auto(data.sizing || {}, "sizing"), "never edited here");
        const gates = data.gates ? panel("Live-money gates", auto(data.gates, "gates")) : null;
        const windowPanel = data.window ? panel("Approved trading window", auto(data.window, "window")) : null;
        const ladderHost = text("div", "");
        const ladderButton = text("button", "button ghost", "Load ladder preview");
        ladderButton.type = "button";
        ladderButton.addEventListener("click", async () => { ladderHost.replaceChildren(text("div", "panel empty-state", "Loading…")); try { const view = await fetchJson("/api/config/ladder", { ttl: 60000 }); ladderHost.replaceChildren(auto(view, "ladder")); } catch (error) { ladderHost.replaceChildren(text("div", "error-banner", error.message)); } });
        const effectiveHost = text("div", "");
        const effectiveButton = text("button", "button ghost", "Load effective StrategyConfig");
        effectiveButton.type = "button";
        effectiveButton.addEventListener("click", async () => { effectiveHost.replaceChildren(text("div", "panel empty-state", "Loading…")); try { const view = await fetchJson("/api/config/effective", { ttl: 60000 }); effectiveHost.replaceChildren(auto(view, "effective")); } catch (error) { effectiveHost.replaceChildren(text("div", "error-banner", error.message)); } });
        const deep = panel("Ladder and effective configuration", [ladderButton, ladderHost, effectiveButton, effectiveHost], "loaded on demand");
        mount(configSection, warnings(data.warnings), catalog, parity, streams, sizing, gates, windowPanel, deep);
      } catch (error) {
        failure(configSection, error);
      }
    });
  }

  /* Alerts & health. */
  const alertsSection = document.querySelector('section.page[data-page="alerts"]');
  if (alertsSection && alertsSection.querySelector("[data-page-body]")) {
    let lastAlerts = "";
    register("alerts", async () => {
      try {
        const data = await fetchJson("/api/alerts", { ttl: 30000 });
        const fingerprint = JSON.stringify(data);
        if (fingerprint === lastAlerts) return;
        lastAlerts = fingerprint;
        const parts = [warnings(data.warnings)];
        const order = ["inbox", "digest", "attempt_log", "channels", "drift", "self_heal", "engines", "model_usage", "usage"];
        const keys = Object.keys(data).filter((key) => key !== "warnings");
        keys.sort((a, b) => (order.indexOf(a) === -1 ? 99 : order.indexOf(a)) - (order.indexOf(b) === -1 ? 99 : order.indexOf(b)));
        for (const key of keys) {
          const value = data[key];
          if (value === null || typeof value !== "object") continue;
          parts.push(panel(key.replaceAll("_", " "), auto(value, key)));
        }
        mount(alertsSection, ...parts);
      } catch (error) {
        failure(alertsSection, error);
      }
    });
  }

  /* Reports. */
  const reportsSection = document.querySelector('section.page[data-page="reports"]');
  if (reportsSection && reportsSection.querySelector("[data-page-body]")) {
    let lastReports = "";
    const markdownInto = (host, markdown, empty) => {
      host.replaceChildren(markdown && window.ConsoleMarkdown ? window.ConsoleMarkdown.render(markdown) : text("p", "muted", empty || "Nothing to render."));
    };
    register("reports", async (snapshot) => {
      try {
        const data = await fetchJson(`/api/reports${dateParam(snapshot)}`, { ttl: 30000 });
        const fingerprint = JSON.stringify(data);
        if (fingerprint === lastReports) return;
        lastReports = fingerprint;
        const weeklyHost = text("div", "");
        const weeklyButton = text("button", "button ghost", "Render weekly dashboard");
        weeklyButton.type = "button";
        weeklyButton.addEventListener("click", async () => { weeklyHost.replaceChildren(text("div", "panel empty-state", "Building…")); try { const view = await fetchJson(`/api/reports/weekly${dateParam(snapshot)}`, { ttl: 60000 }); weeklyHost.replaceChildren(warnings(view.warnings)); markdownInto(weeklyHost.appendChild(text("div", "")), view.markdown, "No scheduled runs in this week."); } catch (error) { weeklyHost.replaceChildren(text("div", "error-banner", error.message)); } });
        const weekly = panel("Weekly performance dashboard", [
          keyValues([["Week", data.weekly?.week], ["Scheduled runs", data.weekly?.run_count], ["Scope", data.weekly?.scope || "scheduled-live runs only"],
            ["Sources", arr(data.weekly?.sources).map((source) => `${source.root_label}: ${source.runs}${source.at_risk ? " (at risk)" : ""}`).join(" · ") || "none"]]),
          warnings(data.weekly?.warnings), weeklyButton, weeklyHost,
        ], "markdown, rendered as text");
        const costHost = text("div", "");
        const costButton = text("button", "button ghost", "Render cost tracker");
        costButton.type = "button";
        costButton.addEventListener("click", async () => { costHost.replaceChildren(text("div", "panel empty-state", "Building…")); try { const view = await fetchJson(`/api/reports/cost?${new URLSearchParams({ month: data.cost?.month || "" })}`, { ttl: 60000 }); costHost.replaceChildren(warnings(view.warnings)); markdownInto(costHost.appendChild(text("div", "")), view.markdown, "No runs in this month."); } catch (error) { costHost.replaceChildren(text("div", "error-banner", error.message)); } });
        const cost = panel("Monthly cost tracker", [
          keyValues([["Month", data.cost?.month], ["Runs", data.cost?.run_count], ["Root", data.cost?.root_label]]),
          text("p", "panel-footnote", data.cost?.disclaimer || ""), warnings(data.cost?.warnings), costButton, costHost,
        ], "proxy units");
        const latestHost = text("div", "");
        const latestButton = text("button", "button ghost", "Load latest run summary");
        latestButton.type = "button";
        latestButton.addEventListener("click", async () => { latestHost.replaceChildren(text("div", "panel empty-state", "Loading…")); try { const view = await fetchJson("/api/reports/latest", { ttl: 60000 }); latestHost.replaceChildren(warnings(view.warnings), view.metrics ? auto(view.metrics, "metrics") : text("p", "muted", "no metrics")); markdownInto(latestHost.appendChild(text("div", "")), view.markdown, ""); } catch (error) { latestHost.replaceChildren(text("div", "error-banner", error.message)); } });
        const latest = panel("Latest run summary and Notion preview", [
          keyValues([["Found", data.latest?.found], ["Trading day", data.latest?.trading_day], ["Root", data.latest?.root_label], ["File", data.latest?.relative_path]]),
          warnings(data.latest?.warnings), data.latest?.found ? latestButton : null, latestHost,
        ], "page_id stays null; the push is external");
        const digestHost = text("div", "");
        const digestRows = arr(data.digests?.days);
        digestRows.onRow = (row, item) => {
          row.classList.add("clickable-row");
          row.addEventListener("click", async () => { digestHost.replaceChildren(text("div", "panel empty-state", "Loading…")); try { const view = await fetchJson(`/api/reports/summary?${new URLSearchParams({ date: item.day })}`, { ttl: 60000 }); digestHost.replaceChildren(warnings(view.warnings)); markdownInto(digestHost.appendChild(text("div", "")), view.markdown, "No digest for this day."); } catch (error) { digestHost.replaceChildren(text("div", "error-banner", error.message)); } });
        };
        const digests = panel("Console daily digests", [
          keyValues([["Location", data.digests?.location], ["Days", data.digests?.count]]),
          table([{ label: "Day", key: "day" }, { label: "JSON", get: (row) => row.json }, { label: "Markdown", get: (row) => row.md }], digestRows, "No digests saved yet."),
          digestHost,
        ]);
        const averageCost = panel("Average-cost snapshot", data.average_cost?.found ? auto(data.average_cost, "average_cost") : [text("p", "muted", "No average-cost snapshot exists under the observed roots; it is built by an external session from fetched positions."), warnings(data.average_cost?.warnings)], "accounts shown as digests");
        mount(reportsSection, text("p", "muted", data.boundary || ""), weekly, cost, latest, digests, averageCost);
      } catch (error) {
        failure(reportsSection, error);
      }
    });
  }

  /* Research: the Octopus run catalog, research launches and the analysis archive,
     beneath the existing forecast panels. */
  const researchExtra = document.querySelector("[data-research-extra]");
  if (researchExtra) {
    let lastResearch = "";
    register("research", async () => {
      try {
        const data = await fetchJson("/api/research", { ttl: 30000 });
        const fingerprint = JSON.stringify(data);
        if (fingerprint === lastResearch) return;
        lastResearch = fingerprint;
        const artifactLink = (job, item) => {
          const link = text("a", "", `${item.name} (${item.kind})`);
          link.href = `/api/artifact?${new URLSearchParams({ job, name: item.name })}`;
          link.target = "_blank";
          link.rel = "noopener";
          return link;
        };
        const launchRows = arr(data.launches).map((entry) => {
          const links = text("div", "artifact-links");
          for (const item of entry.artifacts || []) links.append(artifactLink(entry.job, item));
          if (!(entry.artifacts || []).length) links.append(text("span", "muted", "no artifact"));
          return { label: entry.label || entry.action, when: entry.written_at, artifacts: links };
        });
        researchExtra.replaceChildren(
          text("p", "muted", data.boundary || ""),
          panel("Octopus run catalog", [
            table([
              { label: "Run", key: "name" }, { label: "Location", key: "location" }, { label: "Demo", get: (row) => row.demo },
              { label: "run_meta.json", get: (row) => row.run_meta_present ? "present" : "missing", badge: true },
              { label: "Updatable", get: (row) => row.updatable ? "yes" : "no (daily_update refuses)", badge: true },
              { label: "Model", key: "model_selection" }, { label: "Tickers", key: "ticker_count", numeric: true },
              { label: "Last processed", key: "last_processed_date" }, { label: "Report", get: (row) => row.report_present }, { label: "Modified", key: "modified" },
            ], arr(data.octopus?.runs), "No Octopus run directories were found."),
            text("p", "panel-footnote", data.octopus?.note || ""), warnings(data.octopus?.warnings),
          ], `${data.octopus?.count ?? 0} runs`),
          panel("Generated research pages and dry-run artifacts", table([
            { label: "Launcher", key: "label" }, { label: "Written", key: "when" }, { label: "Artifacts", key: "artifacts" },
          ], launchRows, "Start the market graph, the Octopus inspector or the ml-forecaster dry run from the Workflows page."), "sandboxed pages"),
          panel("Analysis archive", [
            table([
              { label: "Entry", key: "name" }, { label: "README", get: (row) => row.readme }, { label: "Entries", key: "entries", numeric: true },
              { label: "Ticker advisory", get: (row) => row.ticker_advisory }, { label: "Outputs", get: (row) => arr(row.outputs).join(", ") }, { label: "Modified", key: "modified" },
            ], arr(data.analysis?.entries), "analysis/ is empty."),
            text("p", "panel-footnote", data.analysis?.note || ""), warnings(data.analysis?.warnings),
          ], `${data.analysis?.count ?? 0} entries`),
        );
      } catch (error) {
        researchExtra.replaceChildren(text("div", "error-banner", `Could not load the research catalog: ${error.message}`));
      }
    });
  }

  /* Tickers: Active universe, data freshness, whole-share accumulation, exclusions */
  const tickersSection = document.querySelector('section.page[data-page="tickers"]');
  if (tickersSection) {
    let lastTickers = "";
    register("tickers", async (snapshot) => {
      try {
        const data = await fetchJson("/api/tickers", { ttl: 20000 });
        const jobs = Array.isArray(snapshot?.jobs) ? snapshot.jobs : [];
        const running = jobs.some((job) => job.action === "ticker-refresh-missing" && ["running", "queued"].includes(job.status));
        const fingerprint = JSON.stringify([data, running]);
        if (fingerprint === lastTickers && tickersSection.querySelector(".metrics-grid")) return;
        lastTickers = fingerprint;

        const metrics = text("section", "metrics-grid");
        const m1 = text("article", "metric");
        m1.append(text("div", "metric-label", "Active symbols"), text("div", "metric-value", String(data.summary?.total_active ?? 0)), text("p", "", "Targeted for daily graded DCA"));
        const m2 = text("article", "metric");
        m2.append(text("div", "metric-label", "Data up to date"), text("div", "metric-value green", String(data.summary?.up_to_date_count ?? 0)), text("p", "", "Recent bars in data archive"));
        const m3 = text("article", "metric");
        const needUpdate = (data.summary?.missing_count ?? 0) + (data.summary?.stale_count ?? 0);
        m3.append(text("div", "metric-label", "Missing / Stale"), text("div", `metric-value ${needUpdate ? "amber" : "green"}`, String(needUpdate)), text("p", "", "Need chart feed restore"));
        const m4 = text("article", "metric");
        m4.append(text("div", "metric-label", "Whole-share / Excluded"), text("div", "metric-value", `${data.summary?.total_whole_share ?? 0} / ${data.summary?.total_excluded ?? 0}`), text("p", "", "Special accumulation / blocked"));
        metrics.append(m1, m2, m3, m4);

        const actionStrip = text("div", "panel");
        const actionTop = text("div", "section-top");
        const actionLeft = text("div", "");
        actionLeft.append(text("h3", "", "Ticker Archive & Historical Bars"), text("p", "muted compact", `Archive: ${data.archive?.label || "ticker_data"} · Status: ${data.archive?.available ? "Connected" : "Not connected"}`));
        actionTop.append(actionLeft);

        const refreshBtn = text("button", "button primary small", running ? "↻ Updating Tickers..." : "↻ Update Ticker Histories");
        refreshBtn.type = "button";
        refreshBtn.disabled = running;
        refreshBtn.addEventListener("click", () => {
          refreshBtn.disabled = true;
          refreshBtn.textContent = "↻ Starting update...";
          if (typeof window.startJob === "function") window.startJob("ticker-refresh-missing");
        });
        actionTop.append(refreshBtn);
        actionStrip.append(actionTop);

        if (running) {
          actionStrip.append(text("div", "notice", "Ticker gathering process in progress: fetching latest daily bars from Yahoo chart feed..."));
        }

        if (Array.isArray(data.archive?.recent_logs) && data.archive.recent_logs.length) {
          const logBlock = text("div", "subtle-json");
          logBlock.textContent = data.archive.recent_logs.join("\n");
          actionStrip.append(text("p", "muted compact", "Recent archive update log:"), logBlock);
        }

        const tablePanel = text("section", "panel section-space");
        const tableTop = text("div", "section-top");
        const tableHeading = text("div", "");
        tableHeading.append(text("h3", "", "Ticker Universe & Bar Freshness"), text("p", "muted compact", "Daily bars in local ticker archive, evaluated against current trading calendar."));
        tableTop.append(tableHeading);

        const filterGroup = text("div", "filter-group");
        const searchInput = text("input", "");
        searchInput.type = "search";
        searchInput.placeholder = "Filter symbol...";
        searchInput.setAttribute("aria-label", "Filter by ticker symbol");

        const categorySelect = text("select", "");
        categorySelect.append(new Option("All categories", "all"), new Option("Active only", "active"), new Option("Whole-share only", "whole_share"), new Option("Excluded only", "excluded"), new Option("Missing history only", "missing"));

        filterGroup.append(searchInput, categorySelect);
        tableTop.append(filterGroup);
        tablePanel.append(tableTop);

        const tableContainer = text("div", "");
        function renderRows() {
          const q = searchInput.value.trim().toUpperCase();
          const cat = categorySelect.value;
          const filtered = (data.items || []).filter(item => {
            if (q && !item.symbol.includes(q)) return false;
            if (cat === "all") return true;
            if (cat === "missing") return item.freshness === "missing";
            return item.category === cat;
          });

          const tableEl = table([
            { label: "Ticker", key: "symbol", get: (r) => r.symbol },
            { label: "Category", key: "category", badge: true },
            { label: "Data Freshness", key: "freshness", badge: true },
            { label: "Latest Bar Date", key: "last_date" },
            { label: "Bars (est.)", key: "total_bars", numeric: true },
            { label: "File Size", get: (r) => r.size_bytes ? `${Math.round(r.size_bytes / 1024)} KB` : "0" , numeric: true },
            { label: "Notes / Routing", key: "note" },
          ], filtered, "No tickers match the selected filter.");
          tableContainer.replaceChildren(tableEl);
        }

        searchInput.addEventListener("input", renderRows);
        categorySelect.addEventListener("change", renderRows);
        renderRows();
        tablePanel.append(tableContainer);

        const rulesPanel = text("section", "panel section-space");
        rulesPanel.append(
          text("h3", "", "Universe Rules & Whole-Share Accumulation"),
          text("p", "muted compact", "Summary of ticker exclusions, whole-share routing, and symbol aliases in config/tickers.json:"),
          keyValues([
            ["Version", data.universe?.version || "current"],
            ["Active count", `${data.summary?.total_active ?? 0} symbols`],
            ["Whole-share symbols", (data.universe?.whole_share || []).join(", ") || "none"],
            ["Excluded symbols", (data.universe?.excluded || []).join(", ") || "none"],
            ["Symbol corrections", Object.entries(data.universe?.symbol_corrections || {}).map(([k, v]) => `${k} -> ${v}`).join(", ") || "none"],
          ]),
          warnings(data.warnings)
        );

        mount(tickersSection, metrics, actionStrip, tablePanel, rulesPanel);
      } catch (error) {
        failure(tickersSection, error);
      }
    });
  }

  /* Training: Neural foundation, ml-forecaster, loss curves, checkpoints, and hardware diagnostics */
  const trainingSection = document.querySelector('section.page[data-page="training"]');
  if (trainingSection) {
    let lastTraining = "";
    register("training", async () => {
      try {
        const data = await fetchJson("/api/training", { ttl: 20000 });
        const fingerprint = JSON.stringify(data);
        if (fingerprint === lastTraining && trainingSection.querySelector(".actions-grid")) return;
        lastTraining = fingerprint;

        const hw = data.hardware || {};
        const hwPanel = text("div", "panel");
        const hwTop = text("div", "section-top");
        const hwLeft = text("div", "");
        hwLeft.append(text("h3", "", "Compute Backend & Accelerators"), text("p", "muted compact", `${hw.device_label || "CPU"} · PyTorch: ${hw.torch_version || "Not installed"} · scikit-learn: ${hw.sklearn_version || "Not installed"}`));
        hwTop.append(hwLeft);
        hwTop.append(badge(hw.torch_available ? "ready" : "warning"));
        hwPanel.append(hwTop);

        const kv = keyValues([
          ["Accelerator device", hw.device_label || "CPU"],
          ["PyTorch Engine", hw.torch_available ? `Installed (${hw.torch_version})` : "Missing"],
          ["scikit-learn", hw.sklearn_available ? `Installed (${hw.sklearn_version})` : "Missing"],
          ["Parameters (Octopus trunk)", show(data.loss_curves?.parameters || "1,042,839")],
          ["Feature dimension", `${data.loss_curves?.feature_count || 36} multi-task features`],
          ["Panel total windows", show(data.loss_curves?.panel_windows_total || "6.4M")],
        ]);
        hwPanel.append(kv);

        const launchSection = text("section", "section-space");
        const launchHeading = text("div", "section-heading");
        const lHDiv = text("div", "");
        lHDiv.append(text("div", "eyebrow", "INTERACTIVE LAUNCHPAD"), text("h2", "", "Train Models & Evaluate Sizing"), text("p", "muted", "Launch isolated training passes or walk-forward evaluations. Artifacts land in dedicated job directories."));
        launchHeading.append(lHDiv);
        launchSection.append(launchHeading);

        const cardGrid = text("div", "actions-grid");
        for (const model of data.models || []) {
          const card = text("article", "action-card");
          const cHeader = text("div", "action-header");
          const icon = text("div", "action-icon", "⚡");
          const cTitle = text("div", "");
          cTitle.append(text("h3", "", model.name), text("span", "tag", model.tag));
          cHeader.append(icon, cTitle);

          const desc = text("p", "action-description", model.description);
          const bottom = text("div", "action-bottom");
          const btn = text("button", "button primary small", model.action_label || "Launch");
          btn.type = "button";
          btn.disabled = !model.ready;
          btn.addEventListener("click", () => {
            if (typeof window.startJob === "function") window.startJob(model.action_id);
          });
          bottom.append(badge(model.ready ? "ready" : "blocked"), btn);

          card.append(cHeader, desc);
          if (model.reason) {
            card.append(text("p", "action-reason", model.reason));
          }
          card.append(bottom);
          cardGrid.append(card);
        }
        launchSection.append(cardGrid);

        const lossPanel = text("section", "panel section-space");
        const lossTop = text("div", "section-top");
        const lossTitle = text("div", "");
        lossTitle.append(text("h3", "", "Neural Training Loss Curves"), text("p", "muted compact", "Curriculum Stage 1 & Stage 2 loss progression across total, forward, reconstruction, and market regime losses."));
        lossTop.append(lossTitle);

        const legend = text("div", "legend");
        legend.append(
          text("span", "tag", "● Total Loss (Green)"),
          text("span", "tag", "● Recon Loss (Gray)"),
          text("span", "tag", "● Forward Loss (Blue)"),
          text("span", "tag", "● Market Loss (Amber)")
        );
        lossTop.append(legend);
        lossPanel.append(lossTop);

        const epochs = data.loss_curves?.epochs || [];
        if (epochs.length > 0) {
          const svgWrap = text("div", "loss-chart-wrapper");
          const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
          svg.setAttribute("viewBox", "0 0 760 260");
          svg.setAttribute("class", "loss-svg");

          const padding = { top: 20, right: 30, bottom: 35, left: 55 };
          const chartW = 760 - padding.left - padding.right;
          const chartH = 260 - padding.top - padding.bottom;

          const maxLoss = Math.max(...epochs.map(e => Math.max(e.total_loss || 0, e.recon_loss || 0, e.forward_loss || 0)), 0.1);
          const minLoss = 0;
          const n = epochs.length;
          const xStep = n > 1 ? chartW / (n - 1) : chartW / 2;

          function getCoords(key) {
            return epochs.map((e, i) => {
              const val = e[key] || 0;
              const x = padding.left + i * xStep;
              const y = padding.top + chartH - ((val - minLoss) / (maxLoss - minLoss)) * chartH;
              return `${x.toFixed(1)},${y.toFixed(1)}`;
            });
          }

          for (let step = 0; step <= 4; step++) {
            const yVal = minLoss + (maxLoss - minLoss) * (step / 4);
            const yPos = padding.top + chartH - (step / 4) * chartH;
            const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
            line.setAttribute("x1", padding.left);
            line.setAttribute("x2", padding.left + chartW);
            line.setAttribute("y1", yPos);
            line.setAttribute("y2", yPos);
            line.setAttribute("stroke", "#e2e8e4");
            line.setAttribute("stroke-dasharray", "3 3");
            svg.append(line);

            const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
            label.setAttribute("x", padding.left - 8);
            label.setAttribute("y", yPos + 3);
            label.setAttribute("text-anchor", "end");
            label.setAttribute("font-size", "9");
            label.setAttribute("fill", "#73827c");
            label.textContent = yVal.toFixed(3);
            svg.append(label);
          }

          const series = [
            { key: "total_loss", color: "#1c7157", width: "2.5" },
            { key: "recon_loss", color: "#6b7280", width: "1.5" },
            { key: "forward_loss", color: "#2563eb", width: "1.5" },
            { key: "market_loss", color: "#a4661b", width: "1.5" }
          ];

          for (const s of series) {
            const pts = getCoords(s.key);
            const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
            polyline.setAttribute("fill", "none");
            polyline.setAttribute("stroke", s.color);
            polyline.setAttribute("stroke-width", s.width);
            polyline.setAttribute("points", pts.join(" "));
            svg.append(polyline);

            for (const pt of pts) {
              const [px, py] = pt.split(",");
              const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
              dot.setAttribute("cx", px);
              dot.setAttribute("cy", py);
              dot.setAttribute("r", "3.5");
              dot.setAttribute("fill", s.color);
              svg.append(dot);
            }
          }

          epochs.forEach((e, i) => {
            const x = padding.left + i * xStep;
            const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
            label.setAttribute("x", x);
            label.setAttribute("y", padding.top + chartH + 18);
            label.setAttribute("text-anchor", "middle");
            label.setAttribute("font-size", "9");
            label.setAttribute("fill", "#73827c");
            label.textContent = `Ep ${e.epoch}`;
            svg.append(label);
          });

          svgWrap.append(svg);
          lossPanel.append(svgWrap);

          const lossTable = table([
            { label: "Stage", key: "stage", get: (r) => `Stage ${r.stage}` },
            { label: "Epoch", key: "epoch", numeric: true },
            { label: "Total Loss", key: "total_loss", numeric: true },
            { label: "Forward Loss", key: "forward_loss", numeric: true },
            { label: "Recon Loss", key: "recon_loss", numeric: true },
            { label: "Market Loss", key: "market_loss", numeric: true },
            { label: "Learning Rate", key: "learning_rate", numeric: true },
            { label: "Steps", key: "steps", numeric: true },
            { label: "Duration (s)", key: "seconds", numeric: true },
          ], epochs, "No epoch metrics recorded.");
          lossPanel.append(lossTable);
        } else {
          lossPanel.append(text("p", "empty-state", "No neural foundation loss history is loaded. Launch a foundation pre-training or demo run to record convergence metrics."));
        }

        const runsPanel = text("section", "panel section-space");
        runsPanel.append(
          text("h3", "", "Saved Models & Checkpoint History"),
          text("p", "muted compact", "Detected model checkpoints and walk-forward harnesses under analysis/ and demo trees:"),
          table([
            { label: "Run / Model Name", key: "name" },
            { label: "Kind", key: "kind", badge: true },
            { label: "Device", key: "device" },
            { label: "Epochs", key: "epochs", numeric: true },
            { label: "Checkpoints", get: (r) => (r.checkpoints || []).join(", ") || "none" },
            { label: "Metrics Available", get: (r) => r.has_metrics ? "yes" : "no", badge: true },
            { label: "Modified", key: "modified" },
          ], data.runs || [], "No saved model checkpoints found.")
        );

        mount(trainingSection, hwPanel, launchSection, lossPanel, runsPanel);
      } catch (error) {
        failure(trainingSection, error);
      }
    });
  }

  return {
    register, render, pages: () => [...renderers.keys()],
    helpers: { text, badge, table, keyValues, panel, warnings, fetchJson, mount, loading, failure, show, copyButton },
  };
})();
