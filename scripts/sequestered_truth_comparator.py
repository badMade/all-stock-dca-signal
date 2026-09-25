#!/usr/bin/env python3
"""Sequestered Truth Comparator: Independent, decoupled evaluation of predictions.

This program serves as the authoritative, sequestered "source of truth" to
evaluate stock price forecasts against actual realized market data without
allowing any training data or model logic to influence or peek at evaluation
results.

Key properties:
- Decoupled from model architectures and training loops.
- Enforces strict data integrity: guards against non-finite values (NaN, Inf),
  duplicate dates/keys, boolean/integer confusion, and lookahead leakage.
- Computes comprehensive error and performance metrics:
  * MAE, RMSE, MAPE for Close, Open, High, Low
  * Directional Hit Rate (return sign prediction accuracy)
  * Quantile Band Coverage (p10-p90 coverage vs nominal target)
  * Realized Range Capture (predicted vs actual intraday range)
  * Cumulative return drift over the evaluation horizon
- Generates Markdown reports, structured JSON metrics, and CSV ledgers.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence


class ComparatorError(Exception):
    """Base exception for comparator validation and execution failures."""


class DataIntegrityError(ComparatorError):
    """Raised when predictions or ground truth violate integrity rules."""


@dataclass(frozen=True)
class DailyRecord:
    """A matched single-day prediction and ground truth observation."""

    date: str
    ticker: str
    actual_close: float
    pred_close: float
    actual_open: float | None = None
    pred_open: float | None = None
    actual_high: float | None = None
    pred_high: float | None = None
    actual_low: float | None = None
    pred_low: float | None = None
    pred_close_p10: float | None = None
    pred_close_p90: float | None = None
    prev_actual_close: float | None = None

    def __post_init__(self) -> None:
        """Validate all numeric values are strictly finite and non-negative."""
        for name, val in [
            ("actual_close", self.actual_close),
            ("pred_close", self.pred_close),
            ("actual_open", self.actual_open),
            ("pred_open", self.pred_open),
            ("actual_high", self.actual_high),
            ("pred_high", self.pred_high),
            ("actual_low", self.actual_low),
            ("pred_low", self.pred_low),
            ("pred_close_p10", self.pred_close_p10),
            ("pred_close_p90", self.pred_close_p90),
            ("prev_actual_close", self.prev_actual_close),
        ]:
            if val is not None:
                # Guard against boolean masquerading as int/float
                if isinstance(val, bool):
                    raise DataIntegrityError(f"Field {name} cannot be a boolean")
                if not isinstance(val, (int, float)) or not math.isfinite(val):
                    raise DataIntegrityError(f"Field {name} must be a finite number, got {val}")
                if val < 0.0:
                    raise DataIntegrityError(f"Field {name} must be non-negative, got {val}")


@dataclass
class TickerMetrics:
    """Summary evaluation metrics for one ticker across the evaluated horizon."""

    ticker: str
    days_count: int
    close_mae: float
    close_rmse: float
    close_mape_pct: float
    directional_accuracy_pct: float | None = None
    directional_hits: int = 0
    directional_opportunities: int = 0
    band_coverage_pct: float | None = None
    band_hits: int = 0
    band_opportunities: int = 0
    avg_band_width_usd: float | None = None
    range_capture_pct: float | None = None
    range_hits: int = 0
    cumulative_actual_return_pct: float | None = None
    cumulative_pred_return_pct: float | None = None
    cumulative_return_drift_pct: float | None = None


@dataclass
class EvaluationSummary:
    """Overall evaluation summary across all tickers and trading days."""

    start_date: str
    end_date: str
    total_days: int
    tickers_evaluated: list[str]
    aggregate_close_mae: float
    aggregate_close_rmse: float
    aggregate_close_mape_pct: float
    overall_directional_accuracy_pct: float | None
    overall_band_coverage_pct: float | None
    ticker_metrics: dict[str, TickerMetrics] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert metrics to a clean serializable dictionary."""
        return {
            "start_date": self.start_date,
            "end_date": self.end_date,
            "total_days": self.total_days,
            "tickers_evaluated": self.tickers_evaluated,
            "aggregate_close_mae": round(self.aggregate_close_mae, 4),
            "aggregate_close_rmse": round(self.aggregate_close_rmse, 4),
            "aggregate_close_mape_pct": round(self.aggregate_close_mape_pct, 4),
            "overall_directional_accuracy_pct": (
                round(self.overall_directional_accuracy_pct, 4)
                if self.overall_directional_accuracy_pct is not None
                else None
            ),
            "overall_band_coverage_pct": (
                round(self.overall_band_coverage_pct, 4)
                if self.overall_band_coverage_pct is not None
                else None
            ),
            "ticker_metrics": {k: asdict(v) for k, v in self.ticker_metrics.items()},
        }


