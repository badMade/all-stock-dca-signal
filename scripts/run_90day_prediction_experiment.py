#!/usr/bin/env python3
"""Run a 90-trading-day walk-forward prediction experiment and sequestered evaluation.

This script orchestrates the full end-to-end experiment:
1. Identifies the exact 90-trading-day evaluation window ending at today (or --end-date).
2. Generates strictly zero-lookahead walk-forward predictions for the selected tickers.
3. Quarantines the predictions in an isolated sandbox directory.
4. Triggers the Sequestered Truth Comparator ("source of truth") to evaluate the
   predictions against the actual realized market data.
5. Outputs Markdown reports, JSON metrics, and CSV ledgers.

Usage:
    python3 scripts/run_90day_prediction_experiment.py --tickers SPY AAPL NVDA MSFT GOOGL
    python3 scripts/run_90day_prediction_experiment.py --days 90 --model persistence
    python3 scripts/run_90day_prediction_experiment.py --days 90 --model gbm
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.sequestered_truth_comparator import (  # noqa: E402
    SequesteredComparator,
    export_daily_ledger_csv,
    load_actuals_from_csv,
    render_markdown_report,
)

DEFAULT_TICKERS = ["SPY", "AAPL", "MSFT", "NVDA", "GOOGL"]


def get_trading_days(ticker_dir: Path, reference_ticker: str = "SPY") -> list[str]:
    """Retrieve sorted list of trading dates from a reference ticker CSV.

    Args:
        ticker_dir: Directory containing per-ticker CSVs.
        reference_ticker: Ticker symbol to extract calendar from.

    Returns:
        Sorted list of date strings (YYYY-MM-DD).
    """
    ref_path = ticker_dir / f"{reference_ticker}.csv"
    if not ref_path.exists():
        # Fall back to AAPL if SPY is absent
        ref_path = ticker_dir / "AAPL.csv"
    if not ref_path.exists():
        raise FileNotFoundError(f"Reference ticker file not found in {ticker_dir}")

    dates: list[str] = []
    with ref_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            d = row.get("Date") or row.get("date")
            if d:
                dates.append(d.strip().split(" ")[0].split("T")[0])
    return sorted(list(set(dates)))


def generate_walkforward_baseline_predictions(
    tickers: list[str],
    trading_days: list[str],
    history_dir: Path,
    output_path: Path,
) -> list[dict[str, Any]]:
    """Generate walk-forward 1-step ahead predictions with zero lookahead bias.

    For each day t in the 90-day window, forecasts day t+1 using only data <= t.
    Uses an honest empirical quantile band (p10-p90) from trailing 60-day volatility.

    Args:
        tickers: List of ticker symbols to predict.
        trading_days: Chronological list of trading days covering the window.
        history_dir: Directory with actuals CSV files.
        output_path: Destination path for prediction CSV.

    Returns:
        List of generated prediction records.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_predictions: list[dict[str, Any]] = []

    for ticker in tickers:
        csv_path = history_dir / f"{ticker}.csv"
        if not csv_path.exists():
            print(f"Warning: {csv_path} does not exist, skipping {ticker}", file=sys.stderr)
            continue

        actuals = load_actuals_from_csv(csv_path)
        avail_dates = sorted([d for d in actuals.keys() if d in trading_days])

        # For each evaluation date t (starting from index 1 so t-1 is known)
        for i in range(1, len(avail_dates)):
            target_date = avail_dates[i]
            prev_date = avail_dates[i - 1]

            prev_bar = actuals[prev_date]
            pred_close = prev_bar["close"]
            pred_open = prev_bar["close"]
            pred_high = max(prev_bar["high"], pred_close)
            pred_low = min(prev_bar["low"], pred_close)

            # Trailing 30-day realized volatility for empirical p10-p90 bands
            trailing_window = avail_dates[max(0, i - 31) : i]
            returns = []
            for j in range(1, len(trailing_window)):
                c_prev = actuals[trailing_window[j - 1]]["close"]
                c_curr = actuals[trailing_window[j]]["close"]
                if c_prev > 0:
                    returns.append(math.log(c_curr / c_prev))

            if returns:
                vol = math.sqrt(sum(r**2 for r in returns) / len(returns))
                # 80% coverage corresponds to approximately +/- 1.282 standard deviations
                band_half_width = 1.282 * vol
                pred_p10 = pred_close * math.exp(-band_half_width)
                pred_p90 = pred_close * math.exp(band_half_width)
            else:
                pred_p10 = pred_close * 0.98
                pred_p90 = pred_close * 1.02

            all_predictions.append({
                "date": target_date,
                "ticker": ticker,
                "pred_close": round(pred_close, 4),
                "pred_open": round(pred_open, 4),
                "pred_high": round(pred_high, 4),
                "pred_low": round(pred_low, 4),
                "pred_close_p10": round(pred_p10, 4),
                "pred_close_p90": round(pred_p90, 4),
            })

    # Write predictions CSV
    fieldnames = [
        "date",
        "ticker",
        "pred_close",
        "pred_open",
        "pred_high",
        "pred_low",
        "pred_close_p10",
        "pred_close_p90",
    ]
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_predictions)

    print(f"Generated {len(all_predictions)} predictions written to: {output_path}")
    return all_predictions


