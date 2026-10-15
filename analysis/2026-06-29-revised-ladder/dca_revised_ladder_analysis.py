from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast

import numpy as np
import pandas as pd

# Data/output directory. Override with DCA_LADDER_DIR; defaults to this script's own
# folder so the analysis is portable rather than tied to one machine's absolute path.
BASE = Path(os.environ.get("DCA_LADDER_DIR", Path(__file__).resolve().parent))
OUT = BASE
ACTIVE_TICKERS = [
    "DFTX",
    "TRIN",
    "AAAU",
    "SPCX",
    "TRLV",
    "RVI",
    "VCX",
    "EVR",
    "LUNR",
    "IRDM",
    "DXYZ",
    "LITE",
    "ASML",
    "DRAM",
    "STX",
    "MSFT",
    "AAPL",
    "AMAT",
    "AMD",
    "CBRS",
    "MRVL",
    "BTX",
    "STM",
    "SNDK",
    "WDC",
    "TSM",
    "AVGO",
    "NVDA",
    "QBTS",
    "FLKR",
    "INTC",
    "GOOGL",
    "GOOG",
    "AMZN",
    "GLW",
    "MA",
    "GS",
    "MU",
    "NFLX",
    "XLG",
    "TOPT",
    "QTOP",
    "SATA",
    "RGTI",
    "IBM",
]

BASE_AMOUNT = 1.0
DAILY_CAP = 120.0
WEEKLY_CAP = 600.0


def compute_tier_and_multiplier(drop_pct: float | pd.Series | np.ndarray) -> tuple[Any, Any]:
    is_series = isinstance(drop_pct, pd.Series)
    is_scalar = not isinstance(drop_pct, (pd.Series, np.ndarray))

    arr = np.asarray(drop_pct)
    conds = [
        pd.isna(arr) | (arr < 0.1),
        arr < 2.0,
        arr < 5.0,
        arr < 8.0,
    ]
    mults = np.select(conds, [0.0, 1.0, 1.5, 2.0], default=3.0)
    tiers = np.select(conds, ["0x", "1x", "1.5x", "2x"], default="3x")

    if is_scalar:
        return float(mults.item()), str(tiers.item())
    if is_series:
        return pd.Series(mults, index=drop_pct.index), pd.Series(tiers, index=drop_pct.index)
    return mults, tiers