def _safe_float(val: Any) -> float | None:
    """Safely convert a value to finite float, returning None if absent or NaN."""
    if val is None or val == "" or val == "N/A" or val == "nan":
        return None
    if isinstance(val, bool):
        return None
    try:
        num = float(val)
        return num if math.isfinite(num) else None
    except (ValueError, TypeError):
        return None


def load_actuals_from_csv(path: Path) -> dict[str, dict[str, float]]:
    """Load actual market history from a CSV file into a date-keyed dictionary.

    Args:
        path: Path to ticker history CSV with Date, Open, High, Low, Close.

    Returns:
        Dictionary mapping ISO date 'YYYY-MM-DD' to a dictionary of OHLC prices.
    """
    if not path.is_file():
        raise DataIntegrityError(f"Actuals history file not found: {path}")

    records: dict[str, dict[str, float]] = {}
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_date = row.get("Date") or row.get("date")
            if not raw_date:
                continue
            # Normalize to YYYY-MM-DD
            date_str = raw_date.strip().split(" ")[0].split("T")[0]
            # Prefer Adj Close if present for split adjustments, else Close
            close_val = _safe_float(row.get("Adj Close")) or _safe_float(row.get("Close"))
            if close_val is None:
                continue

            open_val = _safe_float(row.get("Open"))
            high_val = _safe_float(row.get("High"))
            low_val = _safe_float(row.get("Low"))

            records[date_str] = {
                "open": open_val if open_val is not None else close_val,
                "high": high_val if high_val is not None else close_val,
                "low": low_val if low_val is not None else close_val,
                "close": close_val,
            }
    return records


def load_predictions_from_csv(path: Path) -> list[dict[str, Any]]:
    """Load predictions from a CSV file.

    Expects columns such as:
    - Date / date / target_date
    - Ticker / ticker
    - Pred_Close / pred_close / predicted_close
    - Optional: pred_open, pred_high, pred_low, pred_close_p10, pred_close_p90

    Args:
        path: Path to predictions CSV.

    Returns:
        List of raw prediction dictionaries.
    """
    if not path.is_file():
        raise DataIntegrityError(f"Predictions file not found: {path}")

    predictions: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_date = row.get("Date") or row.get("date") or row.get("target_date")
            raw_ticker = row.get("Ticker") or row.get("ticker") or row.get("symbol")
            if not raw_date or not raw_ticker:
                continue

            date_str = raw_date.strip().split(" ")[0].split("T")[0]
            ticker_str = raw_ticker.strip().upper()

            pred_close = (
                _safe_float(row.get("Pred_Close"))
                or _safe_float(row.get("pred_close"))
                or _safe_float(row.get("predicted_close"))
                or _safe_float(row.get("pred_close_p50"))
                or _safe_float(row.get("close"))
            )
            if pred_close is None:
                continue

            pred_open = (
                _safe_float(row.get("Pred_Open"))
                or _safe_float(row.get("pred_open"))
                or _safe_float(row.get("predicted_open"))
            )
            pred_high = (
                _safe_float(row.get("Pred_High"))
                or _safe_float(row.get("pred_high"))
                or _safe_float(row.get("predicted_high"))
            )
            pred_low = (
                _safe_float(row.get("Pred_Low"))
                or _safe_float(row.get("pred_low"))
                or _safe_float(row.get("predicted_low"))
            )
            pred_p10 = (
                _safe_float(row.get("Pred_Close_P10"))
                or _safe_float(row.get("pred_close_p10"))
                or _safe_float(row.get("p10"))
            )
            pred_p90 = (
                _safe_float(row.get("Pred_Close_P90"))
                or _safe_float(row.get("pred_close_p90"))
                or _safe_float(row.get("p90"))
            )

            predictions.append({
                "date": date_str,
                "ticker": ticker_str,
                "pred_close": pred_close,
                "pred_open": pred_open,
                "pred_high": pred_high,
                "pred_low": pred_low,
                "pred_close_p10": pred_p10,
                "pred_close_p90": pred_p90,
            })
    return predictions


