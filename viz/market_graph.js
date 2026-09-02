/* 3D market graph viewer -- "The Ocean".
 *
 * Renders the payload emitted by dca_market_graph.py in two modes that share one
 * normalized grid:
 *
 *   Pulse -- time is the animation clock. Every ticker is a box glyph whose Y extent
 *            is the day's high/low range and whose Z extent is the open->close body,
 *            positioned along X by volume. Recent frames persist as fading trails, so
 *            a broad move reads as a ripple passing through the cloud.
 *
 *   Wave  -- time becomes a spatial axis. The ground plane is (time x ticker) with
 *            tickers in seriated order, and Y is the high/low band drawn as a
 *            translucent envelope, so the whole window stands still as a terrain.
 *
 * Written against three.js r149's UMD build, which supplies a global `THREE`. See
 * viz/vendor/README.md for why that version and not the current one. Orbit controls
 * are hand-rolled below rather than vendored, because OrbitControls is ESM-only from
 * r147 and would reintroduce exactly the module-loading problem the pin avoids.
 *
 * No build step, no bundler, no external requests: this file is inlined verbatim into
 * a single self-contained HTML file that runs from a file:// URL.
 */

(function () {
  "use strict";

  // --- world sizing --------------------------------------------------------
  // Chosen so the default camera framing shows the whole field without the user
  // having to zoom, and so the price channels (Y, Z) read at a comparable visual
  // scale to volume (X) rather than being dwarfed by it.
  var W = {
    volume: 34,      // X extent for the [0,1] volume channel (pulse)
    price: 6,        // world units per 1.0 of normalized price move (pulse)
    pulseClamp: 1.9, // clip glyph extents at this many normalized units -- see updatePulse
    time: 92,        // X extent of the time axis (wave)
    timeStrip: 3.0,  // ...but a single-session payload gets this narrow strip instead,
                     // mirroring how one ticker gets a ribbon a single pitch deep --
                     // spanning one observation across the full 92 units would read as
                     // a month of flat terrain that nobody measured
    tickerPitch: 3.0,   // world units between adjacent ticker rows (wave)
    tickerMaxDepth: 46, // ...but never spread the ticker axis wider than this
    waveHeight: 4,   // world units per 1.0 of normalized price move (wave)
    waveClamp: 2.2,  // clip the surface at this many normalized units -- see buildWave
    glyphWidth: 0.8,
    minGlyph: 0.05   // a doji still needs to be visible
  };

  // Camera framing per mode. Pulse's field is roughly 34 x 18 x 18 world units, so it
  // wants a much closer eye than Wave's 64-unit-long time axis.
  var FRAMING = {
    pulse: { radius: 52, phi: 1.06, theta: 0.86 },
    wave: { radius: 138, phi: 0.92, theta: 0.74 }
  };

  var MAX_TRAIL = 60;
  var BG = 0x0b0e14;

  function clamp(value, low, high) {
    return value < low ? low : value > high ? high : value;
  }

  // --- payload -------------------------------------------------------------

  var payload;
  try {
    payload = JSON.parse(document.getElementById("payload").textContent);
  } catch (err) {
    return fail("Could not parse the embedded payload: " + err.message);
  }
  if (!payload || payload.schema_version !== 1) {
    return fail(
      "Unsupported payload schema (found " +
        (payload && payload.schema_version) +
        ", expected 1). Regenerate this file with the matching version of " +
        "dca_market_graph.py."
    );
  }
  if (typeof THREE === "undefined") {
    return fail("three.js failed to load -- this file was emitted without its inlined renderer.");
  }
  if (!payload.series || !payload.series.length || !payload.dates.length) {
    return fail("Payload contains no plottable series.");
  }

  var MARKET = "__MARKET__";
  var frames = payload.dates.length;

  // --- state ---------------------------------------------------------------

  var state = {
    mode: "pulse",
    frame: frames - 1,
    playing: false,
    trail: Math.max(1, Math.min(MAX_TRAIL, (payload.meta && payload.meta.default_trail) || 20)),
    msPerFrame: 60,
    colorByCluster: false,
    showMarket: true,
    hidden: {}          // cluster id -> true when the user toggles a band off
  };

  // Cluster lookup per ticker, so both renderers can colour and filter identically.
  var clusterOf = {};
  var clusterColor = {};
  (payload.grouping.clusters || []).forEach(function (cluster) {
    clusterColor[cluster.id] = new THREE.Color(cluster.color);
    (cluster.tickers || []).forEach(function (t) { clusterOf[t] = cluster.id; });
  });

  var byTicker = {};
  payload.series.forEach(function (s) { byTicker[s.ticker] = s; });

  // Draw order follows the grouping, so adjacent rows in Wave are similar names.
  var order = (payload.grouping.order || []).filter(function (t) { return byTicker[t]; });
  payload.series.forEach(function (s) {
    if (s.ticker !== MARKET && order.indexOf(s.ticker) === -1) order.push(s.ticker);
  });

  function visibleTickers() {
    return order.filter(function (t) {
      if (t === MARKET) return false;
      var cid = clusterOf[t];
      return !(cid !== undefined && state.hidden[cid]);
    });
  }

  // --- scene ---------------------------------------------------------------

  var stage = document.getElementById("stage");
  var renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  if (THREE.sRGBEncoding !== undefined) renderer.outputEncoding = THREE.sRGBEncoding;
  stage.appendChild(renderer.domElement);

  var scene = new THREE.Scene();
  scene.background = new THREE.Color(BG);
  scene.fog = new THREE.Fog(BG, 150, 420);

  var camera = new THREE.PerspectiveCamera(46, 1, 0.5, 600);

  scene.add(new THREE.AmbientLight(0xffffff, 0.62));
  var key = new THREE.DirectionalLight(0xffffff, 0.85);
  key.position.set(28, 44, 26);
  scene.add(key);
  var rim = new THREE.DirectionalLight(0x88aaff, 0.28);
  rim.position.set(-30, -12, -24);
  scene.add(rim);

  var pulseGroup = new THREE.Group();
  var waveGroup = new THREE.Group();
  scene.add(pulseGroup, waveGroup);

  // --- hand-rolled orbit controls -----------------------------------------
  // Spherical camera around a target: left-drag orbits, right-drag (or shift-drag)
  // pans, wheel dollies. Roughly fifty lines, versus vendoring a second ESM-only file.

  var orbit = { radius: 96, theta: 0.86, phi: 1.06, target: new THREE.Vector3(0, 0, 0) };
  var drag = null;

  function applyCamera() {
    orbit.phi = Math.max(0.06, Math.min(Math.PI - 0.06, orbit.phi));
    orbit.radius = Math.max(12, Math.min(420, orbit.radius));
    camera.position.set(
      orbit.target.x + orbit.radius * Math.sin(orbit.phi) * Math.sin(orbit.theta),
      orbit.target.y + orbit.radius * Math.cos(orbit.phi),
      orbit.target.z + orbit.radius * Math.sin(orbit.phi) * Math.cos(orbit.theta)
    );
    camera.lookAt(orbit.target);
  }

  renderer.domElement.addEventListener("pointerdown", function (e) {
    renderer.domElement.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, pan: e.button === 2 || e.shiftKey };
  });
  renderer.domElement.addEventListener("pointerup", function (e) {
    if (drag) renderer.domElement.releasePointerCapture(e.pointerId);
    drag = null;
  });
  renderer.domElement.addEventListener("pointerleave", function () { drag = null; });
  renderer.domElement.addEventListener("contextmenu", function (e) { e.preventDefault(); });

  renderer.domElement.addEventListener("pointermove", function (e) {
    if (!drag) { hover(e); return; }
    var dx = e.clientX - drag.x;
    var dy = e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    if (drag.pan) {
      // Pan in the camera's own screen plane so dragging feels like moving the paper.
      var right = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 0);
      var up = new THREE.Vector3().setFromMatrixColumn(camera.matrix, 1);
      var k = orbit.radius * 0.0016;
      orbit.target.addScaledVector(right, -dx * k).addScaledVector(up, dy * k);
    } else {
      orbit.theta -= dx * 0.006;
      orbit.phi -= dy * 0.006;
    }
    applyCamera();
  });

  renderer.domElement.addEventListener("wheel", function (e) {
    e.preventDefault();
    orbit.radius *= Math.pow(1.0012, e.deltaY);
    applyCamera();
  }, { passive: false });

  // --- axes ----------------------------------------------------------------

  var axisGroup = new THREE.Group();
  scene.add(axisGroup);

  function drawAxis(from, to, color) {
    var geo = new THREE.BufferGeometry().setFromPoints([from, to]);
    axisGroup.add(new THREE.Line(geo, new THREE.LineBasicMaterial({ color: color, transparent: true, opacity: 0.55 })));
  }

  function rebuildAxes() {
    while (axisGroup.children.length) axisGroup.remove(axisGroup.children[0]);
    var V = THREE.Vector3;
    // A faint ground plane at y=0 (the "flat vs prior close" plane). Without it the
    // glyph cloud floats in featureless black and the eye has nothing to judge depth
    // or vertical position against while orbiting.
    var grid = new THREE.GridHelper(
      state.mode === "pulse" ? W.volume * 1.4 : Math.max(timeExtent(), waveDepth()) * 1.1,
      16, 0x2b3446, 0x1a2130
    );
    grid.material.transparent = true;
    grid.material.opacity = 0.5;
    axisGroup.add(grid);
    if (state.mode === "pulse") {
      var hx = W.volume / 2, hy = W.price * 1.5, hz = W.price * 1.5;
      drawAxis(new V(-hx, 0, 0), new V(hx, 0, 0), 0xe0b64a);   // X volume
      drawAxis(new V(0, -hy, 0), new V(0, hy, 0), 0x4c9be8);   // Y high/low
      drawAxis(new V(0, 0, -hz), new V(0, 0, hz), 0xc86fd4);   // Z open/close
    } else {
      var tx = timeExtent() / 2, tz = waveDepth() / 2;
      drawAxis(new V(-tx, 0, -tz), new V(tx, 0, -tz), 0xe0b64a);
      drawAxis(new V(-tx, -W.waveHeight * W.waveClamp, -tz), new V(-tx, W.waveHeight * W.waveClamp, -tz), 0x4c9be8);
      drawAxis(new V(-tx, 0, -tz), new V(-tx, 0, tz), 0xc86fd4);
    }
  }

  // --- pulse renderer ------------------------------------------------------

  var pulseMesh = null;
  var pulseIndex = [];   // instance slot -> {ticker, frame} for raycast lookup
  var dummy = new THREE.Object3D();
  var bgColor = new THREE.Color(BG);
  var upColor = new THREE.Color(0x4fbf88);
  var downColor = new THREE.Color(0xe2688f);
  var flatColor = new THREE.Color(0x7d879b);
  var marketColor = new THREE.Color(0xf2f5fa);

  function buildPulse() {
    if (pulseMesh) {
      pulseGroup.remove(pulseMesh);
      pulseMesh.geometry.dispose();
      pulseMesh.material.dispose();
    }
    var tickers = visibleTickers();
    var count = Math.max(1, (tickers.length + 1) * MAX_TRAIL);
    // depthWrite off so glyphs blend instead of occluding: with ~50 tickers sharing
    // one volume/range/body space, overlap is the norm, and an opaque front glyph
    // would hide the very co-movement the mode exists to show. The usual cost of
    // depthWrite:false -- visible sort artifacts on large flat surfaces -- does not
    // bite here because the glyphs are small and convex.
    var material = new THREE.MeshLambertMaterial({
      transparent: true, opacity: 0.5, depthWrite: false
    });
    pulseMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1, 1, 1), material, count);
    pulseMesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    pulseGroup.add(pulseMesh);
    updatePulse();
  }

  function glyphColor(series, frame, isMarket) {
    if (isMarket) return marketColor;
    if (state.colorByCluster) {
      var cid = clusterOf[series.ticker];
      if (cid !== undefined && clusterColor[cid]) return clusterColor[cid];
      return flatColor;
    }
    var open = series.zo[frame], close = series.zc[frame];
    if (open === null || close === null) return flatColor;
    if (close > open) return upColor;
    if (close < open) return downColor;
    return flatColor;
  }

  function updatePulse() {
    if (!pulseMesh) return;
    var tickers = visibleTickers().slice();
    if (state.showMarket && byTicker[MARKET]) tickers.push(MARKET);

    var slot = 0;
    pulseIndex.length = 0;
    var color = new THREE.Color();

    for (var t = 0; t < tickers.length; t++) {
      var series = byTicker[tickers[t]];
      var isMarket = tickers[t] === MARKET;
      for (var age = 0; age < MAX_TRAIL; age++) {
        var frame = state.frame - age;
        var live = age < state.trail && frame >= 0 && series.x[frame] !== null;
        if (!live) {
          // Park unused instances at zero scale -- cheaper than rebuilding the mesh
          // every time the trail length or the cluster filter changes.
          dummy.position.set(0, 0, 0);
          dummy.scale.set(0, 0, 0);
          dummy.updateMatrix();
          pulseMesh.setMatrixAt(slot, dummy.matrix);
          pulseIndex[slot] = null;
          slot++;
          continue;
        }

        // The glyph: one box. Y spans the day's low->high (the wick), Z spans
        // open->close (the body), X sits at the day's volume.
        // Clip the extents. Without this the highest-volatility names draw glyphs
        // several times larger than everything else and physically enclose the rest
        // of the market, so the cloud becomes one opaque slab. Clipping costs the
        // "this was an enormous day" cue, which the tooltip still reports exactly.
        var C = W.pulseClamp;
        var yl = clamp(series.yl[frame], -C, C) * W.price;
        var yh = clamp(series.yh[frame], -C, C) * W.price;
        var zo = clamp(series.zo[frame], -C, C) * W.price;
        var zc = clamp(series.zc[frame], -C, C) * W.price;
        var fade = age / Math.max(1, state.trail);      // 0 = current day
        var shrink = 1 - 0.45 * fade;

        dummy.position.set(
          (series.x[frame] - 0.5) * W.volume,
          (yl + yh) / 2,
          (zo + zc) / 2
        );
        dummy.scale.set(
          W.glyphWidth * (isMarket ? 1.9 : 1) * shrink,
          Math.max(Math.abs(yh - yl), W.minGlyph) * shrink,
          Math.max(Math.abs(zc - zo), W.minGlyph) * shrink
        );
        dummy.updateMatrix();
        pulseMesh.setMatrixAt(slot, dummy.matrix);

        // InstancedMesh has no per-instance opacity in r149, so trails fade by
        // lerping toward the background colour instead. Visually equivalent here
        // because the background is flat and opaque.
        color.copy(glyphColor(series, frame, isMarket)).lerp(bgColor, 0.82 * fade);
        pulseMesh.setColorAt(slot, color);
        pulseIndex[slot] = { ticker: tickers[t], frame: frame };
        slot++;
      }
    }

    pulseMesh.count = slot;
    pulseMesh.instanceMatrix.needsUpdate = true;
    if (pulseMesh.instanceColor) pulseMesh.instanceColor.needsUpdate = true;
  }

  // --- wave renderer -------------------------------------------------------

  var waveMesh = null;

  function waveDepth() {
    var rows = visibleTickers().length;
    return Math.min(W.tickerPitch * Math.max(rows - 1, 1), W.tickerMaxDepth);
  }

  // The time axis's world extent, which is NOT the constant W.time for every payload:
  // a one-session window gets the narrow strip instead. Shared by buildWave, the axes,
  // and the camera so all three agree about how long the drawn axis actually is --
  // three copies of `W.time` would leave the grid and the eye framing a 92-unit axis
  // around a 3-unit strip, which is how "renders nothing" becomes "renders a speck".
  function timeExtent() {
    return frames === 1 ? W.timeStrip : W.time;
  }

  // Framed to what is drawn rather than to the widest axis a payload could have. The
  // tuned 138 assumes the full-length time axis; the ticker depth and the clamped
  // vertical band set the floor so a strip is not framed closer than it is tall.
  function waveRadius() {
    var extent = Math.max(timeExtent(), waveDepth(), W.waveHeight * W.waveClamp * 2);
    return FRAMING.wave.radius * (extent / W.time);
  }

  function buildWave() {
    if (waveMesh) {
      waveGroup.remove(waveMesh);
      waveMesh.geometry.dispose();
      waveMesh.material.dispose();
      waveMesh = null;
    }
    var tickers = visibleTickers();
    if (tickers.length < 1 || frames < 1) return;

    var rows = tickers.length;
    var cols = frames;
    // A single ticker still has a surface to draw: extrude its one row into a narrow
    // ribbon rather than returning early. `--tickers NVDA --mode wave` is a supported
    // request, and answering it with an empty scene reads as a broken page.
    var ribbon = rows === 1;
    if (ribbon) rows = 2;
    // The identical argument on the other axis, which the ribbon fix originally missed.
    // `--start 2025-03-03 --end 2025-03-03` is equally supported -- build_payload()
    // deliberately emits that one frame -- and a surface needs two columns to form a
    // quad, so the single column is duplicated into a narrow strip. Only the genuinely
    // empty case returns early now; a degenerate dimension gets extruded, not refused.
    var strip = cols === 1;
    if (strip) cols = 2;
    var perSurface = rows * cols;
    // Ticker-axis depth follows the ticker COUNT rather than filling a fixed extent.
    // Spreading five tickers across a wide axis stretches the triangles between
    // unrelated names into huge sloped sheets that read as structure but are pure
    // interpolation; a constant row pitch keeps neighbouring rows honestly adjacent.
    var depth = waveDepth();
    var positions = new Float32Array(perSurface * 2 * 3);
    var colors = new Float32Array(perSurface * 2 * 3);
    // Per-vertex validity. A vertex is valid only once its ticker has had a real
    // observation; quads touching an invalid corner are omitted from the index buffer
    // below rather than drawn.
    var valid = new Uint8Array(perSurface);
    var color = new THREE.Color();

    for (var r = 0; r < rows; r++) {
      var series = byTicker[tickers[ribbon ? 0 : r]];
      var z = (r / (rows - 1) - 0.5) * depth;
      // Carry-forward is bounded at BOTH ends, and only interior gaps get it.
      //
      //   before the first observation -- nothing to carry, and seeding zero would draw
      //     flat terrain for a period the ticker did not trade in at all;
      //   after the last observation -- carrying on would make a delisted, suspended or
      //     merely stale series appear to keep trading flat to the end of the axis,
      //     which is the same fabrication pointing the other way;
      //   between them -- a single untraded session leaves the surface flat rather than
      //     tearing a hole through it, which is what a reader expects.
      //
      // So find the last real column first, then gate validity on both bounds.
      var lastReal = -1;
      for (var back = frames - 1; back >= 0; back--) {
        if (series.yh[back] !== null) { lastReal = back; break; }
      }
      var lastHigh = 0, lastLow = 0, lastBody = 0, seen = false;
      for (var c = 0; c < cols; c++) {
        // Both duplicated columns of a strip read the same single frame, exactly as
        // both duplicated rows of a ribbon read the same single ticker.
        var sc = strip ? 0 : c;
        if (series.yh[sc] !== null) {
          lastHigh = series.yh[sc];
          lastLow = series.yl[sc];
          lastBody = series.zc[sc] - series.zo[sc];
          seen = true;
        }
        var x = (c / (cols - 1) - 0.5) * timeExtent();
        var i = r * cols + c;
        valid[i] = (seen && sc <= lastReal) ? 1 : 0;

        // Clip rather than autoscale: one 15% earnings gap would otherwise set the
        // vertical scale for the whole terrain and flatten every ordinary session into
        // the floor. The clipped peaks stay visibly pegged at the ceiling.
        positions[i * 3] = x;
        positions[i * 3 + 1] = clamp(lastHigh, -W.waveClamp, W.waveClamp) * W.waveHeight;
        positions[i * 3 + 2] = z;

        var j = perSurface + i;
        positions[j * 3] = x;
        positions[j * 3 + 1] = clamp(lastLow, -W.waveClamp, W.waveClamp) * W.waveHeight;
        positions[j * 3 + 2] = z;

        if (state.colorByCluster) {
          var cid = clusterOf[tickers[ribbon ? 0 : r]];
          color.copy(cid !== undefined && clusterColor[cid] ? clusterColor[cid] : flatColor);
        } else {
          // Diverging on the open->close body: green where the day closed up,
          // pink where it closed down, grey at flat.
          var strength = Math.max(-1, Math.min(1, lastBody * 2.2));
          color.copy(flatColor).lerp(strength >= 0 ? upColor : downColor, Math.abs(strength));
        }
        colors[i * 3] = color.r; colors[i * 3 + 1] = color.g; colors[i * 3 + 2] = color.b;
        colors[j * 3] = color.r; colors[j * 3 + 1] = color.g; colors[j * 3 + 2] = color.b;
      }
    }

    // Two independent surfaces sharing one buffer: the high sheet and the low sheet.
    // The gap between them is the day's range, so the terrain has visible thickness
    // exactly where the market was volatile.
    var indices = [];
    for (var surface = 0; surface < 2; surface++) {
      var base = surface * perSurface;
      for (var rr = 0; rr < rows - 1; rr++) {
        for (var cc = 0; cc < cols - 1; cc++) {
          var q0 = rr * cols + cc;
          var q1 = q0 + 1;
          var q2 = (rr + 1) * cols + cc;
          var q3 = q2 + 1;
          // Every corner must be a session the ticker had actually traded by. One
          // invalid corner means this quad would span into pre-history, so it is
          // simply not emitted -- the surface begins where the data does.
          if (!(valid[q0] && valid[q1] && valid[q2] && valid[q3])) continue;
          indices.push(base + q0, base + q2, base + q1,
                       base + q1, base + q2, base + q3);
        }
      }
    }
    if (!indices.length) return;   // nothing overlapped enough to form a surface

    var geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    // Sized by the VERTEX count, not the index count: a Uint16 index cannot address a
    // vertex above 65535 no matter how few indices there happen to be.
    geometry.setIndex(perSurface * 2 > 65535 ? new THREE.Uint32BufferAttribute(indices, 1)
                                             : new THREE.Uint16BufferAttribute(indices, 1));
    geometry.computeVertexNormals();

    waveMesh = new THREE.Mesh(geometry, new THREE.MeshLambertMaterial({
      vertexColors: true,
      side: THREE.DoubleSide,
      transparent: true,
      opacity: 0.86,
      flatShading: false
    }));
    waveGroup.add(waveMesh);
  }

  // --- mode switching ------------------------------------------------------

  function setMode(mode) {
    state.mode = mode;
    pulseGroup.visible = mode === "pulse";
    waveGroup.visible = mode === "wave";
    document.getElementById("playback").style.display = mode === "pulse" ? "" : "none";
    var frame = FRAMING[mode];
    if (mode === "pulse") buildPulse(); else buildWave();
    orbit.radius = mode === "wave" ? waveRadius() : frame.radius;
    orbit.phi = frame.phi;
    orbit.theta = frame.theta;
    orbit.target.set(0, 0, 0);
    var metric = payload.scales.price_metric === "rolling_high_drawdown"
      ? "drawdown below the rolling " + payload.scales.drawdown_window + "-session high"
      : "movement vs prior close";
    document.getElementById("mode-hint").textContent = mode === "pulse"
      ? "One frame per trading day. X volume, Y high/low, Z open/close; " + metric + "; trails show the last "
        + state.trail + " days."
      : "Time runs along X, tickers along Z in similarity order. The band's thickness is the day's range; " + metric + ".";
    document.getElementById("ax-x-desc").textContent = mode === "pulse" ? "volume" : "time";
    document.getElementById("ax-y-desc").textContent = "high / low";
    document.getElementById("ax-z-desc").textContent = mode === "pulse" ? "open / close" : "ticker (grouped)";
    rebuildAxes();
    applyCamera();
  }

  function rebuildActive() {
    if (state.mode === "pulse") buildPulse(); else buildWave();
  }

  // --- hover ---------------------------------------------------------------

  var raycaster = new THREE.Raycaster();
  var pointer = new THREE.Vector2();
  var tooltip = document.getElementById("tooltip");

  function hover(event) {
    if (state.mode !== "pulse" || !pulseMesh) { tooltip.hidden = true; return; }
    var rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    var hits = raycaster.intersectObject(pulseMesh);
    var found = null;
    for (var i = 0; i < hits.length; i++) {
      var entry = pulseIndex[hits[i].instanceId];
      if (entry) { found = entry; break; }
    }
    if (!found) { tooltip.hidden = true; return; }

    var series = byTicker[found.ticker];
    var pct = payload.scales.pct_per_unit * 100;
    tooltip.replaceChildren();
    var title = document.createElement("b");
    title.textContent = found.ticker;
    tooltip.appendChild(title);
    tooltip.appendChild(document.createTextNode(
      "  " + String(payload.dates[found.frame]) + "\n" +
      "range  " + fmtPct(series.yl[found.frame] * pct) + " .. " + fmtPct(series.yh[found.frame] * pct) + "\n" +
      "body   " + fmtPct(series.zo[found.frame] * pct) + " -> " + fmtPct(series.zc[found.frame] * pct)
    ));
    tooltip.hidden = false;
    tooltip.style.left = (event.clientX + 14) + "px";
    tooltip.style.top = (event.clientY + 14) + "px";
  }

  function fmtPct(value) {
    if (value === null || value === undefined || isNaN(value)) return "  n/a";
    return (value >= 0 ? "+" : "") + value.toFixed(2) + "%";
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  // --- UI ------------------------------------------------------------------

  var scrub = document.getElementById("scrub");
  var frameLabel = document.getElementById("frame-label");
  var playButton = document.getElementById("play");

  scrub.max = String(frames - 1);
  scrub.value = String(state.frame);

  function setFrame(frame) {
    state.frame = Math.max(0, Math.min(frames - 1, frame));
    scrub.value = String(state.frame);
    frameLabel.textContent = payload.dates[state.frame] + "  ·  " + (state.frame + 1) + "/" + frames;
    updatePulse();
  }

  scrub.addEventListener("input", function () {
    state.playing = false;
    playButton.innerHTML = "&#9654;";
    setFrame(parseInt(scrub.value, 10));
  });

  playButton.addEventListener("click", togglePlay);

  function togglePlay() {
    state.playing = !state.playing;
    playButton.innerHTML = state.playing ? "&#10074;&#10074;" : "&#9654;";
    // Restarting from the end would otherwise sit on the last frame doing nothing.
    if (state.playing && state.frame >= frames - 1) setFrame(0);
  }

  // Shortcuts are document-level, so they must stand down whenever a real control has
  // focus. Otherwise Space on a focused checkbox toggles playback instead of the
  // checkbox, and the arrow keys steal the scrubber's and select's own behaviour --
  // which makes the panel unusable by keyboard.
  function typingInAControl() {
    var el = document.activeElement;
    if (!el || el === document.body) return false;
    if (el.isContentEditable) return true;
    return ["INPUT", "SELECT", "TEXTAREA", "BUTTON", "OPTION"].indexOf(el.tagName) !== -1;
  }

  document.addEventListener("keydown", function (e) {
    if (typingInAControl()) return;
    if (e.code === "Space" && state.mode === "pulse") { e.preventDefault(); togglePlay(); }
    if (e.code === "ArrowRight") { e.preventDefault(); setFrame(state.frame + 1); }
    if (e.code === "ArrowLeft") { e.preventDefault(); setFrame(state.frame - 1); }
  });

  document.getElementById("speed").addEventListener("change", function (e) {
    state.msPerFrame = parseInt(e.target.value, 10);
  });

  var trailInput = document.getElementById("trail");
  trailInput.max = String(MAX_TRAIL);
  trailInput.value = String(state.trail);
  document.getElementById("trail-label").textContent = String(state.trail);
  trailInput.addEventListener("input", function (e) {
    state.trail = parseInt(e.target.value, 10);
    document.getElementById("trail-label").textContent = String(state.trail);
    updatePulse();
  });

  document.getElementById("color-cluster").addEventListener("change", function (e) {
    state.colorByCluster = e.target.checked;
    rebuildActive();
  });

  document.getElementById("show-market").addEventListener("change", function (e) {
    state.showMarket = e.target.checked;
    if (state.mode === "pulse") buildPulse();
  });

  // Cluster legend: click a band to hide/show it in both modes.
  var legend = document.getElementById("legend");
  (payload.grouping.clusters || []).forEach(function (cluster) {
    // A real <button>, not a click handler on an <li>: this control hides and shows
    // data, so it has to be reachable by keyboard and announce its pressed state.
    var li = document.createElement("li");
    var button = document.createElement("button");
    button.type = "button";
    button.className = "legend-item";
    button.setAttribute("aria-pressed", "true");
    button.innerHTML =
      '<i class="swatch" style="background:' + escapeHtml(cluster.color) + '"></i>' +
      '<span class="name">' + escapeHtml(cluster.label) + "</span>" +
      '<span class="members">' + escapeHtml((cluster.tickers || []).join(" ")) + "</span>";
    button.addEventListener("click", function () {
      var hidden = !state.hidden[cluster.id];
      state.hidden[cluster.id] = hidden;
      li.classList.toggle("off", hidden);
      button.setAttribute("aria-pressed", hidden ? "false" : "true");
      rebuildActive();
    });
    li.appendChild(button);
    legend.appendChild(li);
  });
  if (!(payload.grouping.clusters || []).length) {
    document.getElementById("legend-panel").hidden = true;
  }

  document.getElementById("mode-toggle").addEventListener("click", function (e) {
    var button = e.target.closest("button[data-mode]");
    if (!button) return;
    Array.prototype.forEach.call(this.querySelectorAll("button"), function (b) {
      b.classList.toggle("on", b === button);
    });
    setMode(button.dataset.mode);
  });

  // --- static copy ---------------------------------------------------------

  var tickerCount = payload.tickers.filter(function (t) { return t !== MARKET; }).length;
  document.getElementById("subtitle").textContent =
    tickerCount + " tickers · " + frames + " sessions · " +
    payload.dates[0] + " to " + payload.dates[frames - 1] +
    (payload.source ? " · " + payload.source : "");

  document.getElementById("grouping-source").textContent =
    payload.grouping.source === "returns"
      ? "ordered by return correlation"
      : payload.grouping.source === "none"
        ? "no grouping applied"
        : "ordered by supplied grouping (" + payload.grouping.source + ")";

  var metricNote = payload.scales.price_metric === "rolling_high_drawdown"
    ? "below rolling " + payload.scales.drawdown_window + "-session high"
    : "vs prior close";
  document.getElementById("scale-note").textContent =
    "1 unit on Y/Z ≈ " + (payload.scales.pct_per_unit * 100).toFixed(2) +
    "% " + metricNote + " · volume: " + payload.scales.volume.mode.replace(/_/g, " ");

  // --- run -----------------------------------------------------------------

  function resize() {
    var width = stage.clientWidth || window.innerWidth;
    var height = stage.clientHeight || window.innerHeight;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
  }
  window.addEventListener("resize", resize);

  var lastStep = 0;
  function loop(now) {
    requestAnimationFrame(loop);
    if (state.playing && state.mode === "pulse" && now - lastStep >= state.msPerFrame) {
      lastStep = now;
      if (state.frame >= frames - 1) setFrame(0); else setFrame(state.frame + 1);
    }
    renderer.render(scene, camera);
  }

  function fail(message) {
    var box = document.getElementById("error");
    if (box) { box.textContent = message; box.hidden = false; }
    if (window.console) console.error(message);
  }

  resize();
  setFrame(state.frame);
  // Open in the mode the CLI's --mode asked for. "both" ships both renderers and has
  // to pick one to show first; Pulse is the better opening view because it is the mode
  // that animates. Anything unrecognised falls back the same way.
  var initialMode = payload.mode === "wave" ? "wave" : "pulse";
  Array.prototype.forEach.call(
    document.querySelectorAll("#mode-toggle button[data-mode]"),
    function (b) { b.classList.toggle("on", b.dataset.mode === initialMode); }
  );
  setMode(initialMode);
  requestAnimationFrame(loop);
})();
