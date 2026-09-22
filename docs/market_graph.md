# 3D market graph — "The Ocean"

A 3D visualization of market activity across the ticker universe, or of a single stock.
It is the first thing in this repository that produces pixels: every other output is
CSV, Markdown, or JSON.

It exists because tabular reports hide the one thing an eye is genuinely better at than
a summary table — whether a move is broad or idiosyncratic, and which names are moving
together. A drop table tells you eleven tickers fell more than 2%. The graph tells you
at a glance whether that was one sector rolling over or the whole market.

"The Ocean" is a **display title only**. Module, CLI, and doc filenames stay literal so
the code remains greppable for "market graph".

## The axes

| Axis | Channel |
| --- | --- |
| **X** | volume |
| **Y** | the high/low range |
| **Z** | the open→close body |
| time | the fourth channel, spatialized differently per mode |

Y means high/low in **both** modes. That is the through-line that keeps them one feature
rather than two.

## Two modes

### Pulse — time is the clock

One frame per trading day. Every ticker is a box glyph: its Y extent is that day's
low→high wick, its Z extent the open→close body, and it sits along X at that day's
volume. Recent frames persist as fading trails, so a sector-wide move reads as a ripple
travelling through the cloud.

Overlap is the point, not a defect. Two tickers occupying the same spot behaved
identically that day. Glyphs blend rather than occlude (`depthWrite` is off) precisely so
that co-movement stays visible through the front of the cloud.

### Wave — time is an axis

Time becomes spatial, so the whole window stands still as a terrain you orbit. The
ground plane is (time × ticker), with tickers in **similarity order** (see below), and
the height is the high/low band drawn as two stacked surfaces — the gap between them is
the day's range, so the terrain is visibly thicker where the market was volatile. Colour
carries the open→close body.

Wave deliberately re-tasks X and Z to give time a spatial home. This is the one place the
literal axis spec bends, and it is what makes a market-wide swing appear as a crest
sweeping across the ticker axis.

## Ticker ordering: grouping by similar behaviour

In whole-market mode the ticker axis must not be alphabetical — that scatters related
names and destroys the pattern the chart exists to show. Tickers are **seriated** so that
similar-behaving names sit adjacent, and banded into labelled, colour-coded groups.

There are two tiers, and **both work**:

| Tier | How to get it | Needs | What it captures |
| --- | --- | --- | --- |
| `returns` (default) | nothing — it is the default | nothing at all | "moves together": close-to-close return correlation |
| `file` | `ml-forecaster/cluster_tickers.py` | the ML venv + the ticker archive | "moves together" **and** "behaves like": volatility regime, drawdown character, volume profile |

### The default tier

`dca_market_graph.seriate_tickers()`, pure standard library:

1. close-to-close returns **aligned on each pair's own shared timestamp axis** —
   computed at correlation time from consecutive shared observations, not once per
   ticker against its own previous bar. A gap on one side used to let that side's return
   span a longer interval than its peer's at the same nominal endpoint, pairing two
   returns that covered different stretches of time and distorting exactly the halted,
   newly-listed, or archive-gappy names whose grouping is already least certain.
   `ml-forecaster/cluster_tickers.py`'s scipy tier avoids the identical problem by
   pivoting closes onto the shared date axis before differencing with
   `pct_change(fill_method=None)`; this is the same guarantee, built for the stdlib tier;
2. Pearson correlation over each pair's shared sessions — pairs with fewer than
   `MIN_OVERLAP_DAYS` (30) of overlap are treated as **uncorrelated**, because a handful
   of shared days can produce a 0.99 and invent a sector that does not exist. The
   overlap is counted in distinct **calendar days**, not in bars: an intraday source
   emits dozens of bars per session, and counting those would let a single morning clear
   a gate written to mean a month;
3. distance `sqrt(2·(1 − corr))` — a true metric, unlike `1 − corr`, which keeps average
   linkage well behaved;
4. average-linkage (UPGMA) clustering;
5. greedy leaf ordering: at each merge, orient the two subtrees so the touching leaves
   are the most similar of the four options.

> Not to be confused with `scripts/correlation.py`, which is about incident *correlation
> IDs* and has nothing to do with statistics.

### The `ml-forecaster` tier

`ml-forecaster/` already engineers a no-lookahead daily feature vector per (ticker, day)
for the sizing model: realized volatility, RSI posture, drawdown from the 52-week high,
trailing returns, relative volume. Two tickers sharing a volatility regime and volume
profile *behave alike* even when their day-to-day correlation is unremarkable.

`cluster_tickers.py` builds a per-ticker profile (mean + std of each feature, z-scored
across tickers), blends its Euclidean distance with the return-correlation distance
(`--alpha`, default 0.5), clusters with `scipy.cluster.hierarchy`, and seriates with the
real `optimal_leaf_ordering`. It writes a grouping JSON the viewer reads.