class SequesteredComparator:
    """Authoritative comparator evaluating predictions against ground-truth actuals."""

    def __init__(self, ground_truth_dir: Path | None = None) -> None:
        """Initialize the comparator.

        Args:
            ground_truth_dir: Optional path to the directory containing per-ticker
                CSV history files (defaulting to repository ticker_data).
        """
        self.ground_truth_dir = ground_truth_dir

    def evaluate_ticker_series(
        self,
        ticker: str,
        predictions: Sequence[dict[str, Any]],
        actuals: dict[str, dict[str, float]],
    ) -> tuple[TickerMetrics, list[DailyRecord]]:
        """Evaluate a single ticker's predictions against actual market bars.

        Args:
            ticker: Ticker symbol (e.g. 'AAPL').
            predictions: Chronological or unsorted prediction records.
            actuals: Date-keyed dictionary of actual OHLC market data.

        Returns:
            Tuple of computed TickerMetrics and list of aligned DailyRecords.
        """
        # Sort predictions by date
        sorted_preds = sorted(predictions, key=lambda x: x["date"])

        # Deduplicate predictions by date (fail closed on conflicting duplicate predictions)
        seen_dates: dict[str, dict[str, Any]] = {}
        for p in sorted_preds:
            d = p["date"]
            if d in seen_dates:
                if abs(seen_dates[d]["pred_close"] - p["pred_close"]) > 1e-6:
                    raise DataIntegrityError(f"Duplicate conflicting predictions for {ticker} on {d}")
            seen_dates[d] = p

        aligned_records: list[DailyRecord] = []
        all_actual_dates = sorted(actuals.keys())

        for p in sorted_preds:
            d = p["date"]
            if d not in actuals:
                continue

            act = actuals[d]

            # Find previous day's close for directional return comparison
            prev_close: float | None = None
            d_idx = all_actual_dates.index(d) if d in all_actual_dates else -1
            if d_idx > 0:
                prev_date = all_actual_dates[d_idx - 1]
                prev_close = actuals[prev_date]["close"]

            rec = DailyRecord(
                date=d,
                ticker=ticker,
                actual_close=act["close"],
                pred_close=p["pred_close"],
                actual_open=act.get("open"),
                pred_open=p.get("pred_open"),
                actual_high=act.get("high"),
                pred_high=p.get("pred_high"),
                actual_low=act.get("low"),
                pred_low=p.get("pred_low"),
                pred_close_p10=p.get("pred_close_p10"),
                pred_close_p90=p.get("pred_close_p90"),
                prev_actual_close=prev_close,
            )
            aligned_records.append(rec)

        if not aligned_records:
            raise DataIntegrityError(f"No aligned prediction-actual records found for ticker {ticker}")

        # Compute point errors
        abs_errors = [abs(r.pred_close - r.actual_close) for r in aligned_records]
        sq_errors = [(r.pred_close - r.actual_close) ** 2 for r in aligned_records]
        pct_errors = [abs(r.pred_close - r.actual_close) / r.actual_close * 100.0 for r in aligned_records]

        n = len(aligned_records)
        mae = sum(abs_errors) / n
        rmse = math.sqrt(sum(sq_errors) / n)
        mape = sum(pct_errors) / n

        # Directional accuracy
        dir_hits = 0
        dir_opps = 0
        for r in aligned_records:
            if r.prev_actual_close is not None and r.prev_actual_close > 0:
                actual_ret = (r.actual_close - r.prev_actual_close) / r.prev_actual_close
                pred_ret = (r.pred_close - r.prev_actual_close) / r.prev_actual_close
                if (actual_ret >= 0 and pred_ret >= 0) or (actual_ret < 0 and pred_ret < 0):
                    dir_hits += 1
                dir_opps += 1

        dir_acc_pct = (dir_hits / dir_opps * 100.0) if dir_opps > 0 else None

        # Band coverage
        band_hits = 0
        band_opps = 0
        band_widths: list[float] = []
        for r in aligned_records:
            if r.pred_close_p10 is not None and r.pred_close_p90 is not None:
                band_opps += 1
                band_widths.append(r.pred_close_p90 - r.pred_close_p10)
                if r.pred_close_p10 <= r.actual_close <= r.pred_close_p90:
                    band_hits += 1

        band_cov_pct = (band_hits / band_opps * 100.0) if band_opps > 0 else None
        avg_band_w = (sum(band_widths) / len(band_widths)) if band_widths else None

        # Range capture
        range_hits = 0
        range_opps = 0
        for r in aligned_records:
            if r.actual_low is not None and r.actual_high is not None:
                range_opps += 1
                if r.actual_low <= r.pred_close <= r.actual_high:
                    range_hits += 1
        range_cap_pct = (range_hits / range_opps * 100.0) if range_opps > 0 else None

        # Cumulative trajectory return
        first_close = aligned_records[0].prev_actual_close or aligned_records[0].actual_close
        last_actual = aligned_records[-1].actual_close
        cum_actual_ret = ((last_actual - first_close) / first_close * 100.0) if first_close > 0 else 0.0

        # Cumulative predicted path (multiplying daily predicted return increments)
        cum_pred_mult = 1.0
        for r in aligned_records:
            base = r.prev_actual_close or r.actual_close
            if base > 0:
                daily_pred_ret = (r.pred_close - base) / base
                cum_pred_mult *= (1.0 + daily_pred_ret)
        cum_pred_ret = (cum_pred_mult - 1.0) * 100.0
        drift = cum_pred_ret - cum_actual_ret

        metrics = TickerMetrics(
            ticker=ticker,
            days_count=n,
            close_mae=round(mae, 4),
            close_rmse=round(rmse, 4),
            close_mape_pct=round(mape, 4),
            directional_accuracy_pct=round(dir_acc_pct, 2) if dir_acc_pct is not None else None,
            directional_hits=dir_hits,
            directional_opportunities=dir_opps,
            band_coverage_pct=round(band_cov_pct, 2) if band_cov_pct is not None else None,
            band_hits=band_hits,
            band_opportunities=band_opps,
            avg_band_width_usd=round(avg_band_w, 2) if avg_band_w is not None else None,
            range_capture_pct=round(range_cap_pct, 2) if range_cap_pct is not None else None,
            range_hits=range_hits,
            cumulative_actual_return_pct=round(cum_actual_ret, 2),
            cumulative_pred_return_pct=round(cum_pred_ret, 2),
            cumulative_return_drift_pct=round(drift, 2),
        )

        return metrics, aligned_records

    def evaluate_all(
        self,
        predictions: Sequence[dict[str, Any]],
        actuals_by_ticker: dict[str, dict[str, dict[str, float]]] | None = None,
    ) -> tuple[EvaluationSummary, list[DailyRecord]]:
        """Evaluate a multi-ticker set of predictions against actual market bars.

        Args:
            predictions: Collection of prediction records.
            actuals_by_ticker: Optional pre-loaded actuals map
                {ticker: {date: {open, high, low, close}}}. If omitted,
                loaded from `self.ground_truth_dir / f"{ticker}.csv"`.

        Returns:
            Tuple of EvaluationSummary and list of all aligned DailyRecords.
        """
        # Group predictions by ticker
        by_ticker: dict[str, list[dict[str, Any]]] = {}
        for p in predictions:
            t = p["ticker"].upper()
            by_ticker.setdefault(t, []).append(p)

        all_records: list[DailyRecord] = []
        metrics_by_ticker: dict[str, TickerMetrics] = {}

        for ticker, preds in sorted(by_ticker.items()):
            if actuals_by_ticker and ticker in actuals_by_ticker:
                act = actuals_by_ticker[ticker]
            elif self.ground_truth_dir:
                csv_path = self.ground_truth_dir / f"{ticker}.csv"
                if not csv_path.exists():
                    print(f"Warning: No ground truth CSV for ticker {ticker} at {csv_path}, skipping", file=sys.stderr)
                    continue
                act = load_actuals_from_csv(csv_path)
            else:
                raise DataIntegrityError(f"No ground truth data source provided for ticker {ticker}")

            t_metrics, t_records = self.evaluate_ticker_series(ticker, preds, act)
            metrics_by_ticker[ticker] = t_metrics
            all_records.extend(t_records)

        if not metrics_by_ticker:
            raise DataIntegrityError("No tickers were successfully evaluated")

        # Global aggregations across all daily records
        all_abs_errors = [abs(r.pred_close - r.actual_close) for r in all_records]
        all_sq_errors = [(r.pred_close - r.actual_close) ** 2 for r in all_records]
        all_pct_errors = [abs(r.pred_close - r.actual_close) / r.actual_close * 100.0 for r in all_records]

        total_obs = len(all_records)
        agg_mae = sum(all_abs_errors) / total_obs
        agg_rmse = math.sqrt(sum(all_sq_errors) / total_obs)
        agg_mape = sum(all_pct_errors) / total_obs

        # Global directional accuracy
        tot_dir_hits = sum(m.directional_hits for m in metrics_by_ticker.values())
        tot_dir_opps = sum(m.directional_opportunities for m in metrics_by_ticker.values())
        overall_dir_acc = (tot_dir_hits / tot_dir_opps * 100.0) if tot_dir_opps > 0 else None

        # Global band coverage
        tot_band_hits = sum(m.band_hits for m in metrics_by_ticker.values())
        tot_band_opps = sum(m.band_opportunities for m in metrics_by_ticker.values())
        overall_band_cov = (tot_band_hits / tot_band_opps * 100.0) if tot_band_opps > 0 else None

        all_dates = sorted(list({r.date for r in all_records}))
        start_d = all_dates[0] if all_dates else ""
        end_d = all_dates[-1] if all_dates else ""

        summary = EvaluationSummary(
            start_date=start_d,
            end_date=end_d,
            total_days=len(all_dates),
            tickers_evaluated=sorted(list(metrics_by_ticker.keys())),
            aggregate_close_mae=agg_mae,
            aggregate_close_rmse=agg_rmse,
            aggregate_close_mape_pct=agg_mape,
            overall_directional_accuracy_pct=overall_dir_acc,
            overall_band_coverage_pct=overall_band_cov,
            ticker_metrics=metrics_by_ticker,
        )

        return summary, all_records


