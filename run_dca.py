#!/usr/bin/env python3
"""Orchestrator and CLI for the near-close graded DCA strategy.

This is the single runnable entry point that ties the pieces together:

    config (dca_config) -> providers/broker -> run_daily_compact (near_close_dca)
        -> post-trade reports (dca_reporting) -> compact Notion sync (dca_notion_sync)

Two ways to run:

* **Live/scheduled** — the scheduler (which holds MCP/connector runtime access) builds
  the providers and broker from injected Robinhood MCP callables and calls
  :func:`run_pipeline` directly. Real placement stays gated behind ``config.dry_run`` and
  ``RobinhoodBroker.enable_live_orders``; the CLI never flips either.

* **Local/CI smoke test** — ``python run_dca.py --demo`` runs the whole pipeline against a
  deterministic in-memory data provider in dry-run mode, writing real artifacts and
  reports to a temp/output dir. Nothing contacts a broker or the network.

The CLI is intentionally dry-run only: placing real orders requires live MCP callables
that a bare Python process does not have, so live trading is driven from the scheduler
via :func:`run_pipeline`, not from this command line.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from dca_config import DEFAULT_CONFIG_DIR, load_strategy_config
from near_close_dca import (
    EASTERN,
    Bar,
    BrokerAdapter,
    MarketDataProvider,
    PriorSameDayAllocation,
    SourceName,
    StrategyConfig,
    StrategyState,
    build_broker,
    load_prior_same_day_allocations,
    run_daily_compact,
)

logger = logging.getLogger("near_close_dca.run")


def configure_logging(level: str = "INFO") -> None:
    """Configure a single stream handler for CLI/scheduler use (idempotent)."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def _fills_from_orders_csv(orders_csv: Path) -> List[Dict[str, Any]]:
    """Read filled orders from a compact-run orders.csv into report-ready fill dicts."""
    if not orders_csv.exists():
        return []
    fills: List[Dict[str, Any]] = []
    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if (row.get("status") or "").lower() != "filled":
                continue
            fills.append(
                {
                    "ticker": row.get("ticker"),
                    "fill_price": row.get("avg_fill_price"),
                    "filled_qty": row.get("quantity_filled"),
                    "order_id": row.get("order_id"),
                }
            )
    return fills


def _execution_targets_from_orders_csv(orders_csv: Path) -> Dict[str, Optional[float]]:
    """Map ticker -> the execution-record target price (signal-time quote) from orders.csv.

    The durable orders.csv records ``decision.target_price`` (the signal-time quote),
    while run_state's ``buy_candidates`` store the decision-bar close. Reconciling to the
    execution target keeps the full slippage report/Notion summary consistent with the
    orders CSV instead of ranking against a different price.
    """
    targets: Dict[str, Optional[float]] = {}
    if not orders_csv.exists():
        return targets
    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            ticker = row.get("ticker")
            if not ticker:
                continue
            raw = row.get("target_price")
            try:
                targets[ticker] = float(raw) if raw not in (None, "", "-") else None
            except (TypeError, ValueError):
                targets[ticker] = None
    return targets


