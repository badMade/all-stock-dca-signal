"""CLI: the user-facing window into a run. Reads ledgers; never re-predicts.

Views (composable with --run-dir):

  --tomorrow            the headline output: tomorrow's predicted O/H/L/C for
                        every eligible ticker, in adjusted dollars with
                        p10-p90 bands (--csv exports, --sort orders)
  --latest              the most recent SCORED day: predicted vs actual,
                        error %, inside-band and inside-range checkmarks
  --ticker T --last N   one ticker's recent prediction record over time
  --summary             the short whole-model analysis (writes analysis.md)
  --html OUT            a self-contained dashboard (inline SVG, no external
                        assets -- precedent: the repo's dca_weekly_dashboard)

Exit codes: 0 ok, 1 REFUSED (no run/ledger/checkpoint where required).
"""

from __future__ import annotations

import argparse
import bisect
import csv
import html as html_module
import json
import math
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
# Index 1, not 0 -- see run_walkforward.py's identical bootstrap comment.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(1, str(REPO_ROOT))

import metrics as metrics_module  # noqa: E402
from data import COMPONENTS  # noqa: E402
from ledger import Ledger, LedgerError  # noqa: E402
from run_walkforward import Refused, _checkpoint_state_path, write_report  # noqa: E402

from analysis import render_analysis  # noqa: E402

_MODEL_PREFERENCE = ("octopus", "gbm_quantile", "persistence")


def _open_ledger(run_dir: Path) -> Ledger:
    """A Ledger bounded to the checkpoint's fingerprinted byte offsets.

    Every inspection view reads through this instead of a bare Ledger(): an
    unrestricted reader can show records that the next resume will delete
    (bytes written after the last checkpoint), or fail decompressing a
    partial gzip member left by a crash -- write_report() already guards
    against this, and every inspect_run.py view must too (Codex review:
    "Bound inspection reads to the checkpoint fingerprints"). Uses the same
    checkpoint.prev fallback write_report()/resume() already use, for the
    narrower crash window between the two renames of checkpoint()'s swap,
    where checkpoint/ is briefly absent even though a complete checkpoint
    exists (Codex review: "Bound ledger reads using checkpoint.prev during
    swap recovery").
    """
    state_path = _checkpoint_state_path(run_dir)
    if state_path is None:
        # Ledger shards can exist before the FIRST checkpoint ever happens
        # (a run appends to the ledger during a step, then crashes before
        # checkpoint()); reading them unrestricted would accept bytes with
        # zero durability authority, showing records the next resume will
        # delete or failing on a partial gzip member -- write_report()
        # already refuses this exact case (Codex review: "Refuse inspection
        # when no checkpoint bounds the ledger").
        raise Refused(f"no checkpoint under {run_dir}; ledger bytes without a checkpoint are not proven durable")
    read_fingerprints = json.loads(state_path.read_text(encoding="utf-8"))["ledger"]
    return Ledger(run_dir, read_fingerprints=read_fingerprints)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="inspect_run.py", description="Inspect a Wondrous Octopus run.")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--tomorrow", action="store_true", help="tomorrow's OHLC forecast for all tickers")
    parser.add_argument("--latest", action="store_true", help="most recent scored day, predicted vs actual")
    parser.add_argument("--ticker", default=None, help="one ticker's recent record (with --last)")
    parser.add_argument("--last", type=int, default=30, help="days shown with --ticker")
    parser.add_argument("--summary", action="store_true", help="short whole-model analysis")
    parser.add_argument("--html", type=Path, default=None, help="write the dashboard to this path")
    parser.add_argument("--csv", type=Path, default=None, help="with --tomorrow: export the table")
    parser.add_argument("--sort", choices=("ticker", "close_return", "band_width"), default="ticker")
    parser.add_argument("--tickers", nargs="*", default=None, help="filter (and pick --html ribbon tickers)")
    parser.add_argument("--model", default=None, help="model to display (default: octopus > gbm > persistence)")
    return parser


# ---------------------------------------------------------------------------
# Shared loading
# ---------------------------------------------------------------------------


def _load_state(run_dir: Path) -> dict:
    for name in ("checkpoint", "checkpoint.prev"):
        path = run_dir / name / "harness_state.json"
        if path.exists():
            return json.loads(path.read_text())
    raise Refused(f"no checkpoint under {run_dir}")


def _load_run_meta(run_dir: Path) -> dict | None:
    meta_path = run_dir / "run_meta.json"
    return json.loads(meta_path.read_text()) if meta_path.exists() else None


