# Purchase history ledger

`scripts/purchase_history.py` builds one consolidated, human-and-machine-readable
record of every confirmed buy the strategy has ever placed, across every stream
and every day: ticker, fill price, quantity, dollar amount, order id, which
stream placed it, and whether the run was scheduled or manual.

## Why this exists

Every run directory under `cron_tracking/` already records its own confirmed
fills (`filled_orders.csv` / `orders.csv`), but that evidence is scattered
across dozens of per-run, per-stream directories with no single place that
answers "what have we bought, and at what price, over the life of the
strategy?" This script builds that place by scanning the whole tree.

It is a **read-only, offline reporting tool**. It does not participate in any
trading decision, gate, or budget calculation, and it is never on the live buy
path — the same category as `backup_strategy.py` / `drift_detection.py`. Run
it by hand whenever you want an updated ledger, or wire it into a periodic
(e.g. end-of-day) reporting job.

## Usage

```bash
python -m scripts.purchase_history
# or, to scan/write somewhere other than the default cron-tracking root:
python -m scripts.purchase_history --cron-tracking-root /path/to/cron_tracking --output-dir /path/to/reports
# or, when a stream's live artifact root has been relocated outside the
# cron-tracking tree (see Limitations below), scan it too:
python -m scripts.purchase_history --additional-root /path/to/relocated_market_open_root
```

Defaults to `dca_paths.cron_tracking_root()` for both scanning and output (so
running it with no arguments in a configured deployment finds the real
artifact tree via `$DCA_CRON_TRACKING_ROOT`, per `docs/live_wiring.md`).
`--additional-root` is repeatable; pass one per relocated stream root.

## Output

Two files, written into the output directory (default: the scanned root):