def _refresh_fills_from_broker(
    orders_csv: Path, broker: BrokerAdapter, fills: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Re-query the broker for submitted-but-unfilled orders and append any fresh fills.

    The immediate post-trade fill lookup can miss a fill that settles moments later; by the
    3:45 reporting step it is available. Only orders not already recorded as filled are
    re-queried, and lookup failures are swallowed so reporting never crashes.
    """
    if not orders_csv.exists():
        return fills
    known = {f.get("order_id") for f in fills}
    refreshed = list(fills)
    with orders_csv.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            order_id = row.get("order_id")
            if not order_id or order_id in known or (row.get("status") or "").lower() == "filled":
                continue
            try:
                fill = broker.get_order_fill(order_id)
            except Exception:  # noqa: BLE001 - a fill re-query must never crash reporting
                logger.exception("fill refresh failed for %s", order_id)
                continue
            if fill is not None and fill.quantity_filled > 0 and fill.avg_fill_price > 0:
                refreshed.append(
                    {
                        "ticker": row.get("ticker"),
                        "fill_price": fill.avg_fill_price,
                        "filled_qty": fill.quantity_filled,
                        "order_id": order_id,
                    }
                )
                known.add(order_id)
    return refreshed


def _default_prior_allocation_state_root(artifacts_dir: Path) -> Path:
    """Derive the cron task root from a scheduled run's artifacts_dir.

    Per config/scheduled_tasks.json the live scheduled path writes into
    ``<task_root>/scheduled_runs/YYYYMMDD_1520ET/``, and prior same-day
    manual/morning allocations live under the sibling
    ``<task_root>/manual_runs/<timestamp>/``. A plain ``.parent`` would land on
    ``scheduled_runs/`` itself (which never contains ``manual_runs/``), so walk
    up looking for a ``scheduled_runs`` path segment and return its parent
    instead; fall back to the immediate parent when the layout doesn't match.
    """
    for candidate in (artifacts_dir, *artifacts_dir.parents):
        if candidate.name == "scheduled_runs":
            return candidate.parent
    return artifacts_dir.parent


def run_pipeline(
    now: datetime,
    config: StrategyConfig,
    state: StrategyState,
    finance_data: MarketDataProvider,
    robinhood_data: MarketDataProvider,
    broker: BrokerAdapter,
    *,
    artifacts_dir: str | Path,
    run_label: str = "daily",
    scheduled: bool = True,
    generate_full_reports: bool = False,
    prior_same_day_allocations: Optional[Dict[str, PriorSameDayAllocation]] = None,
    prior_allocation_state_root: Optional[str | Path] = None,
    deployment_history_root: Optional[str | Path] = None,
    classification_state_root: Optional[str | Path] = None,
) -> Dict[str, Any]:
    """Run one compact DCA cycle and (optionally) the full post-trade report set.

    Returns the merged dict of artifact/report paths and summary metrics. The compact
    daily path always runs; ``generate_full_reports=True`` additionally renders the
    slippage/coverage reports and the compact Notion sync from the run artifacts
    (heavier work reserved for Friday/manual runs).

    The same-day prior-run offset/top-up guard is scheduled-run-only. For a
    scheduled run, ``prior_allocation_state_root`` defaults to the cron task root
    derived from ``artifacts_dir`` (see ``_default_prior_allocation_state_root``)
    so the guard activates without every caller having to wire it explicitly;
    pass ``prior_same_day_allocations`` directly to override, or an explicit
    ``prior_allocation_state_root`` to scan a different tree. Manual runs never
    auto-load prior allocations, so a manual invocation is never guard-active.

    ``deployment_history_root`` feeds the trailing-7-day kill-switch (see
    ``dca_risk.apply_risk_guard``): it should point at the directory containing
    prior days' ``run_state.json`` artifacts. Defaults to ``artifacts_dir``'s
    parent, since the live task's per-day directories are its siblings.

    ``classification_state_root`` is passed through to ``near_close_dca.run_once``
    for a scheduled run: it is where ``latest_scheduled_run.json`` is
    written/updated (see ``scripts.run_classification``). Opt-in only: when not
    given, no ``latest_scheduled_run.json`` write is attempted at all, so a
    caller must pass the real cron_tracking root explicitly to enable it.
    """
    if scheduled and prior_same_day_allocations is None:
        if prior_allocation_state_root is None:
            prior_allocation_state_root = _default_prior_allocation_state_root(Path(artifacts_dir))
        trading_day = now.astimezone(EASTERN).date()
        prior_same_day_allocations = load_prior_same_day_allocations(
            prior_allocation_state_root, trading_day
        )
    if deployment_history_root is None:
        deployment_history_root = Path(artifacts_dir).parent
    result = run_daily_compact(
        now,
        config,
        state,
        finance_data,
        robinhood_data,
        broker,
        artifacts_dir=artifacts_dir,
        run_label=run_label,
        scheduled=scheduled,
        prior_same_day_allocations=prior_same_day_allocations,
        deployment_history_root=deployment_history_root,
        classification_state_root=classification_state_root,
    )
    if result.get("blocked"):
        logger.warning("run blocked by date gate for %s; skipping reports", result.get("run_state_path"))
        return result
    if not generate_full_reports:
        return result

    # Imported lazily so the compact daily path has no dependency on the report layer.
    from dca_notion_sync import write_compact_sync
    from dca_reporting import generate_reports

    out = Path(artifacts_dir)
    run_state = json.loads((out / "run_state.json").read_text(encoding="utf-8"))
    orders_csv = out / "orders.csv"
    fills = _fills_from_orders_csv(orders_csv)
    # A fill can land between placement and this (later) reporting step; re-query the broker
    # for any submitted-but-unfilled order so it is not reported as a missing execution gap.
    fills = _refresh_fills_from_broker(orders_csv, broker, fills)
    # Align candidate target prices with the durable execution records so the slippage
    # report ranks against the same target the orders CSV uses (the signal-time quote).
    targets = _execution_targets_from_orders_csv(orders_csv)
    for candidate in run_state.get("buy_candidates") or []:
        target = targets.get(candidate.get("ticker"))
        if target is not None:
            candidate["target_price"] = target
            candidate["target_price_missing"] = False
    report_paths = generate_reports(run_state, fills, out)
    summary = json.loads(Path(report_paths["latest_run_summary"]).read_text(encoding="utf-8"))
    sync_paths = write_compact_sync(summary, out)
    result["reports"] = report_paths
    result["notion_sync"] = sync_paths
    logger.info("Generated full post-trade reports in %s", out)
    return result


class DemoDataProvider:
    """Deterministic in-memory :class:`MarketDataProvider` for ``--demo`` / CI.

    Generates a downward intraday path so some tickers trigger a graded buy. The drop
    depth is a stable function of the ticker so runs are reproducible without any random
    source. This is a self-test harness — it never represents real market data.
    """

    def __init__(self, source: SourceName, previous_close: float = 100.0) -> None:
        self.source = source
        self._previous_close = previous_close

    def _drop_fraction(self, ticker: str) -> float:
        # 0.0 .. ~4.5% depending on the ticker, stable across runs.
        return (sum(ord(ch) for ch in ticker) % 46) / 1000.0

    def get_intraday_bars(
        self, ticker: str, start: datetime, end: datetime, interval_minutes: int
    ) -> List[Bar]:
        drop = self._drop_fraction(ticker)
        decision_close = self._previous_close * (1.0 - drop)
        # Lookback (session open) sits above the decision close -> 60-minute trend down.
        open_price = self._previous_close * (1.0 + 0.002)
        bars: List[Bar] = []
        cursor = start
        steps = max(1, int((end - start).total_seconds() // (interval_minutes * 60)))
        for i in range(steps + 1):
            frac = i / steps
            price = open_price + (decision_close - open_price) * frac
            bars.append(
                Bar(
                    timestamp=cursor,
                    open=price,
                    high=price * 1.001,
                    low=price * 0.999,
                    close=price,
                    volume=1000.0,
                )
            )
            cursor += timedelta(minutes=interval_minutes)
        return bars

    def get_previous_close_bar(self, ticker: str, as_of: datetime) -> Optional[Bar]:
        timestamp = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        p = self._previous_close
        return Bar(timestamp=timestamp, open=p, high=p, low=p, close=p, volume=0.0)


def _resolve_now(now_arg: Optional[str], config: StrategyConfig) -> datetime:
    """Return the decision-time datetime for the run (parsed --now, or today's gate time)."""
    if now_arg:
        parsed = datetime.fromisoformat(now_arg)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=EASTERN)
    today = datetime.now(tz=EASTERN).date()
    return datetime.combine(today, config.decision_time_et, tzinfo=EASTERN)


def _demo_runtime(config: StrategyConfig) -> Tuple[MarketDataProvider, MarketDataProvider, BrokerAdapter]:
    finance = DemoDataProvider(SourceName.FINANCE)
    robinhood = DemoDataProvider(SourceName.ROBINHOOD)
    broker = build_broker(config)  # DryRunBroker for the demo
    return finance, robinhood, broker


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Near-close graded DCA orchestrator (dry-run CLI).")
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR), help="Directory with config/*.json (default: bundled config/).")
    parser.add_argument("--artifacts-dir", default=None, help="Output directory (default: artifacts/<trading-day>).")
    parser.add_argument("--now", default=None, help="Override decision time (ISO 8601, e.g. 2026-07-06T15:45). For testing.")
    parser.add_argument("--run-label", default="daily", help="Run label recorded in artifacts.")
    parser.add_argument("--report", action="store_true", help="Also render the full slippage/coverage reports and Notion sync.")
    parser.add_argument("--demo", action="store_true", help="Use the deterministic in-memory demo provider (no broker/network).")
    parser.add_argument("--log-level", default="INFO", help="Logging level (DEBUG/INFO/WARNING/ERROR).")
    args = parser.parse_args(argv)

    configure_logging(args.log_level)

    config = load_strategy_config(args.config_dir)
    if not config.dry_run:
        logger.error("Refusing to run: config.dry_run is False. The CLI is dry-run only; drive live runs via run_pipeline().")
        return 2

    now = _resolve_now(args.now, config)
    trading_day = now.astimezone(EASTERN).date()
    artifacts_dir = Path(args.artifacts_dir) if args.artifacts_dir else Path("artifacts") / trading_day.isoformat()

    if not args.demo:
        logger.error(
            "No live data providers available to the CLI. Run with --demo for a local dry-run, "
            "or drive a live run from the scheduler via run_dca.run_pipeline() with wired MCP callables."
        )
        return 2

    logger.info("Starting DEMO dry-run for %s -> %s", trading_day, artifacts_dir)
    finance, robinhood, broker = _demo_runtime(config)
    state = StrategyState()
    result = run_pipeline(
        now,
        config,
        state,
        finance,
        robinhood,
        broker,
        artifacts_dir=artifacts_dir,
        run_label=args.run_label,
        generate_full_reports=args.report,
    )
    logger.info("Done. Artifacts:\n%s", json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