def _current_universe(run_meta: dict | None) -> set[str] | None:
    """The run's CURRENT universe (`None` when unknown, e.g. no run_meta.json yet)."""
    tickers = (run_meta or {}).get("universe", {}).get("tickers")
    return set(tickers) if tickers else None


def _pick_model(available: list[str], requested: str | None) -> str:
    if requested:
        if requested not in available:
            raise Refused(f"model {requested!r} not in this run (available: {available})")
        return requested
    for candidate in _MODEL_PREFERENCE:
        if candidate in available:
            return candidate
    if not available:
        raise Refused("run has no ledgered models yet")
    return available[0]


def _bounded_log_return(log_return: float) -> float:
    # See daily_update.py's _dollar() for why this clamps rather than lets
    # math.exp() raise OverflowError on a divergent model's extreme output
    # (Antigravity review: "Uncaught OverflowError risk in _dollar()").
    # Shared by _dollar() AND the --latest percentage-error computation --
    # a SEPARATE direct math.exp() call on the same raw log-return values
    # was missed by the first pass and could still crash --latest for
    # exactly the divergent run it would be most useful to inspect (Codex
    # review: "Bound the latest-view percentage conversion").
    if math.isnan(log_return):
        return 0.0
    return max(-50.0, min(50.0, log_return))


def _dollar(log_return: float, ref_close: float) -> float:
    return ref_close * math.exp(_bounded_log_return(log_return))


#: Mirrors daily_update.py's own `_INVALID_FORECAST_MARKER`/`_dollar_cell`
#: pair. `_dollar()`'s NaN -> 0.0 clamp exists so a divergent model can't
#: crash a display conversion, but used directly for a DISPLAYED value it
#: silently turns "the model produced no usable output" into "predicted no
#: change" -- a plausible-looking, fabricated price. That contradicts the
#: daily CSV path (which emits this same marker) and the scoring path
#: (which deliberately penalizes NaN as an extreme miss) (Codex review:
#: "Mark NaN forecasts invalid in inspection views"). Used wherever a raw
#: forecast/actual log-return becomes a row value shown to the user across
#: --tomorrow, --latest, ticker history, and the HTML dashboard.
_INVALID_FORECAST_MARKER = "N/A"


def _dollar_cell(log_return: float, ref_close: float) -> object:
    if math.isnan(log_return):
        return _INVALID_FORECAST_MARKER
    return _dollar(log_return, ref_close)


def _fmt_dollar(value: object, width: int) -> str:
    """Right-justify a dollar cell that may be the invalid marker."""
    if isinstance(value, str):
        return f"{value:>{width}}"
    return f"{value:>{width}.2f}"


def _tomorrow_rows(
    state: dict, model: str, tickers: list[str] | None, universe: set[str] | None = None
) -> list[dict]:
    entries = [
        entry
        for bucket in state["outstanding"].values()
        for entry in bucket
        if entry["model"] == model
        and (tickers is None or entry["ticker"] in tickers)
        # The harness deliberately KEEPS a removed ticker's unresolved
        # forecast in `outstanding` (a capped-universe ranking swap or a
        # temporarily missing CSV might bring it back), even though
        # run_meta.json is already refreshed to the current universe.
        # Without this filter, --tomorrow and the HTML dashboard keep
        # presenting a displaced ticker as a current forecast forever, even
        # though the daily CSV path already filters it out (Codex review:
        # "Filter inspection forecasts to the current universe").
        and (universe is None or entry["ticker"] in universe)
    ]
    if not entries:
        return []
    # Each ticker's CURRENT forecast, not a single market-wide latest
    # origin date: a recovered or newly warmed-up ticker's forecast can
    # legitimately carry an older origin than the rest of the market and
    # is still that ticker's current next-day prediction -- the daily CSV
    # path (_write_next_day_forecasts) already preserves it on the same
    # principle (Codex review: "Keep each ticker's current forecast in
    # --tomorrow"). A ticker can appear at most once here in the normal
    # case; if a stale entry from an interrupted resume ever left two,
    # keep the newer one.
    current: dict[str, dict] = {}
    for entry in entries:
        existing = current.get(entry["ticker"])
        if existing is None or entry["origin_date"] > existing["origin_date"]:
            current[entry["ticker"]] = entry
    rows = []
    for entry in current.values():
        row: dict = {
            "ticker": entry["ticker"],
            "model": entry["model"],
            "origin_date": entry["origin_date"],
            "ref_close": entry["ref_close"],
            "close_return": entry["point"]["close"],
            "band_width": entry["quantiles"]["close"]["p90"] - entry["quantiles"]["close"]["p10"],
        }
        for component in COMPONENTS:
            row[f"pred_{component}"] = _dollar_cell(entry["point"][component], entry["ref_close"])
            for key in ("p10", "p50", "p90"):
                row[f"{component}_{key}"] = _dollar_cell(entry["quantiles"][component][key], entry["ref_close"])
        rows.append(row)
    return rows