**Why a file and not a function call.** `ml-forecaster/` carries pandas, numpy,
scikit-learn, and scipy. `dca_market_graph` is a repo-root module bound by CLAUDE.md's
stdlib-only invariant, so it can never import that tree. The interface is therefore a
document. The graph always works without it; the export only makes the grouping better.

`cluster_tickers.py` is a strictly read-only consumer of `features.build_feature_frame`.
It never writes `config/ml_sizing_adjustment.json` and has no path to the guarded-publish
quality gate, so it cannot affect what the strategy actually sizes.

The grouping in use is recorded in the payload and displayed in the viewer, so a chart
never silently shows you the weaker ordering.

`--clusters` is a request the exporter must be able to honour. SciPy's `maxclust` cut is
a *maximum*, not a target: when linkage heights tie — identical or near-identical
histories, which the archive really does contain — it can return fewer groups than asked
for. `cluster_tickers.py` refuses in that case rather than reporting the smaller number
as though it were the answer, because between identical series there is no principled
place to cut and any split would be invented. Drop `--clusters` to take the automatic
count, which promises nothing and accepts whatever the cut yields. (The stdlib tier is
unaffected: `_cut_clusters` splits the loosest merge structurally, so it always reaches
the requested count.)

### The supplied-grouping contract

A grouping file is **external input** and two of its fields land directly on the ticker
axis, so one invariant covers both: **a ticker appears at most once in the grouping.**

- a repeat in `ticker_order` is placed once, first occurrence winning;
- a ticker claimed by two `clusters` entries stays with the first that named it, and an
  entry left empty by that filtering is dropped.

Neither is cosmetic. A repeated symbol drew the same series twice in one Pulse scene and
gave it two Wave rows while the subtitle counted it once; a ticker in two bands took the
*later* band's colour (the viewer's cluster lookup is last-write-wins) while *both*
legend buttons advertised it, so hiding the earlier band left it on screen in a colour
matching neither button.

Tickers present in the data but absent from the document are still appended in a trailing
`ungrouped` band rather than dropped — omitting a name from a whole-market picture would
misrepresent the market.

## Data sources

One loader interface, four implementations, covering every OHLCV store here:

| `--source` | Reads | Notes |
| --- | --- | --- |
| `csv` | `historical_stock_data.csv` | The only OHLCV file committed to git, hence the default. **Thin** — a handful of real tickers plus single-row stubs, which are dropped for having no prior close. |
| `archive` | `dca_ticker_paths.configured_ticker_history_dir()` | The primary store, thousands of symbols. Gitignored; set `$DCA_TICKER_HISTORY_DIR` outside a populated checkout. Defaults to the **active universe**, not the whole archive — pass `--all-tickers-in-archive` to override. |
| `intraday` | `<history_dir>/_intraday/` | Per-poll bars via `dca_intraday_archive.read_ticker_observations` — resolution the daily archive cannot show. |
| `run-dir` | a run's `raw/historicals_batch*.json` | Replays a completed scheduled run from the broker responses it actually decided on. Nested `{"data": {...}}` connector envelopes are unwrapped exactly as `run_dca` unwraps them. |

Two details the loaders get right that are easy to get wrong:

- **Split adjustment.** The archive stores raw OHLC beside an `Adj Close`. Read raw, a
  2-for-1 split looks like a 50% overnight collapse — one fabricated move that would set
  the shared scale for the whole scene. Every price is rescaled by the row's own
  `Adj Close / Close` ratio, the same idiom `scripts/generate_per_ticker_sizing.py` uses.
  Volume is left alone; it is a share count, not a price.
- **The prior-close baseline.** `--start` keeps one bar from *before* the window, purely
  so the first requested session has something to measure against. It is a calculation
  input and is not drawn. Without it the first session was always null and a
  single-session window resolved to nothing at all.
- **A frame exists only where something can be drawn.** A timestamp earns a place on the
  axis when at least one ticker has a bar there *with a usable prior close*. Since every
  channel is a percent move against that close, a timestamp where no ticker has one is a
  frame nobody can see — it used to open Pulse on a blank scene, report a session count
  larger than the data, and start the span before the first visible observation. This
  covers interior gaps too, not just the leading one.
- **Undrawn bars never touch the volume scale.** Volume is normalized over the bars that
  actually rendered — read off the price pass, not guessed at — because an invisible bar
  shifting the per-ticker log min/max or the whole-market mean makes the axis quietly
  untrue: two drawn sessions with identical volume land at different X positions because
  of a bar the reader cannot see. There are two ways to be undrawn and they share one
  rule: outside the window (the baseline above), and having no prior close — which is
  every series' *first* bar, one per ticker on every render, not an edge case.

