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
    if (/failed|error|corrupt|refused|abort|blocked|at.risk|missing/.test(normalized)) tone = "bad";
    else if (/warn|stale|unverified|recovery|incomplete|skip|partial|unregistered|disabled|not_registered|unknown/.test(normalized)) tone = "warning";
    else if (/running|queued|pending|loading/.test(normalized)) tone = "running";
    else if (/valid|complete|success|ready|healthy|ok|available|configured|present|yes|true|registered|enabled/.test(normalized)) tone = "good";
    return text("span", `status-pill ${tone}`, String(value ?? "unknown").replaceAll("_", " "));
  }

  function table(columns, rows, emptyMessage) {
    const wrap = text("div", "table-scroll");
    const node = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const column of columns) {
      const cell = text("th", column.numeric ? "numeric" : "", column.label);
      headRow.append(cell);
    }
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

  /* The placeholder pages already show which observed roots the console will
     read, so the router and the labelled-root contract can be verified before
     any page-specific reader exists. */
  function renderRoots(section, snapshot) {
    const target = section.querySelector("[data-roots]");
    if (!target) return;
    const roots = Array.isArray(snapshot?.roots) ? snapshot.roots : [];
    target.replaceChildren(...(roots.length
      ? roots.map((root) => {
        const row = text("span", "tag", `${root.kind}: ${root.label}`);
        return row;
      })
      : [text("span", "muted", "No observed roots reported yet.")]));
  }

  for (const section of document.querySelectorAll("section.page[data-page]")) {
    if (section.hasAttribute("data-placeholder")) {
      register(section.dataset.page, (snapshot) => renderRoots(section, snapshot));
    }
  }

  /* Docs page: the catalog with registration status, and a text-only reader. */
  const docsSection = document.querySelector('section.page[data-page="docs"]');
  if (docsSection) {
    let selected = null;

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

    let renderedIndex = "";

    register("docs", async () => {
      const list = docsSection.querySelector("[data-doc-list]");
      if (!list) return;
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
  if (envSection) {
    let renderedEnv = "";
    register("environment", async () => {
      try {
        const data = await fetchJson("/api/env", { ttl: 30000 });
        const fingerprint = JSON.stringify(data);
        if (fingerprint === renderedEnv) return;
        renderedEnv = fingerprint;
        const session = data.session;
        const copyButton = (command) => {
          const button = text("button", "button secondary small", "Copy");
          button.type = "button";
          button.addEventListener("click", async () => {
            try { await navigator.clipboard.writeText(command); button.textContent = "Copied"; }
            catch { button.textContent = "Select to copy"; }
          });
          return button;
        };
        const gateRows = data.gates.map((gate) => {
          const line = text("div", "module-command");
          line.append(text("code", "", gate.command), copyButton(gate.command));
          return { name: gate.name, command: line };
        });
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
            ...Object.keys(Object.assign({}, ...data.interpreters.map((row) => row.packages))).filter((key) => key !== "error").map((key) => ({ label: key, get: (row) => row.packages[key] === true ? "installed" : row.packages[key] === false ? "missing" : "unknown", badge: true })),
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

  return { register, render, pages: () => [...renderers.keys()], helpers: { text, badge, table, keyValues, panel, warnings, fetchJson, mount, loading, failure, show } };
})();