def _latest_day_rows(run_dir: Path, model: str) -> list[dict]:
    """Rows for the single most recent SCORED target_date, in one streaming
    pass. A full-universe run's score history can be tens of millions of
    records; --latest only ever needs one day's worth of them, so it must
    not materialize the whole ledger into a list first (Codex review:
    "Stream inspection views instead of loading the full ledger").
    """
    best_date: str | None = None
    best_rows: list[dict] = []
    for row in _open_ledger(run_dir).iter_records(model=model, kind="score"):
        target = row["target_date"]
        if best_date is None or target > best_date:
            best_date, best_rows = target, [row]
        elif target == best_date:
            best_rows.append(row)
    return best_rows


def _ticker_tail_rows(run_dir: Path, model: str, ticker: str, last: int) -> list[dict]:
    """The last ``last`` scored days for ONE ticker, ascending by
    target_date, in one streaming pass over just that ticker's records --
    bounded by one ticker's own history (thousands of rows at most), not
    every ticker's (Codex review: "Stream inspection views instead of
    loading the full ledger"). Records can arrive out of target_date order
    (a late-rebound score matures behind the cursor), so this keeps a small
    sorted tail rather than assuming append order.
    """
    keys: list[str] = []
    tail: list[dict] = []
    for row in _open_ledger(run_dir).iter_records(model=model, kind="score"):
        if row["ticker"] != ticker:
            continue
        position = bisect.bisect_right(keys, row["target_date"])
        keys.insert(position, row["target_date"])
        tail.insert(position, row)
        if len(tail) > last:
            del keys[0]
            del tail[0]
    return tail


def _daily_close_aggregates(
    run_dir: Path, model: str
) -> tuple[list[str], list[float], list[float], list[float], list[float]]:
    """Per-day close-only aggregates for the dashboard charts, in one
    streaming pass -- coverage/MAE/market series only ever need four small
    running sums per day, not every field of every record retained at once
    (Codex review: "Stream inspection views instead of loading the full
    ledger").
    """
    totals: dict[str, list[float]] = {}  # target_date -> [n, in_band, abs_err, pred, actual]
    for row in _open_ledger(run_dir).iter_records(model=model, kind="score"):
        bucket = totals.setdefault(row["target_date"], [0.0, 0.0, 0.0, 0.0, 0.0])
        bucket[0] += 1.0
        bucket[1] += 1.0 if row["score"]["in_band"]["close"] else 0.0
        bucket[2] += row["score"]["abs_err"]["close"]
        bucket[3] += row["point"]["close"]
        bucket[4] += row["actual"]["close"]
    days = sorted(totals)
    coverage_daily = [totals[day][1] / totals[day][0] for day in days]
    mae_daily = [totals[day][2] / totals[day][0] for day in days]
    market_pred = [totals[day][3] / totals[day][0] for day in days]
    market_actual = [totals[day][4] / totals[day][0] for day in days]
    return days, coverage_daily, mae_daily, market_pred, market_actual


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


def _view_tomorrow(args: argparse.Namespace) -> int:
    state = _load_state(args.run_dir)
    available = sorted({entry["model"] for bucket in state["outstanding"].values() for entry in bucket})
    model = _pick_model(available, args.model)
    universe = _current_universe(_load_run_meta(args.run_dir))
    rows = _tomorrow_rows(state, model, args.tickers, universe)
    if not rows:
        print("no pending forecasts (run run_walkforward.py or daily_update.py first)")
        return 0
    reverse = args.sort != "ticker"
    rows.sort(key=lambda row: row[args.sort], reverse=reverse)
    # Rows can now carry DIFFERENT origin dates (a recovered/lagging
    # ticker's current forecast predates the rest of the market) -- a
    # single "predicted from <date>" header claiming one date for every
    # row would misrepresent those. Header the most current origin and
    # mark any row that's actually older, instead of asserting one date
    # for all of them.
    latest_origin = max(row["origin_date"] for row in rows)
    print(f"Tomorrow's forecast ({model}, as of {latest_origin}, adjusted $):")
    print(f"{'ticker':<8} {'open':>9} {'high':>9} {'low':>9} {'close':>9}  close band [p10 – p90]")
    for row in rows:
        stale = f"  (from {row['origin_date']})" if row["origin_date"] != latest_origin else ""
        print(
            f"{row['ticker']:<8} {_fmt_dollar(row['pred_open'], 9)} {_fmt_dollar(row['pred_high'], 9)} "
            f"{_fmt_dollar(row['pred_low'], 9)} {_fmt_dollar(row['pred_close'], 9)}  "
            f"[{_fmt_dollar(row['close_p10'], 0)} – {_fmt_dollar(row['close_p90'], 0)}]{stale}"
        )
    if args.csv:
        # Create the parent directory first, same as --html already does
        # below: naming a file under a directory that doesn't exist yet
        # raised an uncaught FileNotFoundError instead of producing the
        # requested export (Codex review: "Create the CSV output
        # directory before exporting").
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        # Explicit encoding: omitting it defers to the system locale, which
        # is not always UTF-8 (Antigravity review: "Missing explicit UTF-8
        # encoding").
        with args.csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"csv written: {args.csv}")
    return 0


