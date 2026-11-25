"""Phase 1 command line: run the walk-forward backtest over the local CSV price archive.

    python -m kalman_signals.backtest_cli --history-dir ../ticker_data --market SPY \\
        --universe ../config/tickers.json --non-equity QQQ,VT,XLG,AAAU \\
        --start 2024-01-01 --end 2026-10-31 --out-json /tmp/kalman.json --out-md /tmp/kalman.md

Research only (ADR Phase 1): reads ``<TICKER>.csv`` files in the archive's
``Date,Open,High,Low,Close,Adj Close,...`` layout, writes a report, and touches nothing else.
Closes are the ``Adj Close`` column and opens are scaled by the same split/dividend factor,
so a split never reads as a 50% "dip". The archive is a mirror of a vendor feed and has bad
rows (an out-of-range or yearless date, an empty or zero price); those rows are skipped and
counted per file in the report rather than failing the run, because this loader is the
research boundary -- the pure functions behind it still reject anything malformed.

The market ticker must have its own CSV. SPY is not in the archive as of 2026-11-23: either
pass ``--market-file`` pointing at a SPY CSV in the same layout kept OUTSIDE the managed
archive (the archive is the daily updater's universe, so research files are not added to
it), or pass ``--market VT`` (or another proxy that is in it). The report records the ticker
and the file that were used.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path

from kalman_signals.backtest import BUCKET_NAMES, calibration, forward_by_bucket, report, walk_forward
from kalman_signals.residual_dip import ResidualDipConfig

#: Calendar days of history loaded before --start: one 504-session window plus slack.
HISTORY_LOOKBACK_DAYS = 800
#: Read-only: the repo's live universe file, used only to list tickers when --tickers is absent.
DEFAULT_UNIVERSE = Path(__file__).resolve().parents[2] / "config" / "tickers.json"


def _number(text: str | None) -> float | None:
    try:
        value = float(text) if text not in (None, "") else None
    except ValueError:
        return None
    return value if value is not None and math.isfinite(value) and value > 0.0 else None


def load_csv(path: Path, ticker: str, first: date, last: date) -> tuple[list[dict[str, object]], int]:
    """(rows, skipped) for one archive file, keeping first <= date <= last."""
    rows: list[dict[str, object]] = []
    skipped = 0
    seen: set[date] = set()
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            try:
                d = date.fromisoformat((raw.get("Date") or "")[:10])
            except ValueError:
                skipped += 1
                continue
            if not first <= d <= last:
                continue
            close, adj, opened = _number(raw.get("Close")), _number(raw.get("Adj Close")), _number(raw.get("Open"))
            if close is None or adj is None or d in seen:
                skipped += 1
                continue
            seen.add(d)
            factor = adj / close
            rows.append({"ticker": ticker, "date": d, "close": adj,
                         "open": None if opened is None else opened * factor, "final": True})
    return rows, skipped


def load_earnings(path: Path) -> dict[str, list[date]]:
    """{ticker: [announcement dates]} from JSON: that mapping itself, or under a "dates" key."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data = data.get("dates", data) if isinstance(data, dict) else data
    if not isinstance(data, dict):
        raise ValueError("earnings file must map ticker -> list of ISO dates")
    return {str(t): sorted({date.fromisoformat(str(d)[:10]) for d in ds}) for t, ds in data.items()}


def _universe(args: argparse.Namespace) -> list[str]:
    if args.tickers:
        return [t.strip() for t in args.tickers.split(",") if t.strip()]
    data = json.loads(Path(args.universe).read_text(encoding="utf-8"))
    return [t if isinstance(t, str) else t["symbol"] for t in data["active"]]