def run_octopus_walkforward_predictions(
    tickers: list[str],
    start_date: str,
    end_date: str,
    model: str,
    run_dir: Path,
    output_path: Path,
) -> list[dict[str, Any]]:
    """Execute wondrous-octopus/run_walkforward.py and extract frozen predictions.

    Args:
        tickers: List of ticker symbols.
        start_date: First prediction date.
        end_date: Last prediction date.
        model: Model name ('persistence', 'gbm', or 'octopus').
        run_dir: Isolated run directory for the octopus harness.
        output_path: Destination path for the extracted prediction CSV.

    Returns:
        List of extracted prediction records.
    """
    cmd = [
        sys.executable,
        str(REPO_ROOT / "wondrous-octopus" / "run_walkforward.py"),
        "--tickers",
        *tickers,
        "--start",
        start_date,
        "--end",
        end_date,
        "--model",
        model,
        "--run-dir",
        str(run_dir),
    ]

    print(f"Running Wondrous Octopus replay: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Octopus replay failed with returncode {result.returncode}:", file=sys.stderr)
        print(result.stdout, file=sys.stderr)
        print(result.stderr, file=sys.stderr)
        raise RuntimeError("Octopus walkforward run failed")

    # Extract forecasts from ledger
    # Note: If inspect_run export is available, use it; otherwise read ledger
    export_cmd = [
        sys.executable,
        str(REPO_ROOT / "wondrous-octopus" / "inspect_run.py"),
        "--run-dir",
        str(run_dir),
        "--tomorrow",
        "--csv",
        str(output_path),
    ]
    subprocess.run(export_cmd, cwd=str(REPO_ROOT), capture_output=True)

    # If the output_path has content, read it; otherwise fallback to baseline extractor
    if output_path.exists() and output_path.stat().st_size > 0:
        with output_path.open("r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader)

    raise RuntimeError(f"Could not extract predictions to {output_path}")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for the 90-day prediction experiment."""
    parser = argparse.ArgumentParser(
        prog="run_90day_prediction_experiment.py",
        description="Run a 90-trading-day walk-forward prediction experiment and sequestered comparison.",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="List of tickers to evaluate (e.g. SPY AAPL MSFT NVDA GOOGL).",
    )
    parser.add_argument(
        "--all-scheduled",
        action="store_true",
        help="Evaluate all active tickers configured in the scheduler (config/tickers.json).",
    )
    parser.add_argument(
        "--all-archive",
        action="store_true",
        help="Evaluate all ticker CSVs present in the archive.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of trading days to simulate/evaluate (default: 90).",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="Target end date (YYYY-MM-DD, defaults to most recent date in ticker archive).",
    )
    parser.add_argument(
        "--model",
        choices=("persistence", "gbm", "octopus"),
        default="persistence",
        help="Forecasting model to use (default: persistence).",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="Directory containing ticker CSV files (default: ticker_data).",
    )
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=None,
        help="Directory to store isolated experiment artifacts.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI orchestrator entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    history_dir = args.history_dir or (REPO_ROOT / "ticker_data")
    if not history_dir.is_dir():
        print(f"REFUSED: History directory not found at {history_dir}", file=sys.stderr)
        return 1

    # Resolve trading days
    all_trading_days = get_trading_days(history_dir)
    if args.end_date:
        end_idx = None
        for i, d in enumerate(all_trading_days):
            if d <= args.end_date:
                end_idx = i
        if end_idx is None:
            print(f"REFUSED: Requested end-date {args.end_date} precedes all trading days", file=sys.stderr)
            return 1
    else:
        end_idx = len(all_trading_days) - 1

    start_idx = max(0, end_idx - args.days)
    selected_window = all_trading_days[start_idx : end_idx + 1]
    start_date = selected_window[1]  # First predicted date
    end_date = selected_window[-1]

    # Resolve target tickers
    if args.all_archive:
        discovered = [p.stem.upper() for p in history_dir.glob("*.csv") if not p.stem.startswith("_")]
        target_tickers = sorted(discovered)
    elif args.all_scheduled or (args.tickers is None):
        tickers_cfg_path = REPO_ROOT / "config" / "tickers.json"
        if tickers_cfg_path.exists():
            cfg_data = json.loads(tickers_cfg_path.read_text(encoding="utf-8"))
            active_list = cfg_data.get("active") or []
            target_tickers = sorted([t.upper() for t in active_list if (history_dir / f"{t}.csv").exists()])
        else:
            target_tickers = [t for t in DEFAULT_TICKERS if (history_dir / f"{t}.csv").exists()]
    else:
        target_tickers = [t.upper() for t in args.tickers if (history_dir / f"{t}.csv").exists()]

    if not target_tickers:
        print("REFUSED: No matching ticker CSVs found in history directory", file=sys.stderr)
        return 1

    print("=" * 80)
    print("90-DAY PREDICTION & SEQUESTERED TRUTH EVALUATION EXPERIMENT")
    print("=" * 80)
    print(f"Evaluation window:  {start_date} to {end_date} ({len(selected_window) - 1} prediction steps)")
    print(f"Tickers count:      {len(target_tickers)} tickers")
    if len(target_tickers) <= 15:
        print(f"Tickers:            {', '.join(target_tickers)}")
    else:
        print(f"Tickers sample:     {', '.join(target_tickers[:10])} ... (+{len(target_tickers) - 10} more)")
    print(f"Model:              {args.model}")
    print(f"Ground truth vault: {history_dir}")

    # Set up isolated experiment directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    exp_dir = args.experiment_dir or (REPO_ROOT / "artifacts" / "experiments" / f"run_90d_{timestamp}")
    sandbox_dir = exp_dir / "isolated_prediction_sandbox"
    results_dir = exp_dir / "sequestered_results"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    predictions_csv = sandbox_dir / "frozen_predictions.csv"

    # Step 1: Generate predictions without lookahead
    print("\n[Step 1/2] Generating out-of-sample predictions (zero lookahead)...")
    if args.model == "persistence":
        preds = generate_walkforward_baseline_predictions(
            tickers=target_tickers,
            trading_days=selected_window,
            history_dir=history_dir,
            output_path=predictions_csv,
        )
    else:
        # Full Wondrous Octopus replay
        preds = run_octopus_walkforward_predictions(
            tickers=target_tickers,
            start_date=start_date,
            end_date=end_date,
            model=args.model,
            run_dir=sandbox_dir / "octopus_run",
            output_path=predictions_csv,
        )

    # Step 2: Sequestered Evaluation (Source of Truth)
    print("\n[Step 2/2] Running Sequestered Truth Comparator ('Source of Truth')...")
    comparator = SequesteredComparator(ground_truth_dir=history_dir)
    summary, records = comparator.evaluate_all(preds)

    # Save reports
    report_md_path = results_dir / "comparison_report.md"
    report_json_path = results_dir / "comparison_metrics.json"
    ledger_csv_path = results_dir / "daily_comparison_ledger.csv"

    md_content = render_markdown_report(summary, records)
    report_md_path.write_text(md_content, encoding="utf-8")
    report_json_path.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
    export_daily_ledger_csv(records, ledger_csv_path)

    print("\n" + md_content)
    print("=" * 80)
    print("EXPERIMENT ARTIFACTS GENERATED:")
    print(f"- Markdown Report:   {report_md_path}")
    print(f"- JSON Metrics:      {report_json_path}")
    print(f"- Daily CSV Ledger:  {ledger_csv_path}")
    print(f"- Frozen Sandbox:    {predictions_csv}")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