def _view_latest(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.run_dir)
    model = _pick_model(ledger.models(), args.model)
    day_rows = _latest_day_rows(args.run_dir, model)
    if not day_rows:
        print("nothing scored yet")
        return 0
    latest = day_rows[0]["target_date"]
    if args.tickers:
        day_rows = [row for row in day_rows if row["ticker"] in args.tickers]
    if not day_rows:
        # A --tickers filter naming nothing on the latest day is a normal
        # empty result, not a ZeroDivisionError in the TOTAL row (Codex
        # review: "Handle an empty filtered latest-day set").
        print(f"no scored rows on {latest} match the requested tickers")
        return 0
    day_rows.sort(key=lambda row: row["ticker"])
    print(f"Most recent scored day: {latest} ({model}, adjusted $):")
    print(
        f"{'ticker':<8} {'pred close':>10} {'actual':>10} {'err %':>7}  {'in band':>7} {'in range':>8}"
    )
    hits_band = hits_range = 0
    for row in day_rows:
        ref = row["ref_close"]
        predicted = _dollar_cell(row["point"]["close"], ref)
        actual = _dollar_cell(row["actual"]["close"], ref)
        # point/actual are LOG-returns vs ref_close; the true relative
        # price error is exp(predicted - actual) - 1, not the raw log-space
        # difference -- they agree only for small misses (a 0.2 log-return
        # difference is a ~22.1% price error, not 20%) (Codex review:
        # "Compute displayed percentage error in price space"). A NaN on
        # either side already makes predicted/actual show the invalid
        # marker above; the price-space error itself is meaningless then
        # too, so it gets the same marker instead of a fabricated 0%.
        if math.isnan(row["point"]["close"]) or math.isnan(row["actual"]["close"]):
            error_pct: object = _INVALID_FORECAST_MARKER
        else:
            error_pct = 100.0 * abs(
                math.exp(_bounded_log_return(row["point"]["close"] - row["actual"]["close"])) - 1.0
            )
        in_band = row["score"]["in_band"]["close"]
        in_range = row["score"]["close_in_range"]
        hits_band += 1 if in_band else 0
        hits_range += 1 if in_range else 0
        error_pct_str = f"{error_pct:>6.2f}%" if isinstance(error_pct, float) else f"{error_pct:>7}"
        print(
            f"{row['ticker']:<8} {_fmt_dollar(predicted, 10)} {_fmt_dollar(actual, 10)} {error_pct_str}  "
            f"{'✓' if in_band else '✗':>7} {'✓' if in_range else '✗':>8}"
        )
    n = len(day_rows)
    print(f"{'TOTAL':<8} {n} tickers, in-band {hits_band}/{n} ({100 * hits_band / n:.0f}%), "
          f"in-range {hits_range}/{n} ({100 * hits_range / n:.0f}%)")
    return 0


def _view_ticker(args: argparse.Namespace) -> int:
    ledger = _open_ledger(args.run_dir)
    model = _pick_model(ledger.models(), args.model)
    rows = _ticker_tail_rows(args.run_dir, model, args.ticker, args.last)
    if not rows:
        print(f"no scored forecasts for {args.ticker}")
        return 0
    print(f"{args.ticker} — last {len(rows)} scored days ({model}, adjusted $):")
    print(f"{'date':<12} {'pred close':>10} {'actual':>10} {'band [p10 – p90]':>22} {'hit':>4}  rolling")
    hits = 0
    for index, row in enumerate(rows, start=1):
        ref = row["ref_close"]
        in_band = row["score"]["in_band"]["close"]
        hits += 1 if in_band else 0
        print(
            f"{row['target_date']:<12} {_fmt_dollar(_dollar_cell(row['point']['close'], ref), 10)} "
            f"{_fmt_dollar(_dollar_cell(row['actual']['close'], ref), 10)} "
            f"[{_fmt_dollar(_dollar_cell(row['quantiles']['close']['p10'], ref), 8)} – "
            f"{_fmt_dollar(_dollar_cell(row['quantiles']['close']['p90'], ref), 0)}] "
            f"{'✓' if in_band else '✗':>4}  {100 * hits / index:.0f}%"
        )
    return 0


