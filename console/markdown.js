/* A bounded, text-node-only markdown renderer for report artifacts.
   Supports headings, paragraphs, unordered and ordered lists, pipe tables,
   fenced code, bold and inline code. Links and raw HTML are shown as text:
   a report is data, never markup, and the page's CSP forbids inline handlers
   anyway (GUI expansion Phase 4). */
"use strict";

window.ConsoleMarkdown = (() => {
  const MAX_CHARS = 200_000;

  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = String(value);
    return element;
  }

  /* Inline formatting: **bold** and `code` only; everything else is literal text. */
  function inline(target, text) {
    const pattern = /(\*\*[^*]+\*\*|`[^`]+`)/g;
    let last = 0;
    for (const match of text.matchAll(pattern)) {
      if (match.index > last) target.append(document.createTextNode(text.slice(last, match.index)));
      const token = match[0];
      if (token.startsWith("**")) target.append(node("strong", "", token.slice(2, -2)));
      else target.append(node("code", "", token.slice(1, -1)));
      last = match.index + token.length;
    }
    if (last < text.length) target.append(document.createTextNode(text.slice(last)));
  }

  function tableFrom(lines) {
    const rows = lines.map((line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim()));
    const table = document.createElement("table");
    const head = document.createElement("thead");
    const headRow = document.createElement("tr");
    for (const cell of rows[0]) { const th = node("th"); inline(th, cell); headRow.append(th); }
    head.append(headRow);
    const body = document.createElement("tbody");
    for (const cells of rows.slice(2)) {
      const row = document.createElement("tr");
      for (const cell of cells) { const td = node("td"); inline(td, cell); row.append(td); }
      body.append(row);
    }
    table.append(head, body);
    const wrap = node("div", "table-scroll");
    wrap.append(table);
    return wrap;
  }

  function render(markdown) {
    const container = node("div", "markdown");
    const text = String(markdown ?? "").slice(0, MAX_CHARS);
    const lines = text.split("\n");
    let index = 0;
    let paragraph = [];
    const flush = () => {
      if (!paragraph.length) return;
      const p = node("p");
      inline(p, paragraph.join(" "));
      container.append(p);
      paragraph = [];
    };
    while (index < lines.length) {
      const line = lines[index];
      const trimmed = line.trim();
      if (trimmed.startsWith("```")) {
        flush();
        const code = [];
        index += 1;
        while (index < lines.length && !lines[index].trim().startsWith("```")) { code.push(lines[index]); index += 1; }
        container.append(node("pre", "markdown-code", code.join("\n")));
        index += 1;
        continue;
      }
      const heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (heading) {
        flush();
        const h = node(`h${Math.min(6, heading[1].length + 1)}`, "markdown-heading");
        inline(h, heading[2]);
        container.append(h);
        index += 1;
        continue;
      }
      if (trimmed.startsWith("|") && index + 1 < lines.length && /^\|?\s*:?-{2,}/.test(lines[index + 1].trim())) {
        flush();
        const block = [];
        while (index < lines.length && lines[index].trim().startsWith("|")) { block.push(lines[index]); index += 1; }
        if (block.length >= 2) container.append(tableFrom(block));
        continue;
      }
      const listItem = /^([-*]|\d+\.)\s+(.*)$/.exec(trimmed);
      if (listItem) {
        flush();
        const ordered = /^\d+\./.test(listItem[1]);
        const list = document.createElement(ordered ? "ol" : "ul");
        while (index < lines.length) {
          const item = /^([-*]|\d+\.)\s+(.*)$/.exec(lines[index].trim());
          if (!item) break;
          const li = node("li");
          inline(li, item[2]);
          list.append(li);
          index += 1;
        }
        container.append(list);
        continue;
      }
      if (!trimmed) { flush(); index += 1; continue; }
      paragraph.push(trimmed);
      index += 1;
    }
    flush();
    if (text.length >= MAX_CHARS) container.append(node("p", "muted", "Rendering stopped at the size limit."));
    return container;
  }

  return { render };
})();