* **`purchase_history.csv`** — one row per confirmed fill:
  `trading_day, ticker, quantity, avg_fill_price, amount_usd, order_id,
  stream, run_origin, run_id, broker, source_run_dir`. `amount_usd` is
  `avg_fill_price * quantity_filled` (the executed notional, matching this
  repo's own `total_filled_notional` reporting convention), rounded to 6
  decimal places — not 2 — whenever both resolved: the per-ticker rollup sums
  `amount_usd` first and rounds only the ticker total at the end, so rounding
  each row to cents here would compound per-fill rounding error into the
  published totals. The standard full-report CSV's raw recorded amount is the
  REQUESTED notional, not a recomputed executed one, and this ledger prefers
  the true executed figure over it. Falls back to the CSV's resolved amount
  only when price or quantity is unavailable (a documented amount-only
  shape) — with one exception: a PARTIAL execution with a resolved quantity
  but no resolved price never falls back to the CSV's raw amount either,
  since `near_close_dca._orders_csv_fills` only recomputes executed cash for
  a partial row when both price and quantity are valid, so an unpriced
  partial's raw `amount_usd` is still the requested notional the shared
  parser's own row-level comment documents, not executed cash. `amount_usd`
  is left `null` for that row rather than publishing a figure that would
  overstate invested dollars; `quantity` (a real, confirmed executed count)
  is still preserved.
* **`purchase_history_by_ticker.json`** — a per-ticker rollup: total shares
  bought, total invested, average cost basis, buy count, and first/last buy
  date.

A field the underlying fill record cannot resolve (a genuinely amount-only or
price-only confirmed fill — a documented, real shape; see
`near_close_dca._orders_csv_fills`) is written empty/`null`, never a
fabricated zero, so it cannot silently skew the average-cost-basis math.

## How it stays correct

Rather than re-parsing `filled_orders.csv`/`orders.csv` a second, potentially
differently-behaving way, this module calls
`near_close_dca._orders_csv_fills(run_dir)` — the exact same hardened
fill-parsing logic every same-day top-up guard and reporting path in this repo
already relies on, including its handling of partial fills, the broker-native
recovery column shapes (`average_fill_price`/`quantity` vs. the canonical
`avg_fill_price`/`quantity_filled`), and rows with no `status` column at all
(manual override shapes). This avoids adding a fourth CSV reader that could
drift from the other three.

Records are deduplicated by broker `order_id`, scoped to the executing
broker, across run directories (a recovery rerun or manual-exclusion copy of
the same fill is counted once, not twice). Scoping to broker matters because
an order_id is only guaranteed unique WITHIN the broker that issued it — this
repo supports more than one (`near_close_dca.AlpacaBroker` alongside
`RobinhoodBroker`), and nothing prevents two different brokers from
generating the same order_id string independently, which an unscoped dedup
key could collapse into one row. A run state predating the `broker` field (or
recording it as something other than a string) falls back to `None` rather
than a fabricated broker name, so legacy states still dedup against each
other exactly as before broker-scoping existed. A confirmed fill with no
`order_id` at all — a real, older shape some manual/recovery
`filled_orders.csv` files predate — is kept, not dropped; its dedup key is
scoped to its own run directory instead. Attribution (which run gets credit)
and fill data (quantity/price/amount) are resolved on separate axes when a
duplicate spans directories: the non-manual/scheduled occurrence is credited
as the origin, while the more complete (non-partial) occurrence's numbers
win — so a stale partial snapshot next to the run that placed an order never
overrides a later completed snapshot's quantity.

The scan walks every `run_state*.json` under the cron-tracking root, not just
the exact `run_state.json` name, to pick up the documented archived/manual
variants (`run_state_scheduled.json`, `run_state_partial.json`, etc.) the same
way `dca_risk.py` / `scripts/run_classification.py` / `dca_weekly_dashboard.py`
already do.

For each run it also checks the enclosing `*_recovery_*` directory (via
`near_close_dca._recovery_root_for`) when it differs from the directory
`run_state.json` itself lives in — the same case a recovery run that writes
`artifacts/run_state.json` beneath a recovery directory while its confirmed
`filled_orders.csv` sits at that directory's own root, with no
`artifacts.orders_csv_path` recorded to point at it. Unlike the same-day
top-up guard, this ledger does NOT merge those fills with
`near_close_dca._merge_fill` — that helper blends two distinct orders for one
ticker into a single allocation-oriented aggregate with `order_id=None`,
which is correct for "how much capital is deployed" but wrong for a
per-order ledger (it would understate `buy_count` and lose the order_id
identity a later duplicate needs to dedup against). Instead, the nested and
enclosing-root fills are fed through the same per-order dedup-by-`order_id`
logic described above: a genuine cross-directory duplicate (same order_id)
is reconciled there, and two distinct orders for one ticker are kept as two
separate rows.

After the `run_state*.json`-keyed scan above, a second pass sweeps every
scanned root for `filled_orders.csv` / `orders.csv` / `submitted_orders.csv`
**and** the full-report writer's custom-named
`dca_<label>_<day>_<stamp>_orders.csv` (`run_and_report`'s
`generate_slippage_report` writes that file strictly before
`write_run_state` persists `run_state.json`, so an interruption between the
two leaves exactly this shape orphaned) — files whose directory has no
successfully-parsed `run_state*.json` of its own. This is the ledger's
equivalent of the orphan-artifact sweep
`scripts/generate_credit_usage_proxy_report.py` already runs for its own
reporting purpose, generalized here across every scan root (including
`--additional-root` entries) rather than a fixed list of known manual
subroots. A directory whose `run_state*.json` exists but fails to parse is
still swept (only a directory with a run state that actually parsed is
considered "covered" and skipped), matching that precedent's semantics: a
persisted CSV should not disappear from the ledger just because its sibling
state file is corrupt. A parse failure on the orphan CSV itself (unreadable,
headerless, or a truncated final row) is reported as a warning the same way
the run-state and recovery readers already report theirs, rather than
failing soft to an empty mapping with no signal at all.

An orphan fill is recorded with honestly-unknown provenance — `stream`,
`run_origin`, and `run_id` are `null`, and `trading_day` is recovered from a
`YYYY-MM-DD` or `YYYYMMDD` date stamp checked against each path component
from the run's own directory outward (never the whole joined path, which
would otherwise match an OUTER date first — e.g. a dated backup/archive root
like `/backups/2026-08-10/cron_tracking/manual/20260701_fix` must report
`2026-07-01`, the run's own date, not `2026-08-10`) — or left empty when no
component carries one. When the full-report shape is what's being scanned,
the specific matched CSV FILE (not just its enclosing directory) is checked
for a date first, since `generate_slippage_report` always stamps one into
the filename even when the CSV's own directory is an undated shared root
(e.g. `manual_reports/dca_daily_2026-08-05_..._orders.csv`). `broker` is
`null` unless the fill's order_id uniquely matches an order_id already
attributed to exactly one known broker (from anywhere in the scan, not only
the main pass — a legacy/full-report `run_state.json` omits `broker`
entirely, so the "unknown" side of a real duplicate isn't only ever an
orphan copy, and can be discovered before OR after its broker-attributed
twin depending on scan order), in which case it is reconciled into that same
row after the full scan completes rather than surviving as a second,
double-counted one; a genuine collision across more than one known broker is
kept as its own row (broker namespaces are independent, so this is not
necessarily wrong) with a warning calling out the ambiguity. An orphan's
attribution can never win a dedup tie against a directory whose
`run_state.json` actually parsed, even a `manual` one — unknown provenance
ranks worse than every real classification, not merely "middle of the
pack". A warning also notes every orphan inclusion so a consumer can
distinguish a fully-attributed row from a recovered one. Beyond the broker
reconciliation above, an orphan fill is still deduplicated by the same
broker-scoped `order_id` logic as every other fill, so two orphan copies of
the same repair artifact are not double-counted either — including two
DISTINCT full-report CSVs sharing one undated output directory (each run
gets its own uniquely-timestamped filename, so both are discovered and kept
as separate rows, not collapsed to only the first found). A legacy report
with no `order_id` column at all is scoped to its own specific FILE, not
merely its enclosing directory, for exactly this reason: two such reports
sharing one directory, each confirming a fill for the same ticker, would
otherwise collide into a single row and silently discard one of two real
purchases. A directory holding an orphan canonical artifact (e.g. a bare
`filled_orders.csv` with no `run_state.json`) does not suppress a full-report
CSV discovered beside it either — the two filename sets never overlap, so
there is no double-read risk to guard against by treating one as covering
the other.

A non-recovery full-report `run_state.json` can also record
`artifacts.orders_csv_path` pointing at a CSV OUTSIDE its own run directory
entirely (the full-report writer's layout doesn't guarantee co-location).
That external directory is tracked as the fill's real source, and the
SPECIFIC referenced file (not its whole enclosing directory) is marked
covered, so the orphan sweep never re-reads the same physical file under a
different label and double-counts it — while a genuinely different,
orphaned `dca_*_orders.csv` from an unrelated interrupted run can still be
discovered if it happens to share that same output directory (full-report
output directories are explicitly shared across runs, and `run_and_report`
accepts `output_dir` and `state_path` independently — even a directory
whose OWN `run_state.json` parsed successfully can still hold a completely
unrelated full-report CSV from a different interrupted run, and the
full-report orphan pass discovers that CSV too rather than treating the
whole directory as spoken for). If the recorded path has gone stale (the
run's artifacts were archived/relocated after the state was written), the
recorded file's own basename is tried in the run's directory before falling
back further, since an archive move often keeps a custom-named CSV sitting
right beside the relocated `run_state.json`.

The full-report filename's trading-day segment (`dca_<label>_<day>_<stamp>_orders.csv`)
is parsed by its exact, fixed suffix shape rather than a generic first-match
date search, since `<label>` is unrestricted free text a caller can supply
(e.g. a replay run labeled with its own date) and could otherwise be
mistaken for the definitive date that always follows it in the filename.

An orphan CSV's rows are also checked for `near_close_dca.DryRunBroker`'s
synthetic `DRY-RUN`-prefixed order IDs and excluded: a full-report dry run
interrupted after its order CSV was written but before `dry_run=true` was
persisted to `run_state.json` would otherwise have its simulated fills
counted as real purchases, since the orphan sweep has no `run_state.json`
left to check that flag against.

Every scan root (`--cron-tracking-root` plus every `--additional-root`) is
resolved to an absolute, symlink-free path and deduplicated before
scanning, so the same physical directory reachable through two different
path spellings is walked once, not twice.

A supported market-open/intraday recovery can store its confirmed evidence
at the enclosing recovery root ONLY as `submitted_orders.csv` (with a
broker-native `state=filled` row) rather than `filled_orders.csv`/
`orders.csv`; the recovery-root read applies the identical bare-sibling
fallback the primary run directory's own read already does, rather than
silently returning no fills — the orphan sweep above cannot rescue this case
on its own, since the recovery root is marked "covered" as soon as it is
checked, regardless of whether that check actually found any fills.

## Direct execution

`scripts/purchase_history.py` also runs as `python scripts/purchase_history.py`
(not only via the documented `python -m scripts.purchase_history` form): it
inserts the repository root onto `sys.path` before importing `dca_fileio`/
`dca_paths`, the same bootstrap `scripts/drift_detection.py` carries for the
identical reason (direct invocation puts only `scripts/`, not the repo root,
on `sys.path`).

## Limitations

* This is bought-quantity accounting only. It does not know about dividends,
  splits, or sells (the strategy never sells — see `docs/strategy.md`), and it
  is not a substitute for the broker's own tax-lot / cost-basis records.
* It rebuilds both output files from a full scan every run rather than
  appending incrementally. Re-scanning the whole `cron_tracking/` tree is
  cheap relative to a daily reporting cadence, and a full rebuild can never
  drift from a partially-applied incremental update.
* `near_close_dca._orders_csv_fills` returns one fill per ticker per CSV: if a
  single order artifact records two genuinely distinct orders for the same
  ticker within one run (e.g. a retry-created incremental order), only the
  last row survives and the ledger understates that ticker's quantity/invested
  dollars for that run. Fixing this means either changing the shared,
  hardened parser the live same-day top-up guard also depends on, or adding a
  second, differently-shaped CSV reader — both bigger, dedicated changes this
  read-only reporting tool defers rather than risking the live guard's
  behavior or introducing a fourth drifting parser.
* `purchase_history.csv` and `purchase_history_by_ticker.json` are each
  individually atomic (temp file + `os.replace`, so neither is ever observed
  torn or truncated), but the PAIR is not: a crash between the two swaps
  leaves a reader seeing a new CSV alongside the previous JSON (or vice
  versa) until the next full rebuild. True cross-file atomicity needs a
  primitive this repo doesn't have anywhere yet (a versioned-directory +
  pointer swap, or a two-file transaction helper) and would also change the
  fixed output filenames this doc promises consumers read directly today —
  a bigger, dedicated interface change rather than a fix scoped to this
  ledger.
* A single scan root misses a stream whose live artifact root has been
  relocated outside the cron-tracking tree entirely (`morning`/`midday` via
  `artifact_paths.live_run_state_root`, `intraday`/`market-open` via
  `DCA_INTRADAY_LIVE_STATE_ROOT` / `DCA_MARKET_OPEN_LIVE_STATE_ROOT`).
  `--additional-root` (repeatable) lets a caller who knows their deployment's
  actual roots include them; this module does not auto-discover them via
  `run_dca._resolve_cross_stream_prior_allocation_roots`, since that function
  is built around the afternoon top-up guard's own root-trust rules and would
  pull in config-loading machinery this reporting tool otherwise avoids.
* A normal live `MorningDCAExecutor` order (used by both the morning and
  midday streams) never writes a `filled_orders.csv`: `write_artifacts()`
  writes only `submitted_orders.csv` (status reflects placement, not the
  fill) and an `orders.csv` with blank `avg_fill_price`/`quantity_filled`
  columns by design. `near_close_dca._orders_csv_fills` correctly rejects
  those as unconfirmed, so this ledger currently has no source of ordinary
  confirmed morning/midday fills at all -- not an edge case, but the normal
  case for those two streams. Closing this needs either an authoritative
  post-fill reconciliation artifact this ledger could read (see
  `scripts/broker_reconciliation.py`) or reconciling the persisted order IDs
  against the broker before publishing -- a real integration with a
  reliability engine this repo has, not a change to this ledger's own
  parsing logic, and deserving its own dedicated review of that engine's
  guarantees rather than a guess bundled into this PR.
* A run interrupted after its `run_state.json` was written but before its
  order CSV was (e.g. a compact run killed between `_finalize_and_write_artifacts`
  and `run_daily_compact` writing the sibling `orders.csv`) is indistinguishable
  from a genuine zero-orders day: `near_close_dca._orders_csv_fills` returns an
  empty mapping either way, and only actual read/parse failures on an
  *existing* file reach `skip_sink`/this ledger's warnings, not a file that
  was never written at all. Detecting this specific case would mean cross-
  checking the run state's own `buy_candidates`/outcome fields for
  submitted-or-filled evidence against the absence of any order artifact —
  a new heuristic with no existing pattern in this repo to mirror, so it's
  left as a documented gap rather than an invented check that could
  misfire.