def _per_ticker_skill(run_dir: Path, model: str) -> dict[str, float]:
    """Per-ticker close-MAE skill vs persistence (tickers with >= 20 scores)."""
    if model == "persistence":
        return {}
    # Two-pass streamed intersection on (ticker, origin_date), the same
    # compact-integer-key technique metrics.build_report() already uses for
    # its own aggregate-report intersection -- materializing either
    # complete score stream (or even just their (ticker, origin_date) key
    # sets) as Python objects can require many gigabytes on the documented
    # full-universe replay (tens of millions of records per model) and OOM
    # before either --summary or --html renders (Codex review: "Stream the
    # per-ticker skill intersection"). Summing each model's abs_err
    # independently would also compare different scored-date sets whenever
    # one model joined the run late or a ticker's eligibility differs
    # between them, fabricating spurious skill -- the intersection itself
    # is still required, just computed without holding every record in
    # memory at once (Codex review: "Intersect per-ticker skill
    # observations by forecast key").
    ticker_ids: dict[str, int] = {}
    date_ids: dict[str, int] = {}
    # Safely larger than any realistic calendar length (even centuries of
    # daily trading days) so ticker_id/date_id pairs never collide.
    _DATE_ID_SPACE = 10_000_000

    def _key_id(ticker: str, origin_date: str) -> int:
        ticker_id = ticker_ids.setdefault(ticker, len(ticker_ids))
        date_id = date_ids.setdefault(origin_date, len(date_ids))
        return ticker_id * _DATE_ID_SPACE + date_id

    keep: np.ndarray | None = None
    for name in (model, "persistence"):
        keys = np.fromiter(
            (
                _key_id(row["ticker"], row["origin_date"])
                for row in _open_ledger(run_dir).iter_records(model=name, kind="score")
            ),
            dtype=np.int64,
        )
        keys = np.unique(keys)  # sorts and deduplicates in one pass
        keep = keys if keep is None else np.intersect1d(keep, keys, assume_unique=True)
    if keep is None or len(keep) == 0:
        return {}
    sums: dict[str, dict[str, list[float]]] = {}
    for name in (model, "persistence"):
        for row in _open_ledger(run_dir).iter_records(model=name, kind="score"):
            key = _key_id(row["ticker"], row["origin_date"])
            position = int(np.searchsorted(keep, key))
            if position >= len(keep) or keep[position] != key:
                continue
            bucket = sums.setdefault(row["ticker"], {}).setdefault(name, [0.0, 0.0])
            bucket[0] += row["score"]["abs_err"]["close"]
            bucket[1] += 1.0
    skill: dict[str, float] = {}
    for ticker, blocks in sums.items():
        ours = blocks.get(model)
        theirs = blocks.get("persistence")
        if ours and theirs and ours[1] >= 20 and theirs[1] >= 20 and theirs[0] > 0:
            skill[ticker] = 1.0 - (ours[0] / ours[1]) / (theirs[0] / theirs[1])
    return skill


def _health(run_dir: Path, model: str) -> dict:
    ledger = _open_ledger(run_dir)
    retrained_after = metrics_module.latest_retrain_date(ledger.iter_records(model=model, kind="retrain"))
    records = ledger.iter_records(model=model, kind="diagnostic")
    return metrics_module.diagnostic_health(records, retrained_after=retrained_after)


def _load_report(run_dir: Path) -> dict:
    # Always rebuild: daily_update.py keeps appending scores to the ledger
    # after the replay's report.json was written, and this loader must not
    # keep serving replay-era counts/accuracy/coverage indefinitely just
    # because a report already exists on disk (Codex review: "Refresh
    # aggregate reports after daily updates"). write_report() is cheap
    # (streamed from the ledger) and idempotent.
    write_report(run_dir)
    path = run_dir / "report" / "report.json"
    return json.loads(path.read_text())


def _view_summary(args: argparse.Namespace) -> int:
    report = _load_report(args.run_dir)
    ledger = _open_ledger(args.run_dir)
    model = _pick_model(ledger.models(), args.model)
    run_meta = None
    meta_path = args.run_dir / "run_meta.json"
    if meta_path.exists():
        run_meta = json.loads(meta_path.read_text())
    text = render_analysis(
        report,
        run_meta=run_meta,
        per_ticker_skill=_per_ticker_skill(args.run_dir, model),
        health=_health(args.run_dir, model),
        primary=model,
    )
    report_dir = args.run_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "analysis.md").write_text(text, encoding="utf-8")
    print(text)
    print(f"written: {report_dir / 'analysis.md'}")
    return 0


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------