The frame axis is keyed by full timestamp, not calendar date, so the intraday and
`run-dir` sources keep every poll instead of collapsing a day onto its last observation.
Daily sources stamp midnight and are unaffected; `meta.intraday` records which applies.

Intraday frame labels are emitted as `YYYY-MM-DD HH:MMZ` — the trailing `Z` is literal
and means what it says. `_parse_timestamp` converts timezone-aware inputs to UTC, so
these frames are UTC, not the Eastern trading clock every other artifact in this repo is
stamped in; unlabelled they read five hours off for most of the year. They are not
converted to Eastern because `_parse_timestamp` leaves *naive* inputs untouched, so not
every stamp arriving here is provably UTC, and relabelling an unknown zone as Eastern
would trade a vague label for a confidently wrong one. Daily labels stay bare dates.

`run-dir` and the live path are the same code: `ProviderLoader` wraps any
`near_close_dca.MarketDataProvider`, so `RobinhoodDataProvider`, `FinanceDataProvider`,
and the frozen replay provider all work through it. No MCP payload parsing is
reimplemented.

## Normalization

Volume runs to 1e8 while prices sit near 1e2, so a raw plot is a flat line against a
volume wall. Everything is normalized, and every divisor is reported in the payload so
the viewer can label axes in real units.

- **Y and Z** — percent move against the bar's prior close, divided by a *shared* scale
  set at the 98th percentile of absolute moves. Sharing one divisor across both axes and
  every ticker is what makes the scene comparable: a 3% move looks the same size whether
  it happened to NVDA or VT, and the glyph is not stretched along one axis. The
  percentile rather than the maximum means one earnings gap cannot flatten the rest of
  the market into invisibility — the outlier simply runs off the top of the scale.
