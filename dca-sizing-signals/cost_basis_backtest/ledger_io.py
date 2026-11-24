"""The two seams to the Phase 0 outcome ledger (docs/adr-outcome-ledger-pit-harness.md).

The ledger is DuckDB; this package is stdlib only and never imports it or
market_data_ingest. Both seams are plain dicts:

* In: ``baseline_from_ledger_fills`` turns rows of the ledger's ``fills`` table
  (``LedgerStore.fills()``) into the harness's baseline cash flows. Those fills are the
  *graded* buys the live system made, which is the baseline this harness compares against.
  The ledger's own ``baseline_fills`` table is a different thing -- a flat 1x counterfactual
  (open question 4) -- and is not used here.
* Out: ``trial_row`` shapes a finished BacktestResult as one row for
  ``LedgerStore.ingest_trials``, so every variant ever run is counted when the deflated
  Sharpe ratio is computed later (ledger ``trial_count(experiment=...)``).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime

from cost_basis_backtest.engine import BacktestResult
from cost_basis_backtest.view import BacktestInputError, finite, parse_day

_FILL_KEYS = ("run_kind", "symbol", "session", "notional")


def baseline_from_ledger_fills(rows: Iterable[Mapping[str, object]]) -> dict[date, dict[str, float]]:
    """Sum scheduled fill notionals per (session, symbol). Manual-run fills never enter, as
    in every scheduled metric (docs/run_classification.md). Any malformed row fails the whole
    conversion."""
    out: dict[date, dict[str, float]] = {}
    for i, row in enumerate(rows):
        where = f"fills[{i}]"
        if not isinstance(row, Mapping) or any(k not in row for k in _FILL_KEYS):
            raise BacktestInputError(f"{where}: needs {', '.join(_FILL_KEYS)}")
        kind = row["run_kind"]
        if kind not in ("scheduled", "manual"):
            raise BacktestInputError(f"{where}: run_kind must be scheduled or manual, got {kind!r}")
        if kind == "manual":
            continue
        symbol = row["symbol"]
        if not isinstance(symbol, str) or not symbol:
            raise BacktestInputError(f"{where}: symbol must be a non-empty string")
        notional = finite(row["notional"], f"{where} notional")
        if notional <= 0.0:
            raise BacktestInputError(f"{where}: notional must be positive")
        day = out.setdefault(parse_day(row["session"], where), {})
        day[symbol] = day.get(symbol, 0.0) + notional
    return out


def _flat_numbers(prefix: str, value: object, out: dict[str, float]) -> None:
    if isinstance(value, Mapping):
        for k, v in value.items():
            _flat_numbers(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
        out[prefix] = float(value)


def trial_row(result: BacktestResult, *, trial_id: str, experiment: str, config: Mapping[str, object],
              evaluated_at: datetime, period: str = "out_of_sample") -> dict[str, object]:
    """One ``trials`` row. ``metrics`` holds the scalar metrics of ``period`` (falling back
    to ``all`` when the run had no out-of-sample split) flattened to dotted names; the
    ledger's ``sharpe`` column takes the daily active Sharpe. Non-finite values are dropped
    because the ledger refuses them. A rejected result is refused: it has no metrics, and it
    still counts as a trial, so record it with ``rejected_trial_row`` instead."""
    if result.status != "ok":
        raise ValueError("rejected result: use rejected_trial_row so the trial is still counted")
    periods = result.metrics.get("periods", {})
    chosen = period if period in periods else "all"
    data = periods[chosen]
    metrics: dict[str, float] = {}
    _flat_numbers("", {
        "cost_basis_mean_bp": data["cost_basis"]["aggregate"].get("mean_bp"),
        "cost_basis_dollar_weighted_bp": data["cost_basis"]["aggregate"].get("dollar_weighted_mean_bp"),
        "terminal_wealth_difference_bp": data["terminal_wealth_per_dollar"]["difference_bp"],
        "tilt_return_bp": {h: v["return_bp"] for h, v in data["tilt_forward_return"].items()},
        "hit_rate": {h: v["rate"] for h, v in data["hit_rate"].items()},
        "worst_rolling_underperformance_bp": data["worst_rolling_underperformance_bp"],
        "gross_tilt_fraction": data["turnover"]["gross_tilt_fraction"],
        "promotion_passed": 1.0 if result.promotion.get("passed") else 0.0,
    }, metrics)
    if data["active_daily_sharpe"] is not None:
        metrics["sharpe"] = float(data["active_daily_sharpe"])
    return {
        "trial_id": trial_id,
        "experiment": experiment,
        "config": {**dict(config), "period": chosen},
        "metrics": metrics,
        "n_observations": int(data["active_n_days"]),
        "evaluated_at": evaluated_at,
    }


def rejected_trial_row(result: BacktestResult, *, trial_id: str, experiment: str, config: Mapping[str, object],
                       evaluated_at: datetime) -> dict[str, object]:
    """A rejected run is still a trial: recording it keeps the deflation honest."""
    return {
        "trial_id": trial_id,
        "experiment": experiment,
        "config": {**dict(config), "status": result.status, "reason": result.reason},
        "metrics": {"rejected": 1.0},
        "n_observations": 0,
        "evaluated_at": evaluated_at,
    }