def _svg_line_chart(
    series: dict[str, list[float]],
    labels: list[str],
    *,
    title: str,
    target: float | None = None,
    width: int = 720,
    height: int = 200,
) -> str:
    """A minimal inline-SVG multi-line chart (no external assets)."""
    palette = ("#3b6fb6", "#c2571a", "#3f8f4f", "#7a4fa0")
    all_values = [value for values in series.values() for value in values if value == value]
    if not all_values:
        return ""
    low, high = min(all_values), max(all_values)
    if target is not None:
        low, high = min(low, target), max(high, target)
    if high == low:
        high = low + 1e-9
    pad = 36
    inner_w, inner_h = width - pad - 10, height - 30

    def x_of(index: int, count: int) -> float:
        return pad + inner_w * (index / max(1, count - 1))

    def y_of(value: float) -> float:
        return 10 + inner_h * (1 - (value - low) / (high - low))

    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" style="max-width:100%">']
    parts.append(f'<text x="{pad}" y="10" font-size="12" fill="#333">{html_module.escape(title)}</text>')
    parts.append(f'<line x1="{pad}" y1="{10 + inner_h}" x2="{width - 10}" y2="{10 + inner_h}" stroke="#999"/>')
    parts.append(
        f'<text x="2" y="{y_of(high) + 10}" font-size="10" fill="#666">{high:.3g}</text>'
        f'<text x="2" y="{y_of(low)}" font-size="10" fill="#666">{low:.3g}</text>'
    )
    if target is not None:
        y = y_of(target)
        parts.append(f'<line x1="{pad}" y1="{y}" x2="{width - 10}" y2="{y}" stroke="#3f8f4f" stroke-dasharray="4 3"/>')
    for slot, (name, values) in enumerate(series.items()):
        points = " ".join(
            f"{x_of(index, len(values)):.1f},{y_of(value):.1f}"
            for index, value in enumerate(values)
            if value == value
        )
        color = palette[slot % len(palette)]
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.5" points="{points}"/>')
        parts.append(
            f'<text x="{pad + 4 + slot * 130}" y="{height - 4}" font-size="11" fill="{color}">'
            f"{html_module.escape(name)}</text>"
        )
    if labels:
        first_label = html_module.escape(labels[0])
        parts.append(f'<text x="{pad}" y="{height - 16}" font-size="9" fill="#666">{first_label}</text>')
        parts.append(
            f'<text x="{width - 80}" y="{height - 16}" font-size="9" fill="#666">'
            f"{html_module.escape(labels[-1])}</text>"
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_ribbon(ticker: str, rows: list[dict], *, width: int = 720, height: int = 200) -> str:
    """Band ribbon (p10-p90) + p50 line + actual close dots, in dollars."""
    rows = rows[-120:]
    if len(rows) < 2:
        return ""
    # A NaN forecast/actual is invalid, not a legitimate zero-return chart
    # point -- _dollar()'s NaN clamp would otherwise plot the reference
    # close as a plausible band edge or dot, the same fabrication the table
    # views' _dollar_cell() already refuses (Codex review: "Apply invalid
    # handling to the HTML ribbon"). SVG has no numeric slot for an "N/A"
    # marker, so an invalid row is dropped from that series' point list
    # instead -- the polyline/polygon simply skips it -- rather than drawn
    # as data.
    def _valid_points(raw: list[float], ref_close: list[float]) -> list[tuple[int, float]]:
        return [
            (index, _dollar(value, ref_close[index]))
            for index, value in enumerate(raw)
            if not math.isnan(value)
        ]

    ref_closes = [row["ref_close"] for row in rows]
    p10_pts = _valid_points([row["quantiles"]["close"]["p10"] for row in rows], ref_closes)
    p50_pts = _valid_points([row["quantiles"]["close"]["p50"] for row in rows], ref_closes)
    p90_pts = _valid_points([row["quantiles"]["close"]["p90"] for row in rows], ref_closes)
    actual_pts = _valid_points([row["actual"]["close"] for row in rows], ref_closes)
    all_values = [v for _, v in p10_pts + p90_pts + actual_pts]
    if not all_values:
        return ""
    low, high = min(all_values), max(all_values)
    if high == low:
        high = low + 1e-9
    pad = 36
    inner_w, inner_h = width - pad - 10, height - 30

    def x_of(index: int) -> float:
        return pad + inner_w * (index / (len(rows) - 1))

    def y_of(value: float) -> float:
        return 10 + inner_h * (1 - (value - low) / (high - low))

    # A dropped point does not, by itself, put a gap in the rendering: SVG
    # joins whatever points remain directly, so a NaN sitting between two
    # finite values would still be interpolated across, and independently
    # filtering p10/p90 could pair up band edges from two different dates
    # in the same polygon. Only draw across INDICES where the series is
    # actually contiguous, and only build a band segment where BOTH p10
    # and p90 are valid at every index in it (Codex review: "Split ribbon
    # paths at invalid observations").
    def _contiguous_runs(indices: list[int]) -> list[list[int]]:
        runs: list[list[int]] = []
        for index in indices:
            if runs and index == runs[-1][-1] + 1:
                runs[-1].append(index)
            else:
                runs.append([index])
        return runs

    p10_by_index = dict(p10_pts)
    p90_by_index = dict(p90_pts)
    p50_by_index = dict(p50_pts)
    band_indices = sorted(set(p10_by_index) & set(p90_by_index))
    band_polygons = "".join(
        (
            f'<polygon points="{" ".join(f"{x_of(i):.1f},{y_of(p90_by_index[i]):.1f}" for i in run)} '
            f'{" ".join(f"{x_of(i):.1f},{y_of(p10_by_index[i]):.1f}" for i in reversed(run))}" '
            'fill="#3b6fb6" opacity="0.18"/>'
        )
        for run in _contiguous_runs(band_indices)
    )
    mid_polylines = "".join(
        '<polyline fill="none" stroke="#3b6fb6" stroke-width="1.5" points="'
        + " ".join(f"{x_of(i):.1f},{y_of(p50_by_index[i]):.1f}" for i in run)
        + '"/>'
        for run in _contiguous_runs(sorted(p50_by_index))
    )
    dots = "".join(f'<circle cx="{x_of(i):.1f}" cy="{y_of(v):.1f}" r="1.8" fill="#1a1a1a"/>' for i, v in actual_pts)
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img" style="max-width:100%">'
        f'<text x="{pad}" y="10" font-size="12" fill="#333">{html_module.escape(ticker)} '
        "— predicted band vs actual close "
        f"({rows[0]['target_date']} → {rows[-1]['target_date']})</text>"
        f"{band_polygons}{mid_polylines}{dots}</svg>"
    )