- **X** — log-scaled volume, because volume is heavy-tailed. In whole-market mode it is
  **relative** volume (against the ticker's own window mean): "is this unusual for this
  name" is comparable across the universe, whereas absolute volume would just sort the
  axis by market cap. In single-stock mode (`--single`) it is a per-ticker log min-max,
  which uses the full axis for one name — and because that scale is per-ticker, `--single`
  refuses anything but exactly one resolved series. Several of them in one scene would each
  be stretched across the whole X axis on their own scale, so two glyphs at the same X
  would share nothing while the picture invites exactly that comparison.
- Bars with no resolvable prior close are dropped, never defaulted.
- Whole-market mode appends a synthetic `__MARKET__` centroid — the equal-weight mean of
  the normalized channels — so you can see instantly whether a name is moving *with* the
  market or against it.

Volume of zero means "not reported" — quote-derived bars and pre-`bar_*` intraday rows
both carry it — so those entries stay null rather than landing at 0.5, which on this
axis would claim "exactly average volume" for a ticker that reported none.

### Dip-focused charts

The exporter can produce a drawdown view with `--drawdown-window 20` or
`--drawdown-window 252`. These use the rolling high over the selected number of
trading sessions as the reference, so the high is `0%` and prices below it are negative.
For example:

```bash
python -m scripts.generate_market_graph --source csv --drawdown-window 20 \
  --out /tmp/market_graph_20d.html
python -m scripts.generate_market_graph --source csv --drawdown-window 252 \
  --out /tmp/market_graph_52w.html
```

The two files are separate charts: the 20-session view reacts to recent dips, while the
252-session view highlights larger corrections. This is a display metric only. DCA
purchase sizing continues to use the guarded prior-close ladder, which prevents a
stock merely recovering from an old high from being treated as a new selloff signal.

A useful property, and a test: multiplying every price in a series by a constant leaves
the normalized output byte-identical. A split does not move a vertex — which is exactly
why the archive loader applies the `Adj Close` ratio rather than trusting raw prints.

### Default output paths

With `--out` omitted, artifacts land under `$DCA_CRON_TRACKING_ROOT/market_graph/<span>/`,
where `<span>` is `<first>_<last>` taken from the payload's own frame axis.

That span is always a single **path component**, never a path. `--payload` documents are
written by something other than these CLIs, so `artifact_span()` reduces each label to
`[A-Za-z0-9._-]` and refuses an empty or all-dots result — otherwise a `dates` entry like
`../../escape` or `/etc/passwd` would walk out of the artifact root and create
directories elsewhere. `validate_payload_document()` additionally requires the documented
`YYYY-MM-DD` / `YYYY-MM-DD HH:MMZ` shapes, so a malformed document is *refused* rather
than silently renamed into something merely safe.

## Usage

```bash
# Works in a fresh checkout with no environment configured.
python -m scripts.generate_market_graph --source csv --out /tmp/ocean.html

# The full active universe against the real archive.
python -m scripts.generate_market_graph --source archive --tickers @config

# One stock, framed for itself.
python -m scripts.generate_market_graph --source archive --tickers NVDA --single

# Replay a completed scheduled run from its persisted broker responses.
python -m scripts.generate_market_graph --source run-dir \
    --run-dir "$DCA_CRON_TRACKING_ROOT/midday_dca/20260803_1230ET"
```

With the richer grouping:

```bash
cd ml-forecaster && python cluster_tickers.py --out /tmp/ticker_groups.json
cd .. && python -m scripts.generate_market_graph --source archive --tickers @config \
    --group-source file --groups-file /tmp/ticker_groups.json
```

The CLI always prints how many requested tickers actually resolved. That matters: the
default CSV covers a handful of symbols, and a reader who is not told would conclude the
market was quiet rather than that the data was missing.

### Offline stills and animation

`analysis/market-graph-3d/` renders the same payload with matplotlib, for dropping a
still into a document or handing someone a scrubable video. It imports the loaders and
normalization from `dca_market_graph` rather than reimplementing them, so the still and
the web page agree.

```bash
cd analysis/market-graph-3d
pip install -r requirements.txt
python render_market_graph.py --payload /tmp/payload.json --out-dir /tmp/ocean
python render_market_graph.py --source csv --mode pulse --format gif --stride 5
```

MP4 needs an `ffmpeg` binary on PATH; `--format gif` needs only Pillow.

## The emitted file

One self-contained HTML file. three.js is inlined, the payload is embedded, and there
are **zero** network requests — it opens from a `file://` URL on a machine that has never
seen this repository. That is asserted by a test, not assumed.

Viewer controls: orbit by dragging, zoom with the wheel, pan with right-drag, space to
play/pause, arrow keys to step sessions. The mode toggle switches Pulse/Wave, the legend
toggles cluster bands on and off, and hovering a glyph in Pulse reports the real
percentages behind it.

### Why three.js 0.149.0

The viewer needs a classic `<script>` that defines a global `THREE`, so the page stays a
single portable file with no import map. `build/three.min.js` is that UMD bundle; it was
deprecated at r150 and removed at r160, making 0.149.0 the last release where it is the
real build. `OrbitControls` is ESM-only from r147, so the viewer hand-rolls ~50 lines of
orbit/pan/zoom rather than vendoring a second file and reintroducing the problem.

Full provenance, checksums, and the upgrade path: `viz/vendor/README.md`.

The vendored bundle is browser-side only — read as text, pasted into a `<script>` tag,
never imported by Python — so the stdlib-only runtime invariant is untouched.

## Files

| Path | Role |
| --- | --- |
| `dca_market_graph.py` | stdlib-only: loaders, normalization, seriation, payload, HTML emission |
| `viz/market_graph.{html.tmpl,css,js}` | the browser viewer |
| `viz/vendor/` | vendored three.js + licence + provenance |
| `scripts/generate_market_graph.py` | the CLI |
| `schemas/market_graph_payload.schema.json` | the payload contract |
| `ml-forecaster/cluster_tickers.py` | the behavioral grouping exporter |
| `analysis/market-graph-3d/` | the matplotlib renderer |

Viewer assets resolve through `dca_market_graph.viewer_asset_dir()` — `$DCA_VIZ_DIR` →
`viz/` beside the module → `sys.prefix/share/all-stock-dca/viz` → `./viz` — mirroring
`dca_config.get_config_dir()`. Both halves ship in `pyproject.toml`; without the
`data-files` entries an installed wheel imports fine but cannot emit a page.

## Known limits

- **The default source is thin.** The committed CSV has a handful of real tickers, only
  four of which are in the 53-symbol active universe. "The market as a whole" stays
  sparse until `$DCA_TICKER_HISTORY_DIR` points at real data.
- **Clustering needs history.** With only a few tickers the correlation matrix means
  little. The `MIN_OVERLAP_DAYS` floor stops it inventing structure, and the viewer
  labels the grouping source so a thin run looks thin rather than authoritative.
- **Both modes clip extreme values** (`pulseClamp` / `waveClamp` in `viz/market_graph.js`)
  so one 15% gap cannot set the scale for the whole scene. Clipped peaks are visibly
  pegged at the ceiling, and the Pulse tooltip still reports the true percentages.
- **Interior gaps are carried forward, pre-history is not.** A ticker that missed a
  single session leaves the Wave surface flat there rather than tearing a hole. Sessions
  before a ticker's *first* observation are different: there is nothing to carry, so no
  geometry is emitted at all and the surface simply begins where the data does.
- **Return-correlation grouping is O(n³)** and refuses above 400 tickers rather than
  appearing to hang. Above that, narrow the universe, pass `--group-source none`, or use
  the `ml-forecaster` export, which clusters far larger universes via scipy.
