"""
Evaluation harness for predictive-low deferral parameters.

Runs a simulation across historical data using different parameter combinations,
collecting metrics to identify which thresholds maximize value capture.

Exit code: 0 success, 1 missing/corrupt data, 2 runtime failure.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


@dataclass
class DeferralParams:
    """One parameter combination to evaluate."""

    price_drop_threshold_pct: float
    confidence_threshold: float
    wait_time_days: int
    escalation_rebound_pct: float
    capital_reserve_pct: float

    def name(self) -> str:
        """Short identifier for this param set."""
        return (
            f"drop{self.price_drop_threshold_pct:.0%}_conf{self.confidence_threshold:.0%}_"
            f"wait{self.wait_time_days}d_rebound{self.escalation_rebound_pct:.0%}"
        )


@dataclass
class DeferralOutcome:
    """What happened to one deferred ticker."""

    ticker: str
    defer_date: date
    predicted_low: float
    actual_low: float
    actual_buy_price: float
    days_until_hit: Optional[int]
    days_until_escalate: Optional[int]
    days_until_miss: Optional[int]
    status: str  # "hit" | "escalated" | "missed"
    value_captured_usd: float  # (predicted_low - actual_buy_price) * 1.0 (normalized)
    capital_reserved_usd: float
    params: DeferralParams = field(repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ticker": self.ticker,
            "defer_date": self.defer_date.isoformat(),
            "predicted_low": self.predicted_low,
            "actual_low": self.actual_low,
            "actual_buy_price": self.actual_buy_price,
            "days_until_hit": self.days_until_hit,
            "days_until_escalate": self.days_until_escalate,
            "days_until_miss": self.days_until_miss,
            "status": self.status,
            "value_captured_usd": self.value_captured_usd,
            "capital_reserved_usd": self.capital_reserved_usd,
        }


@dataclass
class MetricsPerParamSet:
    """Aggregated metrics for one parameter combination."""

    params: DeferralParams
    total_deferred: int = 0
    hit_count: int = 0
    escalated_count: int = 0
    missed_count: int = 0
    total_value_captured_usd: float = 0.0
    total_capital_reserved_usd: float = 0.0
    avg_days_waited: float = 0.0
    outcomes: List[DeferralOutcome] = field(default_factory=list)

    def hit_rate(self) -> float:
        return self.hit_count / self.total_deferred if self.total_deferred > 0 else 0.0

    def escalation_rate(self) -> float:
        return (
            self.escalated_count / self.total_deferred if self.total_deferred > 0 else 0.0
        )

    def avg_value_per_defer(self) -> float:
        return (
            self.total_value_captured_usd / self.total_deferred
            if self.total_deferred > 0
            else 0.0
        )

    def capital_efficiency(self) -> float:
        """Ratio of value captured to capital reserved."""
        return (
            self.total_value_captured_usd / self.total_capital_reserved_usd
            if self.total_capital_reserved_usd > 0
            else 0.0
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "params": {
                "price_drop_threshold_pct": self.params.price_drop_threshold_pct,
                "confidence_threshold": self.params.confidence_threshold,
                "wait_time_days": self.params.wait_time_days,
                "escalation_rebound_pct": self.params.escalation_rebound_pct,
                "capital_reserve_pct": self.params.capital_reserve_pct,
            },
            "total_deferred": self.total_deferred,
            "hit_count": self.hit_count,
            "escalated_count": self.escalated_count,
            "missed_count": self.missed_count,
            "hit_rate": self.hit_rate(),
            "escalation_rate": self.escalation_rate(),
            "total_value_captured_usd": self.total_value_captured_usd,
            "total_capital_reserved_usd": self.total_capital_reserved_usd,
            "avg_value_per_defer": self.avg_value_per_defer(),
            "capital_efficiency": self.capital_efficiency(),
            "avg_days_waited": self.avg_days_waited,
        }


class DeferralEvaluator:
    """Simulates deferral logic across historical data."""

    def __init__(
        self,
        octopus_dir: Optional[Path],
        ticker_history_dir: Optional[Path],
        start_date: date,
        end_date: date,
    ):
        self.octopus_dir = octopus_dir
        self.ticker_history_dir = ticker_history_dir
        self.start_date = start_date
        self.end_date = end_date
        self.price_history: Dict[str, List[Tuple[date, float, float]]] = {}

    def load_price_history(self) -> bool:
        """Load ticker price histories. Returns True if successful."""
        if not self.ticker_history_dir:
            logger.warning("No ticker history dir; using synthetic data")
            return self._generate_synthetic_prices()

        self.ticker_history_dir = Path(self.ticker_history_dir)
        if not self.ticker_history_dir.exists():
            logger.error(f"Ticker history dir not found: {self.ticker_history_dir}")
            return False

        csv_files = list(self.ticker_history_dir.glob("*.csv"))
        if not csv_files:
            logger.warning(
                f"No CSV files in {self.ticker_history_dir}; using synthetic data"
            )
            return self._generate_synthetic_prices()

        for csv_file in csv_files[:5]:  # Load first 5 for quick eval
            ticker = csv_file.stem.upper()
            try:
                with open(csv_file) as f:
                    reader = csv.reader(f)
                    try:
                        header = next(reader)
                        idx_date = header.index("Date")
                        idx_close = header.index("Close")
                        idx_low = header.index("Low")
                        max_idx = max(idx_date, idx_close, idx_low)
                    except (StopIteration, ValueError):
                        continue

                    prices = []
                    for row in reader:
                        # ⚡ Bolt Optimization: Use index-based access on csv.reader instead of csv.DictReader
                        # to avoid dictionary allocation overhead per row. Bound check avoids IndexError on short rows.
                        if len(row) > max_idx:
                            try:
                                row_date = date.fromisoformat(row[idx_date])
                                if self.start_date <= row_date <= self.end_date:
                                    close = float(row[idx_close])
                                    low = float(row[idx_low])
                                    prices.append((row_date, close, low))
                            except ValueError:
                                continue
                    if prices:
                        self.price_history[ticker] = sorted(prices)
                        logger.info(f"Loaded {len(prices)} rows for {ticker}")
            except Exception as e:
                logger.warning(f"Failed to load {csv_file}: {e}")

        return len(self.price_history) > 0

    def _generate_synthetic_prices(self) -> bool:
        """Generate synthetic price data for testing."""
        import random

        tickers = ["AAPL", "MSFT", "TSLA", "GOOGL", "AMZN"]

        for ticker in tickers:
            prices = []
            random.seed(hash(ticker) % 2**31)
            price = 150.0
            current_date = self.start_date

            while current_date <= self.end_date:
                daily_ret = random.gauss(0.0005, 0.015)
                price *= 1 + daily_ret

                low_pct = random.uniform(0.94, 0.99)
                low = price * low_pct

                prices.append((current_date, price, low))
                current_date += timedelta(days=1)

            self.price_history[ticker] = prices
            logger.info(f"Generated {len(prices)} synthetic rows for {ticker}")

        return True

    def simulate_deferral(
        self, params: DeferralParams, ticker: str, defer_date: date, predicted_low: float
    ) -> Optional[DeferralOutcome]:
        """Simulate one deferral decision with given params. Returns outcome or None."""
        if ticker not in self.price_history:
            return None

        prices = self.price_history[ticker]
        price_dict = {d: (c, low) for d, c, low in prices}

        if defer_date not in price_dict:
            return None

        today_close, today_low = price_dict[defer_date]
        capital_reserved = today_close * params.capital_reserve_pct

        wait_end = defer_date + timedelta(days=params.wait_time_days)
        escalate_threshold = today_close * (1 + params.escalation_rebound_pct)

        days_waited = 0
        actual_buy_price = today_close
        days_until_hit = None
        days_until_escalate = None
        days_until_miss = None
        status = "missed"

        for _i, (check_date, close, low) in enumerate(prices):
            if check_date <= defer_date:
                continue

            days_waited = (check_date - defer_date).days

            if low <= predicted_low * 1.01 and days_until_hit is None:
                days_until_hit = days_waited
                actual_buy_price = min(low, predicted_low)
                status = "hit"
                break

            if close >= escalate_threshold and days_until_escalate is None:
                days_until_escalate = days_waited
                actual_buy_price = escalate_threshold
                status = "escalated"
                break

            if check_date >= wait_end:
                days_until_miss = days_waited
                actual_buy_price = close
                status = "missed"
                break

        value_captured = (predicted_low - actual_buy_price)

        return DeferralOutcome(
            ticker=ticker,
            defer_date=defer_date,
            predicted_low=predicted_low,
            actual_low=today_low,
            actual_buy_price=actual_buy_price,
            days_until_hit=days_until_hit,
            days_until_escalate=days_until_escalate,
            days_until_miss=days_until_miss,
            status=status,
            value_captured_usd=value_captured,
            capital_reserved_usd=capital_reserved,
            params=params,
        )

    def evaluate_params(
        self, param_sets: List[DeferralParams], num_simulations: int = 100
    ) -> List[MetricsPerParamSet]:
        """Run simulations for each param set. Returns ranked metrics."""
        results = []

        for params in param_sets:
            metrics = MetricsPerParamSet(params=params)
            logger.info(f"Evaluating {params.name()}")

            sim_count = 0
            for ticker in list(self.price_history.keys())[:3]:
                prices = self.price_history[ticker]
                for i in range(1, min(len(prices), num_simulations)):
                    defer_date, close, _ = prices[i]
                    predicted_low = close * (1 - 0.05)

                    outcome = self.simulate_deferral(params, ticker, defer_date, predicted_low)
                    if outcome:
                        metrics.outcomes.append(outcome)
                        metrics.total_deferred += 1
                        if outcome.status == "hit":
                            metrics.hit_count += 1
                        elif outcome.status == "escalated":
                            metrics.escalated_count += 1
                        else:
                            metrics.missed_count += 1

                        metrics.total_value_captured_usd += outcome.value_captured_usd
                        metrics.total_capital_reserved_usd += outcome.capital_reserved_usd
                        if outcome.days_until_hit:
                            metrics.avg_days_waited += outcome.days_until_hit
                        elif outcome.days_until_escalate:
                            metrics.avg_days_waited += outcome.days_until_escalate
                        else:
                            metrics.avg_days_waited += outcome.days_until_miss or 0

                        sim_count += 1
                        if sim_count >= num_simulations:
                            break

                if sim_count >= num_simulations:
                    break

            if metrics.total_deferred > 0:
                metrics.avg_days_waited /= metrics.total_deferred

            results.append(metrics)
            logger.info(
                f"  {params.name()}: {metrics.total_deferred} deferred, "
                f"{metrics.hit_rate():.1%} hit, efficiency={metrics.capital_efficiency():.2f}"
            )

        return sorted(results, key=lambda m: m.capital_efficiency(), reverse=True)


def generate_report(results: List[MetricsPerParamSet]) -> str:
    """Generate a human-readable report of parameter sensitivity."""
    lines = [
        "DEFERRAL PARAMETER EVALUATION REPORT",
        "=" * 80,
        "",
        "TOP 10 PARAMETER SETS (ranked by capital efficiency)",
        "-" * 80,
    ]

    for i, metrics in enumerate(results[:10], 1):
        p = metrics.params
        lines.extend([
            f"{i}. {metrics.params.name()}",
            f"   Hit Rate: {metrics.hit_rate():.1%} ({metrics.hit_count}/{metrics.total_deferred})",
            f"   Escalation Rate: {metrics.escalation_rate():.1%}",
            f"   Avg Value/Defer: ${metrics.avg_value_per_defer():.2f}",
            f"   Capital Efficiency: {metrics.capital_efficiency():.3f}",
            f"   Avg Days Waited: {metrics.avg_days_waited:.1f}",
            "",
        ])

    lines.extend([
        "SENSITIVITY ANALYSIS",
        "-" * 80,
        "Effect of price_drop_threshold_pct:",
    ])

    by_drop = {}
    for metrics in results:
        drop = metrics.params.price_drop_threshold_pct
        if drop not in by_drop:
            by_drop[drop] = []
        by_drop[drop].append(metrics)

    for drop in sorted(by_drop.keys()):
        avg_efficiency = sum(m.capital_efficiency() for m in by_drop[drop]) / len(
            by_drop[drop]
        )
        avg_hit_rate = sum(m.hit_rate() for m in by_drop[drop]) / len(by_drop[drop])
        lines.append(
            f"  {drop:.0%}: efficiency={avg_efficiency:.3f}, hit_rate={avg_hit_rate:.1%}"
        )

    lines.extend([
        "",
        "Effect of wait_time_days:",
    ])

    by_wait = {}
    for metrics in results:
        wait = metrics.params.wait_time_days
        if wait not in by_wait:
            by_wait[wait] = []
        by_wait[wait].append(metrics)

    for wait in sorted(by_wait.keys()):
        avg_efficiency = sum(m.capital_efficiency() for m in by_wait[wait]) / len(
            by_wait[wait]
        )
        avg_value = sum(m.avg_value_per_defer() for m in by_wait[wait]) / len(
            by_wait[wait]
        )
        lines.append(f"  {wait}d: efficiency={avg_efficiency:.3f}, avg_value=${avg_value:.2f}")

    lines.extend([
        "",
        "RECOMMENDATION",
        "-" * 80,
    ])

    if results:
        best = results[0]
        p = best.params
        lines.extend([
            f"Start with: {best.params.name()}",
            f"  price_drop_threshold: {p.price_drop_threshold_pct:.0%}",
            f"  confidence_threshold: {p.confidence_threshold:.0%}",
            f"  wait_time_days: {p.wait_time_days}",
            f"  escalation_rebound_pct: {p.escalation_rebound_pct:.0%}",
            f"  capital_reserve_pct: {p.capital_reserve_pct:.0%}",
            "",
            "Expected performance on test data:",
            f"  Hit rate: {best.hit_rate():.1%}",
            f"  Capital efficiency: {best.capital_efficiency():.3f}",
            f"  Avg value per defer: ${best.avg_value_per_defer():.2f}",
        ])

    return "\n".join(lines)


def main(
    octopus_dir: Optional[str] = None,
    ticker_history_dir: Optional[str] = None,
    output: str = "deferral_evaluation.json",
) -> int:
    """Run deferral parameter evaluation."""
    start = date(2024, 1, 1)
    end = date(2026, 9, 16)

    evaluator = DeferralEvaluator(
        octopus_dir=Path(octopus_dir) if octopus_dir else None,
        ticker_history_dir=Path(ticker_history_dir) if ticker_history_dir else None,
        start_date=start,
        end_date=end,
    )

    if not evaluator.load_price_history():
        logger.error("Failed to load price history")
        return 1

    param_grid = [
        DeferralParams(
            price_drop_threshold_pct=drop,
            confidence_threshold=conf,
            wait_time_days=wait,
            escalation_rebound_pct=0.02,
            capital_reserve_pct=0.5,
        )
        for drop in [0.03, 0.05, 0.07]
        for conf in [0.02, 0.05, 0.08]
        for wait in [1, 2, 3]
    ]

    logger.info(f"Testing {len(param_grid)} parameter combinations...")
    results = evaluator.evaluate_params(param_grid, num_simulations=50)

    output_path = Path(output)
    output_data = {
        "evaluation_date": date.today().isoformat(),
        "date_range": f"{start.isoformat()} to {end.isoformat()}",
        "total_params_tested": len(results),
        "ranked_results": [m.to_dict() for m in results[:10]],
    }

    with open(output_path, "w") as f:
        json.dump(output_data, f, indent=2)

    report = generate_report(results)
    print("\n" + report)

    logger.info(f"JSON results written to {output_path}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
