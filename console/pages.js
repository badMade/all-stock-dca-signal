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

  return { register, render, pages: () => [...renderers.keys()] };
})();
