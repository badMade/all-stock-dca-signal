"""First real-data run of the cost-basis harness: residual_reversal against a point-in-time
replay of the graded ladder over the ticker-history archive.

    PYTHONPATH=.:dca-sizing-signals python analysis/2026-11-24-residual-reversal-cost-basis/run_cost_basis_backtest.py \
        --history-dir "$DCA_TICKER_HISTORY_DIR" --out /tmp/rr_cost_basis.json

Research only. Reads CSVs, writes one JSON report to --out, touches no ledger (the trial rows
are in the report; ingesting them is a separate, approved step, as in
analysis/2026-11-24-kalman-phase1/record_trials.py). Lives here rather than in
dca-sizing-signals/ because it imports dca_ladder_core from the repo root, which no package
there may do (tests/test_architecture_boundaries.py).

The baseline is the 3:30 PM canonical ladder at daily granularity, and only that:

  * decision price = the session's close (the live slot sees a near-close quote);
  * base = $2 below the prior close AND the prior low, $1 below the prior close only, else no
    buy (dca_ladder_core.select_base_usd, config/sizing_ladder.json's bases);
  * multiplier = the stepped global ladder (dca_ladder_core.graded_multiplier, no ML nudge,
    no kill switch, no per-ticker calibration);
  * NOT modelled: the 60-minute down-trend filter (no intraday bars), budget caps and tier
    escalation, the other six slots, no-buy windows, and whole-share accumulation.

Every variant below was declared before the first run (docs/adr-factor-residual-dip-signals.md
rule 6: every variant is a trial). Do not add one after seeing results without counting it.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

import dca_ladder_core  # noqa: E402  (repo root on PYTHONPATH)
from cost_basis_backtest import (  # noqa: E402
    BacktestConfig,
    SuggesterCandidate,
    compare_variants,
    run_backtest,
    trial_row,
)
from residual_reversal import (  # noqa: E402
    ResidualReversalConfig,
    ResidualReversalInputs,
    ResidualReversalSuggester,
)

EXPERIMENT = "residual_reversal_cost_basis"
# Frozen before the first run. The default is the ADR's untuned configuration; the two slope
# variants exist so the deflated Sharpe and PBO have more than one trial to deflate against.
VARIANTS: dict[str, ResidualReversalConfig] = {
    "default": ResidualReversalConfig(),
    "slope_0.05": ResidualReversalConfig(slope=0.05),
    "slope_0.2": ResidualReversalConfig(slope=0.2),
}
FIXED_NOW = datetime(2026, 11, 24, 21, 0, tzinfo=timezone.utc)  # generated_at only


# --- archive ---------------------------------------------------------------------------------

def load_bars(history_dir: Path, ticker: str, first: date, last: date) -> list[tuple[date, float, float]]:
    """(date, close, low) rows within [first, last], oldest first. Rows with an unparseable
    date, a year outside 1990-2100 (the archive has yearless rows that parse to year 1 --
    see the CBU.csv note), or a missing/non-positive close or low are dropped. ``Close`` is
    split-adjusted but not dividend-adjusted, which is the price a buy actually paid."""
    path = history_dir / f"{ticker}.csv"
    if not path.exists():
        return []
    out: dict[date, tuple[float, float]] = {}
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                d = date.fromisoformat((row.get("Date") or "")[:10])
                close, low = float(row["Close"]), float(row["Low"])
            except (ValueError, KeyError, TypeError):
                continue
            if not (1990 <= d.year <= 2100) or not (first <= d <= last):
                continue
            if not (math.isfinite(close) and math.isfinite(low) and close > 0 and low > 0):
                continue
            out[d] = (close, low)
    return [(d, *out[d]) for d in sorted(out)]


# --- baseline --------------------------------------------------------------------------------

def load_ladder(path: Path) -> tuple[list[tuple[float, float]], float, float]:
    cfg = json.loads(path.read_text())
    tiers = [(float(t["min_drop_pct"]) / 100.0, float(t["multiplier"])) for t in cfg["tiers"]]
    return tiers, float(cfg["base_amount_usd"]), float(cfg["base_amount_below_close_usd"])


def ladder_baseline(bars_by_ticker: dict[str, list[tuple[date, float, float]]], start: date, end: date,
                    tiers: list[tuple[float, float]], full_base: float,
                    reduced_base: float) -> dict[date, dict[str, float]]:
    """{session: {ticker: usd}} for every session in [start, end] where the ladder buys.
    Uses only the session's own close and the previous bar's close and low, so the baseline
    never reads a later price. A gap (missing previous bar) compares against the last bar
    before it, as the live slot compares against the last regular-session close."""
    out: dict[date, dict[str, float]] = {}
    for ticker, bars in bars_by_ticker.items():
        for (_, prev_close, prev_low), (d, close, _) in zip(bars, bars[1:], strict=False):
            if not (start <= d <= end):
                continue
            base, _reason = dca_ladder_core.select_base_usd(close, prev_close, prev_low,
                                                            full_base=full_base, reduced_base=reduced_base)
            if base <= 0:
                continue
            mult = dca_ladder_core.graded_multiplier((prev_close - close) / prev_close, tiers)
            if mult > 0:
                out.setdefault(d, {})[ticker] = round(base * mult, 2)
    return dict(sorted(out.items()))


# --- candidate -------------------------------------------------------------------------------

def residual_reversal_candidate(benchmark_by_ticker: dict[str, str], config: ResidualReversalConfig,
                                name: str) -> SuggesterCandidate:
    """Same binding as dca-sizing-signals/tests/cost_basis_backtest/_binding.py."""
    lookback = config.beta_window + config.half_life_lookback + 15

    def build(view, buys):
        tickers = sorted(set(buys) | {benchmark_by_ticker[t] for t in buys if t in benchmark_by_ticker})

        def load_inputs(asof):
            return ResidualReversalInputs(
                close_rows=view.close_rows(tickers, lookback_sessions=lookback),
                proposed_buys_usd_by_ticker=dict(buys),
                benchmark_by_ticker=dict(benchmark_by_ticker),
                events_by_ticker=view.events_by_ticker(sorted(buys)),
            )

        return ResidualReversalSuggester(load_inputs, config=config, clock=lambda: FIXED_NOW)

    return SuggesterCandidate(name, build)


# --- main ------------------------------------------------------------------------------------

def _headline(summary: dict) -> dict:
    out = {}
    for period in ("in_sample", "out_of_sample", "all"):
        p = summary["metrics"]["periods"].get(period)
        if not p:
            continue
        agg = p["cost_basis"]["aggregate"]
        out[period] = {
            "cost_basis_improvement_bp_mean": agg.get("mean_bp"),
            "cost_basis_improvement_bp_median": agg.get("median_bp"),
            "cost_basis_improvement_bp_dollar_weighted": agg.get("dollar_weighted_mean_bp"),
            "share_of_tickers_improved": agg.get("share_improved"),
            "cross_ticker_t": agg.get("t_stat"),
            "n_tickers": agg.get("n_tickers"),
            "terminal_wealth_difference_bp": p["terminal_wealth_per_dollar"].get("difference_bp"),
            "tilt_forward_return": p["tilt_forward_return"],
            "hit_rate": p["hit_rate"],
            "turnover": p["turnover"],
            "worst_rolling_underperformance_bp": p.get("worst_rolling_underperformance_bp"),
            "active_daily_sharpe": p.get("active_daily_sharpe"),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--history-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sector-map", type=Path, default=HERE / "sector_map_draft.json")
    ap.add_argument("--tickers", type=Path, default=REPO / "config" / "tickers.json")
    ap.add_argument("--ladder", type=Path, default=REPO / "config" / "sizing_ladder.json")
    ap.add_argument("--start", type=date.fromisoformat, default=date(2021, 1, 4))
    ap.add_argument("--oos-start", type=date.fromisoformat, default=date(2024, 1, 2))
    ap.add_argument("--end", type=date.fromisoformat, default=date(2026, 11, 23))
    ap.add_argument("--warmup-start", type=date.fromisoformat, default=date(2020, 1, 2))
    ap.add_argument("--buy-cost-bps", type=float, default=0.0,
                    help="one-way buy cost; Robinhood charges no commission, so spread only")
    ap.add_argument("--variants", nargs="*", default=list(VARIANTS))
    args = ap.parse_args(argv)

    sector = json.loads(args.sector_map.read_text())
    active = list(json.loads(args.tickers.read_text())["active"])
    missing_map = sorted(set(active) - set(sector["etf_by_ticker"]))
    if missing_map:
        print(f"tickers absent from the sector map (left unmapped): {missing_map}", file=sys.stderr)
    bench = {t: e for t, e in sector["etf_by_ticker"].items() if e and t in active}
    etfs = sorted(set(bench.values()))

    bars = {t: load_bars(args.history_dir, t, args.warmup_start, args.end) for t in active + etfs}
    no_data = sorted(t for t in active + etfs if not bars[t])
    if any(e in no_data for e in etfs):
        raise SystemExit(f"sector ETF history missing: {[e for e in etfs if e in no_data]}")
    tiers, full_base, reduced_base = load_ladder(args.ladder)
    buys = ladder_baseline({t: bars[t] for t in active if bars[t]}, args.start, args.end, tiers,
                           full_base, reduced_base)
    rows = [{"ticker": t, "date": d.isoformat(), "close": c} for t, bs in bars.items() for d, c, _ in bs]
    n_buys = sum(len(v) for v in buys.values())
    print(f"{len(buys)} sessions, {n_buys} baseline buys, ${sum(sum(v.values()) for v in buys.values()):,.0f}; "
          f"{len(bench)} mapped tickers on {len(etfs)} ETFs; no data: {no_data}", file=sys.stderr)

    cfg = BacktestConfig(oos_start=args.oos_start, buy_cost_bps=args.buy_cost_bps)
    results, report = {}, {"experiment": EXPERIMENT, "inputs": {
        "history_dir_tickers": len(active), "no_data": no_data, "sector_map_version": sector["version"],
        "mapped": len(bench), "etfs": etfs, "start": args.start.isoformat(), "oos_start": args.oos_start.isoformat(),
        "end": args.end.isoformat(), "sessions": len(buys), "baseline_buys": n_buys,
        "baseline_usd": round(sum(sum(v.values()) for v in buys.values()), 2), "buy_cost_bps": args.buy_cost_bps,
        "events": "none supplied (no offline 8-K or earnings source), so the corporate_event guard never fires",
    }, "variants": {}}
    for name in args.variants:
        print(f"running {name} ...", file=sys.stderr, flush=True)
        r = run_backtest(residual_reversal_candidate(bench, VARIANTS[name], name), rows, buys, config=cfg)
        results[name] = r
        s = r.summary()
        report["variants"][name] = {"status": s["status"], "reason": s["reason"], "headline": _headline(s)
                                    if r.status == "ok" else None, "promotion": s["promotion"],
                                    "lookahead_flags": s["lookahead_flags"], "summary": s}
        if r.status == "ok":
            report["variants"][name]["trial_row"] = trial_row(
                r, trial_id=f"rr-cb-2026-11-24-{name}", experiment=EXPERIMENT,
                config={"variant": name, **{k: getattr(VARIANTS[name], k) for k in ("slope", "entry")},
                        "sector_map": sector["version"]}, evaluated_at=FIXED_NOW)
        print(json.dumps({name: report["variants"][name]["headline"]}, indent=1, default=str), file=sys.stderr)
    ok = {n: r for n, r in results.items() if r.status == "ok"}
    if len(ok) >= 2:
        c = compare_variants(ok, start=args.oos_start)
        report["comparison"] = {"best": c.best, "sharpes": dict(c.sharpes), "n_trials": c.n_trials,
                                "deflated_sharpe": c.deflated_sharpe,
                                "pbo": None if c.pbo is None else c.pbo.pbo,
                                "n_sessions": c.n_sessions, "notes": list(c.notes)}
    args.out.write_text(json.dumps(report, indent=1, default=str, allow_nan=False))
    print(f"wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
