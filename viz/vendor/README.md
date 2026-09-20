# Vendored third-party viewer assets

This directory holds the **only** third-party code in this repository's runtime tree.
It is deliberately narrow, and the reasoning below is recorded so a future bump knows
exactly what it is walking into.

## Why anything is vendored at all

`CLAUDE.md`'s first hard invariant is "Standard library only at runtime. Python >=3.11,
zero third-party runtime dependencies." That invariant is about **Python**, and it is
not weakened here: nothing in this directory is ever imported by Python. `three.min.js`
is read as bytes by `dca_market_graph.build_viewer_html()` and pasted into an HTML
`<script>` tag. It executes in the reader's browser, never in this process.

Concretely, it is invisible to every gate that enforces the invariant: it is not in
`pyproject.toml`'s `dependencies`, `pip-audit` does not see it, `ruff` does not scan it,
and `coverage` does not measure it.

The alternative was hand-rolling a WebGL renderer (shaders, matrix math, frustum
culling, instancing). That is several thousand lines of untested browser code to
maintain in a repository whose competence is trading logic, not graphics.

## three.js

| | |
| --- | --- |
| Version | **0.149.0** (r149) |
| File | `build/three.min.js` (UMD) |
| License | MIT (see `three.LICENSE`) |
| Source | `https://registry.npmjs.org/three/-/three-0.149.0.tgz` |
| Tarball sha256 | `ab8b7c131f6ce72e05081dff4882046584e4a4ce5ad864f9f2a0e3146cc3997c` |
| `three.min.js` sha256 | `8a5f7249903b54d30f79f708699d2fed2d6a1d0741a4cd41377d1f01bb5a2271` |
| Size | 608,081 bytes (~167 KB gzipped) |

To re-verify or re-vendor:

```bash
curl -sSO https://registry.npmjs.org/three/-/three-0.149.0.tgz
sha256sum three-0.149.0.tgz          # must match the tarball sha256 above
tar xzf three-0.149.0.tgz package/build/three.min.js package/LICENSE
sha256sum package/build/three.min.js  # must match the file sha256 above
```

Note that `registry.npmjs.org` is reachable from a sandboxed agent session while CDNs
such as `unpkg.com` are not, so the registry tarball is the reproducible source.

### Why 0.149.0 and not the latest

The viewer needs a **classic script** that defines a global `THREE`, because the emitted
HTML must be a single self-contained file that works from a `file://` URL with no
network access and no import map.

- `build/three.min.js` is the UMD bundle that provides exactly that.
- It was **deprecated at r150** (loading it prints a console warning) and **removed at
  r160**. 0.149.0 is the last release where it is the real, unencumbered build.
- The modern ESM build (`three.module.min.js`) would have to be inlined as a
  `data:text/javascript;base64,...` module import to stay self-contained. That inflates
  the payload by ~33% for the base64 encoding and is markedly fussier.

So the pin is a deliberate trade: an older three.js in exchange for a plain
`<script>` tag and a genuinely portable single-file artifact.

**If you ever need to bump past r159**, the ESM + data-URL route is the path, and
`dca_market_graph.build_viewer_html()` plus the `{{THREE_JS}}` placeholder in
`../market_graph.html.tmpl` are the two places that change.

### Why OrbitControls is *not* vendored

`OrbitControls` moved to `examples/jsm/` (ESM only) at r147, so vendoring it would
reintroduce exactly the module-loading problem the 0.149.0 pin exists to avoid — for
roughly fifty lines of pointer arithmetic. `../market_graph.js` implements its own
orbit/pan/zoom controller instead. One vendored file, not two.