def tier_for_drop(drop_pct: float | pd.Series | np.ndarray) -> tuple[Any, Any]:
    return compute_tier_and_multiplier(drop_pct)


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
        df["date"] = cast(Any, pd.to_datetime(df["date"])).dt.date
        for col in ["open", "high", "low", "close", "volume"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        frames.append(df)
    if not frames:
        raise RuntimeError("No history files found.")
    hist = pd.concat(frames, ignore_index=True).sort_values(by=["symbol", "date"])
    hist["prev_close"] = hist.groupby("symbol")["close"].shift(1)
    hist["drop_pct"] = (hist["prev_close"] - hist["close"]) / hist["prev_close"] * 100
    hist["multiplier"], hist["tier"] = compute_tier_and_multiplier(hist["drop_pct"])
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
    idx_name = decisions.index.name if decisions.index.name else "index"
    for date, group in decisions.sort_values(
        ["date", "multiplier", "drop_pct"], ascending=[True, False, False]
    ).groupby("date"):
        iso = pd.Timestamp(cast(Any, date)).isocalendar()
        week_key = (iso.year, iso.week)
        remaining_daily = DAILY_CAP
        remaining_weekly = WEEKLY_CAP - week_spend.get(week_key, 0.0)
        # ⚡ Bolt Optimization: Use itertuples() instead of to_dict('records') for faster iteration
        # without per-row dictionary allocation overhead.
        group = group.reset_index(names=[idx_name])
        cols = list(group.columns)
        idx_planned = cols.index("planned_spend")
        idx_close = cols.index("close")

        for row_tuple in group.itertuples(index=False, name=None):
            planned = float(row_tuple[idx_planned])
            allowed = min(planned, max(0.0, remaining_daily), max(0.0, remaining_weekly))

            row = dict(zip(cols, row_tuple))
            row["budgeted_spend"] = allowed
            row["budget_capped"] = planned > allowed + 1e-9

            close_val = row_tuple[idx_close]
            if allowed > 0 and close_val > 0:
                row["graded_shares"] = allowed / close_val
            else:
                row["graded_shares"] = 0.0

            remaining_daily -= allowed
            remaining_weekly -= allowed
            week_spend[week_key] = week_spend.get(week_key, 0.0) + allowed
            rows.append(row)
    res = pd.DataFrame(rows).set_index(idx_name)
    res.index.name = decisions.index.name
    return res


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
    summary["graded_avg_entry_price"] = summary["graded_total_capital"] / summary["graded_final_shares"].replace(
        0, pd.NA
    )
    summary["flat_avg_entry_price"] = summary["flat_total_capital"] / summary["flat_final_shares"].replace(0, pd.NA)
    summary["capital_delta_vs_flat"] = summary["graded_total_capital"] - summary["flat_total_capital"]
    summary["share_delta_vs_flat"] = summary["graded_final_shares"] - summary["flat_final_shares"]
    ordered_cols = [
        "symbol",
        "graded_total_capital",
        "graded_avg_entry_price",
        "graded_final_shares",
        "flat_total_capital",
        "flat_avg_entry_price",
        "flat_final_shares",
        "capital_delta_vs_flat",
        "share_delta_vs_flat",
        "graded_buy_days",
        "0x",
        "1x",
        "1.5x",
        "2x",
        "3x",
        "capped_days",
    ]
    summary = summary[ordered_cols].sort_values(["graded_total_capital", "symbol"], ascending=[False, True])  # type: ignore
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
    q["multiplier"], q["tier"] = compute_tier_and_multiplier(q["drop_pct"])
    q["planned_dollars"] = q["multiplier"] * BASE_AMOUNT
    q["signal_status"] = np.where(q["multiplier"] > 0, "BUY_PREVIEW", "NO_BUY")
    q = q[q["symbol"].isin(ACTIVE_TICKERS)].copy()
    q = q.sort_values(["multiplier", "drop_pct", "symbol"], ascending=[False, False, True])  # type: ignore

    remaining_daily = DAILY_CAP
    remaining_weekly = WEEKLY_CAP
    budgeted = []
    # ⚡ Bolt: Iterate the single needed column directly instead of materializing
    # a list of row dicts. This is faster/lighter than to_dict('records') and,
    # unlike itertuples(), never touches the other columns' names, so it's safe
    # even though q (loaded from an external CSV) may have non-identifier columns.
    for planned in q["planned_dollars"]:
        planned = float(planned)
        allowed = min(planned, remaining_daily, remaining_weekly)
        budgeted.append(allowed)
        remaining_daily -= allowed
        remaining_weekly -= allowed
    q["budgeted_dollars_if_run_now"] = budgeted
    q["would_use_daily_cap"] = q["planned_dollars"].cumsum() > DAILY_CAP
    return q


def write_report(summary: pd.DataFrame, decisions: pd.DataFrame, today: pd.DataFrame) -> dict:
    total_graded = float(summary["graded_total_capital"].sum())
    total_flat = float(summary["flat_total_capital"].sum())
    total_graded_shares = float(summary["graded_final_shares"].sum())
    total_flat_shares = float(summary["flat_final_shares"].sum())
    today_signal = today[today["planned_dollars"] > 0]
    tier_distribution_today = (
        today["tier"].value_counts().reindex(["0x", "1x", "1.5x", "2x", "3x"], fill_value=0).to_dict()
    )

    top_lines = summary.head(10)[
        [
            "symbol",
            "graded_total_capital",
            "graded_avg_entry_price",
            "graded_final_shares",
            "flat_total_capital",
            "flat_avg_entry_price",
            "flat_final_shares",
            "1x",
            "1.5x",
            "2x",
            "3x",
        ]
    ].copy()

    def fmt_money(x: float) -> str:
        return "" if pd.isna(x) else f"${x:,.2f}"

    def fmt_num(x: float, n: int = 6) -> str:
        return "" if pd.isna(x) else f"{x:,.{n}f}"

    md = []
    md.append("# Revised Graded DCA Ladder Analysis")
    md.append("")
    md.append(
        "This analysis uses completed daily bars for the 30 completed trading days before 2026-06-29 for the backtest, and the finance quote snapshot fetched on 2026-06-29 at 11:25 AM ET for the intraday tier preview."
    )
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
    md.append(
        "| Ticker | Graded capital | Graded avg entry | Graded shares | Flat capital | Flat avg entry | Flat shares | 1x | 1.5x | 2x | 3x |"
    )
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    # ⚡ Bolt Optimization: Use to_dict('records') instead of iterrows() for faster iteration.
    # We use to_dict because column names like '1x' are invalid Python identifiers for itertuples.
    for rd in top_lines.to_dict(orient="records"):  # type: ignore
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
        "active_symbols": summary["symbol"].nunique(),
        "start_date": str(decisions["date"].min()),
        "end_date": str(decisions["date"].max()),
        "trading_days": decisions["date"].nunique(),
        "graded_capital": total_graded,
        "flat_capital": total_flat,
        "graded_shares": total_graded_shares,
        "flat_shares": total_flat_shares,
        "budget_capped_rows": int(decisions["budget_capped"].sum()),
        "today_signals": len(today_signal),
        "today_planned_dollars": float(today["planned_dollars"].sum()),
        "today_budgeted_dollars": float(today["budgeted_dollars_if_run_now"].sum()),
        "today_tier_distribution": dict(tier_distribution_today),
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
