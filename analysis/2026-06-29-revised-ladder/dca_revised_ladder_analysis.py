from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path

import pandas as pd


# Data/output directory. Override with DCA_LADDER_DIR; defaults to this script's own
# folder so the analysis is portable rather than tied to one machine's absolute path.
BASE = Path(os.environ.get("DCA_LADDER_DIR", Path(__file__).resolve().parent))
OUT = BASE
ACTIVE_TICKERS = [
    "DFTX", "TRIN", "AAAU", "SPCX", "TRLV", "RVI", "VCX", "EVR", "LUNR", "IRDM",
    "DXYZ", "LITE", "ASML", "DRAM", "STX", "MSFT", "AAPL", "AMAT", "AMD", "CBRS",
    "MRVL", "BTX", "STM", "SNDK", "WDC", "TSM", "AVGO", "NVDA", "QBTS", "FLKR",
    "INTC", "GOOGL", "GOOG", "AMZN", "GLW", "MA", "GS", "MU", "NFLX", "XLG",
    "TOPT", "QTOP", "SATA", "RGTI", "IBM",
]

BASE_AMOUNT = 1.0
DAILY_CAP = 120.0
WEEKLY_CAP = 600.0


def tier_for_drop(drop_pct: float) -> tuple[float, str]:
    if pd.isna(drop_pct) or drop_pct < 0.1:
        return 0.0, "0x"
    if drop_pct < 2.0:
        return 1.0, "1x"
    if drop_pct < 5.0:
        return 1.5, "1.5x"
    if drop_pct < 8.0:
        return 2.0, "2x"
    return 3.0, "3x"


def symbol_from_history_filename(path: Path) -> str:
    return path.name.split("_price_history_", 1)[0]


def load_history() -> pd.DataFrame:
    frames = []
    for path in sorted(BASE.glob("*_price_history_*.csv")):
        symbol = symbol_from_history_filename(path)
        if symbol not in ACTIVE_TICKERS:
            continue
        df = pd.read_csv(path)
        if df.empty:
            continue
        df["symbol"] = symbol
        df["date"] = pd.to_datetime(df["date"]).dt.date
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        frames.append(df)
    if not frames:
        raise RuntimeError("No history files found.")
    hist = pd.concat(frames, ignore_index=True).sort_values(["symbol", "date"])
    hist["prev_close"] = hist.groupby("symbol")["close"].shift(1)
    hist["drop_pct"] = (hist["prev_close"] - hist["close"]) / hist["prev_close"] * 100
    tiers = hist["drop_pct"].apply(tier_for_drop)
    hist["multiplier"] = [x[0] for x in tiers]
    hist["tier"] = [x[1] for x in tiers]
    hist["planned_spend"] = hist["multiplier"] * BASE_AMOUNT
    return hist


def completed_last_30(hist: pd.DataFrame) -> pd.DataFrame:
    completed = hist[hist["date"] < pd.to_datetime("2026-06-29").date()].copy()
    all_dates = sorted(completed["date"].dropna().unique())
    last_30_dates = all_dates[-30:]
    return completed[completed["date"].isin(last_30_dates)].copy()


def apply_budget(decisions: pd.DataFrame) -> pd.DataFrame:
    rows = []
    week_spend: dict[tuple[int, int], float] = {}
    for date, group in decisions.sort_values(["date", "multiplier", "drop_pct"], ascending=[True, False, False]).groupby("date"):
        iso = pd.Timestamp(date).isocalendar()
        week_key = (iso.year, iso.week)
        remaining_daily = DAILY_CAP
        remaining_weekly = WEEKLY_CAP - week_spend.get(week_key, 0.0)
        for row_dict in group.to_dict('records'):
            planned = float(row_dict["planned_spend"])
            allowed = min(planned, max(0.0, remaining_daily), max(0.0, remaining_weekly))
            row_dict["budgeted_spend"] = allowed
            row_dict["budget_capped"] = planned > allowed + 1e-9
            if allowed > 0 and row_dict["close"] > 0:
                row_dict["graded_shares"] = allowed / row_dict["close"]
            else:
                row_dict["graded_shares"] = 0.0
            remaining_daily -= allowed
            remaining_weekly -= allowed
            week_spend[week_key] = week_spend.get(week_key, 0.0) + allowed
            rows.append(row_dict)
    return pd.DataFrame(rows)


