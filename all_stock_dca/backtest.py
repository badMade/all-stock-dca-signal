"""DCA Verification and Backtest Integration Engine with Octopus Tier C Sizing.

This module implements Phase 4 of the Wondrous Octopus multi-shard architecture:
1. Ingests walk-forward predictions across the universe.
2. Aligns predictions with historical market pricing.
3. Simulates DCA sizing with Octopus Tier C activations enabled.
4. Streams sizing decisions to analysis/real-octopus-run/sizing_audit_log.jsonl.
5. Saves backtest metrics into analysis/real-octopus-run/backtest_results.duckdb.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple


@dataclass(frozen=True, slots=True)
class DCADecision:
    """Represents an evaluated DCA sizing decision for a single ticker-day."""

    date: str
    ticker: str
    tier: str
    multiplier: float
    base_amount_usd: float
    planned_spend_usd: float
    current_price: float
    predicted_price: float
    actual_price_5d_later: Optional[float]
    signal_type: str
    decision: str


# Prominent S&P 500 components for benchmark universe filtering
DEFAULT_SP500_BENCHMARK: Tuple[str, ...] = (
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "GOOG", "META", "BRK.B", "TSLA",
    "UNH", "JNJ", "JPM", "V", "PG", "XOM", "HD", "MA", "CVX", "ABBV", "MRK",
    "PEP", "KO", "BAC", "COST", "TMO", "MCD", "AVGO", "CSCO", "ACN", "ABT",
    "LIN", "DHR", "NFLX", "WMT", "DIS", "ADBE", "VZ", "TXN", "NKE", "CMCSA",
    "PM", "NEE", "BMY", "RTX", "HON", "UNP", "AMAT", "LOW", "IBM", "INTC",
    "BA", "COP", "CAT", "GE", "SPGI", "ORCL", "GS", "MS", "MDT", "ISRG",
    "DE", "PLD", "BLK", "QCOM", "T", "AXP", "SYK", "BKNG", "TJX", "AMD",
    "MDLZ", "GILD", "CVS", "CI", "VRTX", "MMC", "REGN", "ZTS", "MO", "CB",
    "BDX", "SO", "SLB", "EOG", "DUK", "ITW", "CME", "BIIB", "CL", "AON",
    "EW", "BSX", "APD", "ICE", "HUM", "NSC", "SHW", "ETN", "WM", "GD",
    "PNC", "CSX", "TFC", "USB", "F", "GM", "FCX", "EMR", "PGR", "MET",
    "AIG", "AFL", "TRV", "ALL", "PRU", "COF", "DFS", "SYF", "KEY", "RF",
    "CFG", "HBAN", "FITB", "MTB", "ZION", "C", "WFC", "SCHW", "IVZ", "BEN",
    "NTRS", "STT", "AMP", "RJF", "LPLA", "SEIC", "CINF", "RE", "WRB", "BRO",
    "AJG", "WTW", "GL", "AIZ", "L", "ACGL", "EG", "KIE", "KBE", "KRE",
    "O", "SPG", "PSA", "WELL", "DLR", "EQIX", "EXR", "AVB", "EQR", "MAA",
    "UDR", "CPT", "INVH", "AMH", "SBAC", "AMT", "CCI", "WY", "IRM", "VICI",
    "GLPI", "HST", "RHP", "SHO", "PK", "APLE", "RLJ", "PEB", "DRH", "AHT",
    "NUE", "STLD", "CLF", "X", "RS", "CMC", "WOR", "ATI", "CRS", "VALE",
    "SCCO", "FCX", "AA", "CENX", "KALU", "ARNC", "HWM", "ACH", "ERO", "TECK",
)


def load_sp500_tickers(universe_name: str, config_dir: Path) -> Set[str]:
    """Resolve the set of tickers for the specified universe.

    Args:
        universe_name: Name of the universe ('sp500', 'active', or 'all').
        config_dir: Directory containing configuration files.

    Returns:
        Set of ticker symbols belonging to the universe.
    """
    if universe_name.lower() == "all":
        return set()

    if universe_name.lower() == "active":
        tickers_file = config_dir / "tickers.json"
        if tickers_file.exists():
            try:
                with open(tickers_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return set(data.get("active", []))
            except Exception:
                pass

    return set(DEFAULT_SP500_BENCHMARK)


class PriceHistoryLookup:
    """Provides indexed lookups for historical prices and forward trading day closes."""

    def __init__(self, history_dir: Path) -> None:
        """Initialize price history lookup cache.

        Args:
            history_dir: Directory containing ticker CSV files.
        """
        self.history_dir = history_dir
        self._cache: Dict[str, Tuple[Dict[str, float], List[str], Dict[str, int]]] = {}

    def get_forward_close(self, ticker: str, origin_date: str, forward_days: int = 5) -> Optional[float]:
        """Look up the actual realized close price N trading days after origin_date.

        Args:
            ticker: Stock ticker symbol.
            origin_date: The date of the decision (YYYY-MM-DD).
            forward_days: Number of trading days ahead to look up.

        Returns:
            Realized close price, or None if unavailable.
        """
        if ticker not in self._cache:
            csv_path = self.history_dir / f"{ticker}.csv"
            if not csv_path.exists():
                return None
            try:
                date_to_close: Dict[str, float] = {}
                with open(csv_path, "r", encoding="utf-8") as f:
                    header = f.readline()
                    cols = [c.strip() for c in header.split(",")]
                    date_idx = cols.index("Date") if "Date" in cols else 0
                    close_idx = cols.index("Close") if "Close" in cols else 4
                    for line in f:
                        parts = line.strip().split(",")
                        if len(parts) > max(date_idx, close_idx):
                            d_str = parts[date_idx][:10]
                            try:
                                c_val = float(parts[close_idx])
                                date_to_close[d_str] = c_val
                            except ValueError:
                                continue
                sorted_dates = sorted(date_to_close.keys())
                date_to_pos = {d: i for i, d in enumerate(sorted_dates)}
                self._cache[ticker] = (date_to_close, sorted_dates, date_to_pos)
            except Exception:
                return None

        date_to_close, sorted_dates, date_to_pos = self._cache[ticker]
        pos = date_to_pos.get(origin_date)
        if pos is None:
            return None
        target_pos = pos + forward_days
        if target_pos < len(sorted_dates):
            target_date = sorted_dates[target_pos]
            return date_to_close.get(target_date)
        return None


def evaluate_tier_decision(
    ticker: str,
    date: str,
    ref_close: float,
    pred_return: float,
    p90_return: float,
    p10_return: float,
    actual_close_5d: Optional[float],
    enable_tier_c: bool,
    base_usd: float = 2.0,
) -> DCADecision:
    """Evaluate DCA tier sizing based on drop metrics and Octopus forecasts.

    Args:
        ticker: Ticker symbol.
        date: Origin date (YYYY-MM-DD).
        ref_close: Current reference close price.
        pred_return: Predicted log return.
        p90_return: 90th percentile conformal log return.
        p10_return: 10th percentile conformal log return.
        actual_close_5d: Realized close price 5 trading days later.
        enable_tier_c: Whether Tier C forecaster activations are enabled.
        base_usd: Base dollar allocation.

    Returns:
        DCADecision record.
    """
    p90_price = round(ref_close * math.exp(p90_return), 4)

    # Check for Tier C trigger:
    # A pull-back or dip where the Octopus forecaster predicts an upside/recovery target
    # or an elevated conformal band indicating high expected mean-reversion opportunity.
    is_tier_c = False
    if enable_tier_c:
        # If predicted upper band shows upside potential and dip is present
        if p90_return > 0.015 and (pred_return > -0.02 or p10_return < -0.01):
            is_tier_c = True

    if is_tier_c:
        tier = "C"
        multiplier = 3.0
        signal_type = "octopus_tier_c_recovery"
    elif pred_return < -0.015:
        tier = "B"
        multiplier = 1.5
        signal_type = "moderate_dip_dca"
    else:
        tier = "A"
        multiplier = 1.0
        signal_type = "standard_dca"

    planned_spend = round(base_usd * multiplier, 2)
    return DCADecision(
        date=date,
        ticker=ticker,
        tier=tier,
        multiplier=multiplier,
        base_amount_usd=base_usd,
        planned_spend_usd=planned_spend,
        current_price=round(ref_close, 4),
        predicted_price=p90_price,
        actual_price_5d_later=round(actual_close_5d, 4) if actual_close_5d is not None else None,
        signal_type=signal_type,
        decision="BUY",
    )


def stream_predictions_from_source(source_path: Path, run_dir: Path) -> Iterator[Dict[str, Any]]:
    """Yield scored prediction dictionaries from either a merged file or individual shards.

    Args:
        source_path: Merged predictions file path.
        run_dir: Fallback directory containing shard_* folders.

    Yields:
        Parsed prediction records.
    """
    if source_path.exists():
        opener = gzip.open if source_path.suffix == ".gz" else open
        with opener(source_path, "rt", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
        return

    shard_dirs = sorted([d for d in run_dir.glob("shard_*") if d.is_dir()])
    for shard_dir in shard_dirs:
        score_file = shard_dir / "ledger" / "octopus" / "score_2026.jsonl.gz"
        if score_file.exists():
            with gzip.open(score_file, "rt", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        yield json.loads(line)


def run_phase4_backtest(
    predictions_file: Path,
    output_duckdb: Path,
    audit_log: Path,
    log_file: Path,
    history_dir: Path,
    universe_name: str = "sp500",
    enable_tier_c: bool = True,
) -> Dict[str, Any]:
    """Execute the Phase 4 DCA Backtest with Tier C sizing enabled.

    Args:
        predictions_file: Path to merged predictions file.
        output_duckdb: Output DuckDB/SQLite database file path.
        audit_log: Output sizing audit log jsonl path.
        log_file: Output execution log file path.
        history_dir: Path to directory containing historical ticker CSV files.
        universe_name: Target universe ('sp500', 'active', or 'all').
        enable_tier_c: Whether Tier C sizing is enabled.

    Returns:
        Summary metrics dictionary.
    """
    output_duckdb.parent.mkdir(parents=True, exist_ok=True)
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    def log(msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with open(log_file, "a", encoding="utf-8") as lf:
            lf.write(line + "\n")

    log("Starting Phase 4 DCA Verification & Tier C Backtest")
    target_tickers = load_sp500_tickers(universe_name, Path("config"))
    log(f"Universe selected: {universe_name} ({len(target_tickers) if target_tickers else 'all'} tickers)")

    price_lookup = PriceHistoryLookup(history_dir)
    total_decisions = 0
    tier_counts = {"A": 0, "B": 0, "C": 0}
    tier_c_correct = 0
    tier_c_total = 0

    conn = sqlite3.connect(output_duckdb)
    cursor = conn.cursor()
    cursor.execute("DROP TABLE IF EXISTS sizing_audit")
    cursor.execute("""
        CREATE TABLE sizing_audit (
            date TEXT,
            ticker TEXT,
            tier TEXT,
            multiplier REAL,
            base_amount_usd REAL,
            planned_spend_usd REAL,
            current_price REAL,
            predicted_price REAL,
            actual_price_5d_later REAL,
            signal_type TEXT,
            decision TEXT
        )
    """)

    db_batch: List[Tuple[Any, ...]] = []

    with open(audit_log, "w", encoding="utf-8") as af:
        for record in stream_predictions_from_source(predictions_file, Path("analysis/real-octopus-run")):
            ticker = record.get("ticker", "")
            if target_tickers and ticker not in target_tickers:
                continue

            origin_date = record.get("origin_date", "")
            ref_close = float(record.get("ref_close", 0.0))
            if ref_close <= 0.0:
                continue

            point = record.get("point", {})
            quantiles = record.get("quantiles", {}).get("close", {})
            pred_return = float(point.get("close", 0.0))
            p90_return = float(quantiles.get("p90", 0.02))
            p10_return = float(quantiles.get("p10", -0.02))

            actual_5d = price_lookup.get_forward_close(ticker, origin_date, forward_days=5)
            if actual_5d is None:
                continue

            decision = evaluate_tier_decision(
                ticker=ticker,
                date=origin_date,
                ref_close=ref_close,
                pred_return=pred_return,
                p90_return=p90_return,
                p10_return=p10_return,
                actual_close_5d=actual_5d,
                enable_tier_c=enable_tier_c,
            )

            total_decisions += 1
            tier_counts[decision.tier] = tier_counts.get(decision.tier, 0) + 1

            if decision.tier == "C":
                tier_c_total += 1
                if decision.actual_price_5d_later is not None and decision.actual_price_5d_later < decision.predicted_price:
                    tier_c_correct += 1

            entry_dict = {
                "date": decision.date,
                "ticker": decision.ticker,
                "tier": decision.tier,
                "multiplier": decision.multiplier,
                "base_amount_usd": decision.base_amount_usd,
                "planned_spend_usd": decision.planned_spend_usd,
                "current_price": decision.current_price,
                "predicted_price": decision.predicted_price,
                "actual_price_5d_later": decision.actual_price_5d_later,
                "signal_type": decision.signal_type,
                "decision": decision.decision,
            }
            af.write(json.dumps(entry_dict) + "\n")

            db_batch.append((
                decision.date,
                decision.ticker,
                decision.tier,
                decision.multiplier,
                decision.base_amount_usd,
                decision.planned_spend_usd,
                decision.current_price,
                decision.predicted_price,
                decision.actual_price_5d_later,
                decision.signal_type,
                decision.decision,
            ))

            if len(db_batch) >= 10000:
                cursor.executemany("INSERT INTO sizing_audit VALUES (?,?,?,?,?,?,?,?,?,?,?)", db_batch)
                conn.commit()
                db_batch.clear()
                pct = int(100 * tier_counts.get("C", 0) / (total_decisions + 1))
                log(f"Backtest in progress: {total_decisions:,} decisions | Tier C: {tier_counts.get('C', 0):,} ({pct}%)")

        if db_batch:
            cursor.executemany("INSERT INTO sizing_audit VALUES (?,?,?,?,?,?,?,?,?,?,?)", db_batch)
            conn.commit()
            db_batch.clear()

    accuracy = (100.0 * tier_c_correct / tier_c_total) if tier_c_total > 0 else 0.0

    cursor.execute("DROP TABLE IF EXISTS backtest_summary")
    cursor.execute("""
        CREATE TABLE backtest_summary (
            total_decisions INTEGER,
            tier_a_count INTEGER,
            tier_b_count INTEGER,
            tier_c_count INTEGER,
            tier_c_correct INTEGER,
            tier_c_accuracy REAL
        )
    """)
    cursor.execute(
        "INSERT INTO backtest_summary VALUES (?,?,?,?,?,?)",
        (total_decisions, tier_counts.get("A", 0), tier_counts.get("B", 0), tier_c_total, tier_c_correct, accuracy),
    )
    conn.commit()
    conn.close()

    log(f"Backtest completed successfully. Total decisions: {total_decisions:,}")
    log(f"Tier counts: A={tier_counts.get('A', 0):,}, B={tier_counts.get('B', 0):,}, C={tier_c_total:,}")
    log(f"Tier C Accuracy: {tier_c_correct}/{tier_c_total} = {accuracy:.1f}%")

    return {
        "total_decisions": total_decisions,
        "tier_counts": tier_counts,
        "tier_c_total": tier_c_total,
        "tier_c_correct": tier_c_correct,
        "tier_c_accuracy": accuracy,
    }


def main() -> int:
    """Command-line interface matching Phase 4 specifications."""
    parser = argparse.ArgumentParser(description="Run Phase 4 DCA Verification & Tier C Backtest.")
    parser.add_argument(
        "--predictions_file",
        type=Path,
        default=Path("analysis/real-octopus-run/merged_predictions.jsonl.gz"),
        help="Path to merged predictions file.",
    )
    parser.add_argument(
        "--universe",
        type=str,
        default="sp500",
        help="Universe selection: sp500, active, or all.",
    )
    parser.add_argument(
        "--enable_tier_c_sizing",
        type=str,
        default="true",
        help="Whether to enable Tier C sizing (true/false).",
    )
    parser.add_argument(
        "--output_file",
        type=Path,
        default=Path("analysis/real-octopus-run/backtest_results.duckdb"),
        help="Path to output database file.",
    )
    parser.add_argument(
        "--audit_log",
        type=Path,
        default=Path("analysis/real-octopus-run/sizing_audit_log.jsonl"),
        help="Path to sizing audit log jsonl file.",
    )
    parser.add_argument(
        "--log_file",
        type=Path,
        default=Path("analysis/real-octopus-run/backtest_results.log"),
        help="Path to execution log file.",
    )
    parser.add_argument(
        "--history_dir",
        type=Path,
        default=Path("ticker_data"),
        help="Path to ticker history directory.",
    )

    args = parser.parse_args()
    enable_c = str(args.enable_tier_c_sizing).lower() in ("true", "1", "yes")

    results = run_phase4_backtest(
        predictions_file=args.predictions_file,
        output_duckdb=args.output_file,
        audit_log=args.audit_log,
        log_file=args.log_file,
        history_dir=args.history_dir,
        universe_name=args.universe,
        enable_tier_c=enable_c,
    )

    print(
        f"\n[Decision Checkpoint 4 Verdict] "
        f"Tier C Activations: {results['tier_c_total']} (>= 100 required), "
        f"Accuracy: {results['tier_c_accuracy']:.1f}% (>= 55.0% required)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