def render_markdown_report(summary: EvaluationSummary, records: Sequence[DailyRecord]) -> str:
    """Generate a clean, professional Markdown evaluation report."""
    lines: list[str] = [
        "# Sequestered Truth Comparator: 90-Day Evaluation Report",
        "",
        "> **Notice**: This report was produced by an independent, sequestered evaluation harness.",
        "> Neither the underlying prediction models nor training data had access to this comparator.",
        "",
        "## 1. Executive Summary",
        "",
        f"- **Evaluation Period**: `{summary.start_date}` to `{summary.end_date}` ({summary.total_days} trading days)",
        f"- **Tickers Evaluated**: `{', '.join(summary.tickers_evaluated)}`",
        f"- **Aggregate Mean Absolute Error (MAE)**: `${summary.aggregate_close_mae:,.2f}`",
        f"- **Aggregate Root Mean Squared Error (RMSE)**: `${summary.aggregate_close_rmse:,.2f}`",
        f"- **Aggregate Percentage Error (MAPE)**: `{summary.aggregate_close_mape_pct:.2f}%`",
    ]

    if summary.overall_directional_accuracy_pct is not None:
        lines.append(f"- **Overall Directional Accuracy (Hit Rate)**: `{summary.overall_directional_accuracy_pct:.2f}%`")
    if summary.overall_band_coverage_pct is not None:
        lines.append(f"- **Overall P10-P90 Band Coverage**: `{summary.overall_band_coverage_pct:.2f}%` (Nominal target: 80%)")

    lines.extend([
        "",
        "## 2. Per-Ticker Evaluation Breakdown",
        "",
        "| Ticker | Days | MAE ($) | RMSE ($) | MAPE (%) | Direction Hit Rate | Band Coverage | Range Capture | Cumul. Actual Ret | Cumul. Pred Ret | Drift |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ])

    for ticker, m in sorted(summary.ticker_metrics.items()):
        dir_str = f"{m.directional_accuracy_pct:.1f}% ({m.directional_hits}/{m.directional_opportunities})" if m.directional_accuracy_pct is not None else "N/A"
        band_str = f"{m.band_coverage_pct:.1f}% ({m.band_hits}/{m.band_opportunities})" if m.band_coverage_pct is not None else "N/A"
        range_str = f"{m.range_capture_pct:.1f}%" if m.range_capture_pct is not None else "N/A"
        act_ret_str = f"{m.cumulative_actual_return_pct:+.2f}%" if m.cumulative_actual_return_pct is not None else "N/A"
        pred_ret_str = f"{m.cumulative_pred_return_pct:+.2f}%" if m.cumulative_pred_return_pct is not None else "N/A"
        drift_str = f"{m.cumulative_return_drift_pct:+.2f}%" if m.cumulative_return_drift_pct is not None else "N/A"

        lines.append(
            f"| **{ticker}** | {m.days_count} | ${m.close_mae:.2f} | ${m.close_rmse:.2f} | {m.close_mape_pct:.2f}% | "
            f"{dir_str} | {band_str} | {range_str} | {act_ret_str} | {pred_ret_str} | {drift_str} |"
        )

    lines.extend([
        "",
        "## 3. Recent 10 Trading Days Comparison Sample",
        "",
        "| Date | Ticker | Predicted Close | Actual Close | Abs Error ($) | Error (%) | Direction Match | In Band |",
        "| :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |",
    ])

    # Show last 10 records per ticker or last 20 overall
    sorted_recs = sorted(records, key=lambda r: (r.date, r.ticker), reverse=True)
    sample_recs = sorted_recs[:20]
    for r in sample_recs:
        abs_err = abs(r.pred_close - r.actual_close)
        err_pct = abs_err / r.actual_close * 100.0
        dir_match = "—"
        if r.prev_actual_close and r.prev_actual_close > 0:
            act_up = r.actual_close >= r.prev_actual_close
            pred_up = r.pred_close >= r.prev_actual_close
            dir_match = "✓ Hit" if (act_up == pred_up) else "✗ Miss"

        in_band = "—"
        if r.pred_close_p10 is not None and r.pred_close_p90 is not None:
            in_band = "✓ Inside" if (r.pred_close_p10 <= r.actual_close <= r.pred_close_p90) else "✗ Outside"

        lines.append(
            f"| {r.date} | **{r.ticker}** | ${r.pred_close:,.2f} | ${r.actual_close:,.2f} | ${abs_err:,.2f} | "
            f"{err_pct:.2f}% | {dir_match} | {in_band} |"
        )

    lines.append("")
    return "\n".join(lines)


def export_daily_ledger_csv(records: Sequence[DailyRecord], output_path: Path) -> None:
    """Export detailed aligned comparison records to CSV."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "date",
        "ticker",
        "actual_close",
        "pred_close",
        "abs_error",
        "error_pct",
        "prev_actual_close",
        "actual_return_pct",
        "pred_return_pct",
        "directional_hit",
        "actual_open",
        "pred_open",
        "actual_high",
        "pred_high",
        "actual_low",
        "pred_low",
        "pred_close_p10",
        "pred_close_p90",
        "in_band",
    ]

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in sorted(records, key=lambda x: (x.ticker, x.date)):
            abs_err = abs(r.pred_close - r.actual_close)
            err_pct = abs_err / r.actual_close * 100.0 if r.actual_close > 0 else 0.0

            act_ret = None
            pred_ret = None
            dir_hit = None
            if r.prev_actual_close and r.prev_actual_close > 0:
                act_ret = (r.actual_close - r.prev_actual_close) / r.prev_actual_close * 100.0
                pred_ret = (r.pred_close - r.prev_actual_close) / r.prev_actual_close * 100.0
                dir_hit = bool((act_ret >= 0 and pred_ret >= 0) or (act_ret < 0 and pred_ret < 0))

            in_band = None
            if r.pred_close_p10 is not None and r.pred_close_p90 is not None:
                in_band = bool(r.pred_close_p10 <= r.actual_close <= r.pred_close_p90)

            writer.writerow({
                "date": r.date,
                "ticker": r.ticker,
                "actual_close": round(r.actual_close, 4),
                "pred_close": round(r.pred_close, 4),
                "abs_error": round(abs_err, 4),
                "error_pct": round(err_pct, 4),
                "prev_actual_close": round(r.prev_actual_close, 4) if r.prev_actual_close else "",
                "actual_return_pct": round(act_ret, 4) if act_ret is not None else "",
                "pred_return_pct": round(pred_ret, 4) if pred_ret is not None else "",
                "directional_hit": dir_hit if dir_hit is not None else "",
                "actual_open": round(r.actual_open, 4) if r.actual_open else "",
                "pred_open": round(r.pred_open, 4) if r.pred_open else "",
                "actual_high": round(r.actual_high, 4) if r.actual_high else "",
                "pred_high": round(r.pred_high, 4) if r.pred_high else "",
                "actual_low": round(r.actual_low, 4) if r.actual_low else "",
                "pred_low": round(r.pred_low, 4) if r.pred_low else "",
                "pred_close_p10": round(r.pred_close_p10, 4) if r.pred_close_p10 else "",
                "pred_close_p90": round(r.pred_close_p90, 4) if r.pred_close_p90 else "",
                "in_band": in_band if in_band is not None else "",
            })


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for the sequestered comparator."""
    parser = argparse.ArgumentParser(
        prog="sequestered_truth_comparator.py",
        description="Independent, sequestered evaluation of stock price predictions against market actuals.",
    )
    parser.add_argument(
        "--predictions",
        type=Path,
        required=True,
        help="Path to CSV file containing predictions.",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="Path to archive directory containing per-ticker CSV files (defaults to ticker_data).",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=None,
        help="Filter evaluation to specific tickers.",
    )
    parser.add_argument(
        "--report-md",
        type=Path,
        default=None,
        help="Path to save the Markdown evaluation report.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="Path to save the structured JSON summary metrics.",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="Path to save the detailed daily aligned records CSV.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the sequestered comparator."""
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        # Determine history dir
        history_dir = args.history_dir
        if history_dir is None:
            # Default to repo root / ticker_data
            default_dir = Path(__file__).resolve().parent.parent / "ticker_data"
            if default_dir.is_dir():
                history_dir = default_dir

        if history_dir is None or not history_dir.is_dir():
            print(f"REFUSED: Ground truth history directory not found: {history_dir}", file=sys.stderr)
            return 1

        print(f"Loading predictions from: {args.predictions}")
        predictions = load_predictions_from_csv(args.predictions)
        if not predictions:
            print(f"REFUSED: No valid predictions loaded from {args.predictions}", file=sys.stderr)
            return 1

        if args.tickers:
            filter_tickers = {t.upper() for t in args.tickers}
            predictions = [p for p in predictions if p["ticker"] in filter_tickers]
            if not predictions:
                print(f"REFUSED: No predictions match filter tickers: {args.tickers}", file=sys.stderr)
                return 1

        comparator = SequesteredComparator(ground_truth_dir=history_dir)
        summary, records = comparator.evaluate_all(predictions)

        # Output Markdown report
        md_text = render_markdown_report(summary, records)
        if args.report_md:
            args.report_md.parent.mkdir(parents=True, exist_ok=True)
            args.report_md.write_text(md_text, encoding="utf-8")
            print(f"Markdown report written to: {args.report_md}")
        else:
            print("\n" + md_text)

        # Output JSON summary
        if args.json_out:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(summary.to_dict(), indent=2), encoding="utf-8")
            print(f"JSON metrics written to: {args.json_out}")

        # Output CSV ledger
        if args.csv_out:
            export_daily_ledger_csv(records, args.csv_out)
            print(f"Daily comparison CSV ledger written to: {args.csv_out}")

        print("\nEvaluation completed successfully.")
        return 0

    except ComparatorError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