def build_backtest(hist_30: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    hist_30 = hist_30.sort_values(["date", "symbol"]).copy()
    hist_30["flat_spend"] = BASE_AMOUNT
    hist_30["flat_shares"] = hist_30["flat_spend"] / hist_30["close"]
    budgeted = apply_budget(hist_30)
    tier_counts = (
        hist_30.pivot_table(index="symbol", columns="tier", values="date", aggfunc="count", fill_value=0)
        .reindex(columns=["0x", "1x", "1.5x", "2x", "3x"], fill_value=0)
        .reset_index()
    )
    g = budgeted.groupby("symbol", as_index=False).agg(
        graded_total_capital=("budgeted_spend", "sum"),
        graded_final_shares=("graded_shares", "sum"),
        graded_buy_days=("budgeted_spend", lambda s: int((s > 0).sum())),
        capped_days=("budget_capped", "sum"),
    )
    f = hist_30.groupby("symbol", as_index=False).agg(
        flat_total_capital=("flat_spend", "sum"),
        flat_final_shares=("flat_shares", "sum"),
    )
    summary = g.merge(f, on="symbol", how="outer").merge(tier_counts, on="symbol", how="left")
    summary["graded_avg_entry_price"] = summary["graded_total_capital"] / summary["graded_final_shares"].replace(0, pd.NA)
    summary["flat_avg_entry_price"] = summary["flat_total_capital"] / summary["flat_final_shares"].replace(0, pd.NA)
    summary["capital_delta_vs_flat"] = summary["graded_total_capital"] - summary["flat_total_capital"]
    summary["share_delta_vs_flat"] = summary["graded_final_shares"] - summary["flat_final_shares"]
    ordered_cols = [
        "symbol", "graded_total_capital", "graded_avg_entry_price", "graded_final_shares",
        "flat_total_capital", "flat_avg_entry_price", "flat_final_shares",
        "capital_delta_vs_flat", "share_delta_vs_flat", "graded_buy_days", "0x", "1x", "1.5x", "2x", "3x", "capped_days",
    ]
    summary = summary[ordered_cols].sort_values(["graded_total_capital", "symbol"], ascending=[False, True])
    return summary, budgeted


def load_quotes() -> pd.DataFrame:
    quote_files = list(BASE.glob("*quotes*.csv"))
    if not quote_files:
        raise RuntimeError("No quote file found.")
    q = pd.read_csv(quote_files[0])
    for col in ["price", "change", "changesPercentage", "previousClose", "open", "dayLow", "dayHigh", "volume"]:
        if col in q.columns:
            q[col] = pd.to_numeric(q[col], errors="coerce")
    q["drop_pct"] = (q["previousClose"] - q["price"]) / q["previousClose"] * 100
    tiers = q["drop_pct"].apply(tier_for_drop)
    q["multiplier"] = [x[0] for x in tiers]
    q["tier"] = [x[1] for x in tiers]
    q["planned_dollars"] = q["multiplier"] * BASE_AMOUNT
    q["signal_status"] = q["multiplier"].apply(lambda x: "BUY_PREVIEW" if x > 0 else "NO_BUY")
    q = q[q["symbol"].isin(ACTIVE_TICKERS)].copy()
    q = q.sort_values(["multiplier", "drop_pct", "symbol"], ascending=[False, False, True])

    remaining_daily = DAILY_CAP
    remaining_weekly = WEEKLY_CAP
    budgeted = []
    for row in q.itertuples(index=False):
        planned = float(row.planned_dollars)
        allowed = min(planned, remaining_daily, remaining_weekly)
        budgeted.append(allowed)
        remaining_daily -= allowed
        remaining_weekly -= allowed
    q["budgeted_dollars_if_run_now"] = budgeted
    q["would_use_daily_cap"] = q["planned_dollars"].cumsum() > DAILY_CAP
    return q


def write_report(summary: pd.DataFrame, decisions: pd.DataFrame, today: pd.DataFrame) -> dict:
    signal_days = decisions[decisions["budgeted_spend"] > 0]
    total_graded = float(summary["graded_total_capital"].sum())
    total_flat = float(summary["flat_total_capital"].sum())
    total_graded_shares = float(summary["graded_final_shares"].sum())
    total_flat_shares = float(summary["flat_final_shares"].sum())
    today_signal = today[today["planned_dollars"] > 0]
    tier_distribution_today = today["tier"].value_counts().reindex(["0x", "1x", "1.5x", "2x", "3x"], fill_value=0).to_dict()

    top_lines = summary.head(10)[[
        "symbol", "graded_total_capital", "graded_avg_entry_price", "graded_final_shares",
        "flat_total_capital", "flat_avg_entry_price", "flat_final_shares", "1x", "1.5x", "2x", "3x"
    ]].copy()

    def fmt_money(x: float) -> str:
        return "" if pd.isna(x) else f"${x:,.2f}"

    def fmt_num(x: float, n: int = 6) -> str:
        return "" if pd.isna(x) else f"{x:,.{n}f}"

    md = []
    md.append("# Revised Graded DCA Ladder Analysis")
    md.append("")
    md.append("This analysis uses completed daily bars for the 30 completed trading days before 2026-06-29 for the backtest, and the finance quote snapshot fetched on 2026-06-29 at 11:25 AM ET for the intraday tier preview.")
    md.append("")
    md.append("## Portfolio-level summary")
    md.append("")
    md.append(f"- Active symbols with data: {summary['symbol'].nunique()}")
    md.append(f"- Graded capital deployed: {fmt_money(total_graded)}")
    md.append(f"- Flat $1/day DCA capital deployed: {fmt_money(total_flat)}")
    md.append(f"- Graded final shares across tickers: {fmt_num(total_graded_shares)}")
    md.append(f"- Flat final shares across tickers: {fmt_num(total_flat_shares)}")
    md.append(f"- Budget-capped historical rows: {int(decisions['budget_capped'].sum())}")
    md.append(f"- Today preview signals: {len(today_signal)}")
    md.append(f"- Today planned dollars if run now: {fmt_money(float(today['planned_dollars'].sum()))}")
    md.append(f"- Today budgeted dollars if run now: {fmt_money(float(today['budgeted_dollars_if_run_now'].sum()))}")
    md.append("")
    md.append("## Top tickers by graded capital deployed")
    md.append("")
    md.append("| Ticker | Graded capital | Graded avg entry | Graded shares | Flat capital | Flat avg entry | Flat shares | 1x | 1.5x | 2x | 3x |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for rd in top_lines.to_dict('records'):
        md.append(
            f"| {rd['symbol']} | {fmt_money(rd['graded_total_capital'])} | {fmt_money(rd['graded_avg_entry_price'])} | {fmt_num(rd['graded_final_shares'])} | "
            f"{fmt_money(rd['flat_total_capital'])} | {fmt_money(rd['flat_avg_entry_price'])} | {fmt_num(rd['flat_final_shares'])} | "
            f"{int(rd['1x'])} | {int(rd['1.5x'])} | {int(rd['2x'])} | {int(rd['3x'])} |"
        )
    md.append("")
    md.append("## Today tier distribution")
    md.append("")
    for tier, count in tier_distribution_today.items():
        md.append(f"- {tier}: {count}")
    md.append("")
    md.append("The full ticker-by-ticker backtest table and today preview table are included as CSV files.")
    report_path = OUT / "dca_revised_ladder_summary.md"
    report_path.write_text("\n".join(md) + "\n")

    return {
        "active_symbols": int(summary["symbol"].nunique()),
        "start_date": str(decisions["date"].min()),
        "end_date": str(decisions["date"].max()),
        "trading_days": int(decisions["date"].nunique()),
        "graded_capital": total_graded,
        "flat_capital": total_flat,
        "graded_shares": total_graded_shares,
        "flat_shares": total_flat_shares,
        "budget_capped_rows": int(decisions["budget_capped"].sum()),
        "today_signals": int(len(today_signal)),
        "today_planned_dollars": float(today["planned_dollars"].sum()),
        "today_budgeted_dollars": float(today["budgeted_dollars_if_run_now"].sum()),
        "today_tier_distribution": {k: int(v) for k, v in tier_distribution_today.items()},
    }


def main() -> None:
    hist = load_history()
    hist_30 = completed_last_30(hist)
    summary, decisions = build_backtest(hist_30)
    today = load_quotes()

    summary.to_csv(OUT / "dca_revised_ladder_backtest_by_ticker.csv", index=False)
    decisions.to_csv(OUT / "dca_revised_ladder_backtest_decisions.csv", index=False)
    today.to_csv(OUT / "dca_today_tier_preview_2026_06_29_1125ET.csv", index=False)
    stats = write_report(summary, decisions, today)
    (OUT / "dca_revised_ladder_summary.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