def _rolling(values: list[float], window: int = 20) -> list[float]:
    output = []
    for index in range(len(values)):
        chunk = values[max(0, index - window + 1) : index + 1]
        output.append(sum(chunk) / len(chunk))
    return output


def _view_html(args: argparse.Namespace) -> int:
    report = _load_report(args.run_dir)
    ledger = _open_ledger(args.run_dir)
    model = _pick_model(ledger.models(), args.model)
    state = _load_state(args.run_dir)
    meta_path = args.run_dir / "run_meta.json"
    run_meta = json.loads(meta_path.read_text()) if meta_path.exists() else None
    analysis_text = render_analysis(
        report,
        run_meta=run_meta,
        per_ticker_skill=_per_ticker_skill(args.run_dir, model),
        health=_health(args.run_dir, model),
        primary=model,
    )

    # Per-day aggregates for the charts, streamed straight off the ledger
    # (Codex review: "Stream inspection views instead of loading the full
    # ledger" -- the old path materialized every scored record for the
    # whole run just to compute these).
    days, coverage_daily, mae_daily, market_pred, market_actual = _daily_close_aggregates(args.run_dir, model)
    # Ribbon tickers must be known BEFORE fetching their rows (each fetch is
    # its own bounded streaming pass); when the caller didn't name any, fall
    # back to the run's recorded universe -- alphabetically, not "whichever
    # tickers happen to have scores", which used to require the same full
    # materialization this fix removes.
    if args.tickers:
        ribbon_tickers = list(args.tickers)[:3]
    elif run_meta and run_meta.get("universe", {}).get("tickers"):
        ribbon_tickers = sorted(run_meta["universe"]["tickers"])[:3]
    else:
        ribbon_tickers = []

    sections = ['<meta charset="utf-8"><title>Wondrous Octopus</title>']
    sections.append(
        "<style>body{font-family:system-ui,sans-serif;max-width:820px;margin:24px auto;padding:0 12px;"
        "background:#fdfdfb;color:#1a1a1a}table{border-collapse:collapse;font-size:13px}"
        "td,th{border:1px solid #ccc;padding:3px 8px;text-align:right}th{background:#eee}"
        "td:first-child,th:first-child{text-align:left}pre{white-space:pre-wrap}</style>"
    )
    sections.append("<h1>Wondrous Octopus — run dashboard</h1>")
    analysis_html = html_module.escape(analysis_text)
    sections.append(f"<h2>Model analysis</h2><pre>{analysis_html}</pre>")

    tomorrow = _tomorrow_rows(state, model, args.tickers, _current_universe(run_meta))
    if tomorrow:
        tomorrow.sort(key=lambda row: row["ticker"])
        # Rows can carry DIFFERENT origin dates (a recovered/lagging
        # ticker's current forecast predates the rest of the market) --
        # header the most current origin and mark any row that's actually
        # older, instead of asserting the alphabetically-first ticker's
        # date for every row, mirroring the text view's identical fix
        # (Codex review: "Preserve per-ticker origin dates in the HTML
        # forecast table").
        latest_origin = max(row["origin_date"] for row in tomorrow)
        sections.append(f"<h2>Tomorrow ({model}, as of {latest_origin}, adjusted $)</h2>")
        header = (
            "<tr><th>ticker</th><th>open</th><th>high</th><th>low</th><th>close</th>"
            "<th>close p10</th><th>close p90</th></tr>"
        )
        body = "".join(
            f"<tr><td>{html_module.escape(row['ticker'])}"
            + (
                f" <small>(from {row['origin_date']})</small>"
                if row["origin_date"] != latest_origin
                else ""
            )
            + "</td>"
            + "".join(f"<td>{_fmt_dollar(row[f'pred_{component}'], 0)}</td>" for component in COMPONENTS)
            + f"<td>{_fmt_dollar(row['close_p10'], 0)}</td><td>{_fmt_dollar(row['close_p90'], 0)}</td></tr>"
            for row in tomorrow[:100]
        )
        sections.append(f'<div style="overflow-x:auto"><table>{header}{body}</table></div>')

    if days:
        sections.append("<h2>Calibration and accuracy</h2>")
        sections.append(
            _svg_line_chart(
                {"rolling 20d coverage": _rolling(coverage_daily)},
                days,
                title="p10–p90 coverage vs 0.80 target",
                target=0.8,
            )
        )
        mae_series = {"rolling 20d close MAE": _rolling(mae_daily)}
        sections.append(_svg_line_chart(mae_series, days, title="close MAE (log-return)"))
        sections.append("<h2>The market as one entity</h2>")
        sections.append(
            _svg_line_chart(
                {"predicted aggregate": _rolling(market_pred, 5), "realized aggregate": _rolling(market_actual, 5)},
                days,
                title="equal-weight close return, 5d smoothed",
            )
        )

    ribbon_sections = []
    for ticker in ribbon_tickers:
        # _svg_ribbon only ever plots the last 120 points, so the tail
        # fetch below never needs more than that either.
        ticker_rows = _ticker_tail_rows(args.run_dir, model, ticker, 120)
        ribbon = _svg_ribbon(ticker, ticker_rows)
        if ribbon:
            ribbon_sections.append(ribbon)
    if ribbon_sections:
        sections.append("<h2>Band vs actual</h2>")
        sections.extend(ribbon_sections)

    regimes = report.get("by_regime", {})
    if regimes:
        sections.append("<h2>Regimes</h2>")
        header = "<tr><th>regime</th><th>model</th><th>close MAE</th><th>coverage</th><th>n</th></tr>"
        body = ""
        for regime, block in regimes.items():
            for name, model_block in block.items():
                close = model_block.get("close")
                if close:
                    body += (
                        f"<tr><td>{html_module.escape(regime)}</td><td>{html_module.escape(name)}</td>"
                        f"<td>{close['mae']:.5f}</td><td>{100 * close['coverage']:.1f}%</td><td>{close['n']}</td></tr>"
                    )
        sections.append(f'<div style="overflow-x:auto"><table>{header}{body}</table></div>')

    args.html.parent.mkdir(parents=True, exist_ok=True)
    args.html.write_text("\n".join(sections), encoding="utf-8")
    print(f"dashboard written: {args.html}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.tomorrow:
            return _view_tomorrow(args)
        if args.latest:
            return _view_latest(args)
        if args.ticker:
            return _view_ticker(args)
        if args.summary:
            return _view_summary(args)
        if args.html:
            return _view_html(args)
        parser.print_help()
        return 0
    except (Refused, LedgerError) as error:
        # iter_records() raises LedgerError (message already prefixed
        # "REFUSED", same convention as Refused) when a checkpointed shard
        # is missing, shortened, or hash-mismatched; left uncaught here,
        # --latest/--ticker/--summary/--html surfaced an uncaught traceback
        # instead of the documented clean REFUSED result (Codex review:
        # "Handle ledger integrity refusals in inspect_run").
        message = str(error)
        print(message if message.startswith("REFUSED") else f"REFUSED: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