def markdown(result: dict[str, object], market: str) -> str:
    cal = result["calibration"]
    lines = [
        "# Kalman residual-dip walk-forward report (Phase 1, research only)",
        "",
        f"- Market factor: `{market}`; sessions {result['first_session']} to {result['last_session']}"
        f" ({result['months']} months, {result['events']} scored events)",
        f"- Gate passed: **{result['gate_passed']}**",
        "",
        "| D7 check | Pass |",
        "| --- | --- |",
        *[f"| {k} | {v} |" for k, v in result["gate"].items()],  # type: ignore[union-attr]
        "",
        "## Calibration of z_total (market factor)",
        "",
        f"n={cal.get('n')}, mean={_fmt(cal.get('mean'))}, variance={_fmt(cal.get('variance'))},"  # type: ignore[union-attr]
        f" beyond 1.96={_fmt(cal.get('share_beyond_1_96'))}, lag-1={_fmt(cal.get('lag1_autocorr'))}",  # type: ignore[union-attr]
        "",
        f"## Forward {result['horizon_sessions']}-session market-adjusted log return by intraday z",
        "",
        "| Bucket | Events | Dates | Mean | SE (date-clustered) |",
        "| --- | --- | --- | --- | --- |",
    ]
    buckets = result["forward_adjusted_by_intraday_z"]
    for name in BUCKET_NAMES:
        b = buckets[name]  # type: ignore[index]
        lines.append(f"| {name} | {b['events']} | {b['dates']} | {_fmt(b['mean'])} | {_fmt(b['se_date_clustered'])} |")
    lines += ["", "## Forward market-adjusted log return by label (descriptive, not a D7 check)", "",
              "| Label | Events | Dates | Mean | SE (date-clustered) |", "| --- | --- | --- | --- | --- |"]
    for name, b in result["forward_adjusted_by_label"].items():  # type: ignore[union-attr]
        lines.append(f"| {name} | {b['events']} | {b['dates']} | {_fmt(b['mean'])} | {_fmt(b['se_date_clustered'])} |")
    earn = result.get("earnings")
    if earn:
        cal_all = earn["calibration_including_earnings"]  # type: ignore[index]
        lines += ["", f"## Earnings windows (pad {earn['pad_sessions']} sessions)", "",  # type: ignore[index]
                  f"Gate read on events OUTSIDE earnings windows; {earn['excluded_events']} events excluded"  # type: ignore[index]
                  f" ({earn['tickers_with_dates']} tickers with dates).",  # type: ignore[index]
                  f"Including them: variance={_fmt(cal_all.get('variance'))},"
                  f" beyond 1.96={_fmt(cal_all.get('share_beyond_1_96'))}."]
    else:
        lines += ["", "Earnings: no announcement dates passed; earnings days are included."]
    lines += ["", f"Labels: {result['labels']}", f"Trials: {result['trials']}", "", "Notes:"]
    lines += [f"- {n}" for n in result["notes"]]  # type: ignore[union-attr]
    return "\n".join(lines) + "\n"


def _fmt(value: object) -> str:
    return "n/a" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history-dir", required=True, type=Path)
    parser.add_argument("--market", default="SPY")
    parser.add_argument("--market-file", type=Path,
                        help="CSV for the market ticker; default <history-dir>/<market>.csv")
    parser.add_argument("--tickers", help="comma list; default: config/tickers.json active via --universe")
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--non-equity", default="", help="comma list of ETFs/funds to exclude")
    parser.add_argument("--clusters", type=Path, help="JSON {ticker: [cluster members]} from comovement_signals")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--earnings", type=Path,
                        help='JSON {ticker: [announcement dates]} (or under "dates"); excludes their windows')
    parser.add_argument("--earnings-pad", type=int, default=1, help="sessions either side of each announcement")
    parser.add_argument("--horizon", type=int, default=5)
    parser.add_argument("--out-json", required=True, type=Path)
    parser.add_argument("--out-md", type=Path)
    args = parser.parse_args(argv)

    first = args.start - timedelta(days=HISTORY_LOOKBACK_DAYS)
    market_file = args.market_file or args.history_dir / f"{args.market}.csv"
    if not market_file.is_file():
        print(f"market ticker file not found: {market_file}", file=sys.stderr)
        return 2
    rows: list[dict[str, object]] = []
    load_report: dict[str, object] = {}
    for ticker in dict.fromkeys([args.market, *_universe(args)]):
        path = market_file if ticker == args.market else args.history_dir / f"{ticker}.csv"
        if not path.is_file():
            load_report[ticker] = "missing_file"
            continue
        loaded, skipped = load_csv(path, ticker, first, args.end)
        rows.extend(loaded)
        if skipped:
            load_report[ticker] = f"skipped_rows={skipped}"
    clusters = json.loads(args.clusters.read_text(encoding="utf-8")) if args.clusters else {}
    non_equity = frozenset(t.strip() for t in args.non_equity.split(",") if t.strip())
    config = ResidualDipConfig(market_ticker=args.market)
    earnings = load_earnings(args.earnings) if args.earnings else None
    events = walk_forward(rows, args.start, args.end, config, clusters, non_equity, horizon=args.horizon,
                          earnings_dates=earnings, earnings_pad=args.earnings_pad)
    kept = [e for e in events if not e.earnings_window]
    result = report(kept, config.q_grid, horizon=args.horizon)
    if earnings is not None:
        result["earnings"] = {
            "pad_sessions": args.earnings_pad,
            "tickers_with_dates": sum(1 for ds in earnings.values() if ds),
            "excluded_events": len(events) - len(kept),
            "calibration_including_earnings": calibration(events),
            "forward_including_earnings": forward_by_bucket(events),
        }
    result["market_ticker"] = args.market
    result["market_file"] = market_file.name
    result["load"] = load_report
    result["non_equity"] = sorted(non_equity)
    args.out_json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.out_md:
        args.out_md.write_text(markdown(result, args.market), encoding="utf-8")
    print(json.dumps({"events": result["events"], "gate_passed": result["gate_passed"], "gate": result["gate"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
