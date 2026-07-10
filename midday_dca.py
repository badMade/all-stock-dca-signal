"""Midday graded-DCA executor (12:30 PM ET live Robinhood execution stream).

This module adds a *midday* live-execution stream on top of the morning
executor pipeline (:mod:`morning_dca`). It reuses the morning executor's
evaluate/build/write machinery via subclassing, overriding only the
stream-identity attributes and adding a same-day top-up guard, so it never
duplicates the shared evaluation code.

Key properties (see ``config/midday_dca.json`` and
``schemas/midday_run_classification.schema.json``):

    - **Live Robinhood-only execution** (cron ``a6239dae``, 12:30 PM ET
      weekdays, ``run_type='midday_dca_live'``, ``trades=true``) on account
      ``562057042``; Alpaca stays read-only.
    - Uses the **afternoon 0x -> 5x sizing ladder** (NOT the morning 0x -> 3x
      ladder) and the same $3 base amount.
    - Same active universe (excludes BYDDY/VCX/TRLV/RVI) and the same confidence
      rule: a candidate whose price/drop qualifies but whose intraday trend
      cannot be validated is flagged ``manual_review_candidate`` and NOT
      auto-bought.
    - **Independent** of the afternoon 3:20 live task: ``stream='midday'`` and
      ``valid_afternoon_evidence=false`` so afternoon reporting never mistakes it
      for afternoon scheduled evidence. Midday fills may be read by the afternoon
      run only as prior-allocation context.
    - **Same-day top-up guard**: morning/manual prior same-day buys are prior
      allocation context only. Midday buys only the incremental dollar difference
      when the midday decision price is strictly below the prior reference price;
      otherwise $0 (mirrors the afternoon top-up rule).

Writes ONLY under its own run-state root
``/home/user/workspace/cron_tracking/midday_dca/YYYYMMDD_1230ET/`` and never
touches the morning or afternoon artifact trees.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

from morning_dca import (
    DEFAULT_MORNING_LIVE_STATE_ROOT,
    EXEC_MANUAL_REVIEW_CANDIDATE,
    MorningConfig,
    MorningDCAExecutor,
    MorningSignal,
)
from near_close_dca import (
    ACTIVE_TICKERS,
    EASTERN,
    EXCLUDED_TICKERS,
    TOP_UP_INCREMENTAL,
    TOP_UP_MISSING_PRIOR_PRICE,
    TOP_UP_NO_INCREMENTAL,
    TOP_UP_NO_PRIOR_RUN,
    TOP_UP_PRICE_NOT_BELOW,
    Action,
    PriorSameDayAllocation,
    StrategyConfig,
    load_prior_same_day_allocations,
)

_log = logging.getLogger(__name__)

# --- Midday stream identity / classification constants ----------------------
MIDDAY_STREAM = "midday"

MIDDAY_LIVE_MODE = "live"
MIDDAY_LIVE_CRON_ID = "a6239dae"
MIDDAY_LIVE_RUN_TYPE = "midday_dca_live"
MIDDAY_LIVE_SCHEDULED_FOR_ET = "12:30"
MIDDAY_LIVE_WINDOW_START_ET = "12:25"
MIDDAY_LIVE_WINDOW_END_ET = "12:35"
MIDDAY_LIVE_SCHEMA_VERSION = "midday-live-v1"

# Artifact root: a sibling of, and never nested under, the morning or afternoon
# roots.
DEFAULT_MIDDAY_LIVE_STATE_ROOT = "/home/user/workspace/cron_tracking/midday_dca"

# The afternoon live task root (cron ce16ba6b). Included in midday's
# prior_allocation_roots so that manual fills recorded via the afternoon path
# before 12:30 PM are seen by the midday top-up guard.
DEFAULT_AFTERNOON_TASK_ROOT = "/home/user/workspace/cron_tracking/ce16ba6b"

# Per-mode identity/profile. The midday stream has a single live mode.
MIDDAY_PROFILES: Dict[str, Dict[str, Any]] = {
    MIDDAY_LIVE_MODE: {
        "mode": MIDDAY_LIVE_MODE,
        "artifact_mode": "live",
        "trades": True,
        "cron_id": MIDDAY_LIVE_CRON_ID,
        "run_type": MIDDAY_LIVE_RUN_TYPE,
        "scheduled_for_et": MIDDAY_LIVE_SCHEDULED_FOR_ET,
        "window_start_et": MIDDAY_LIVE_WINDOW_START_ET,
        "window_end_et": MIDDAY_LIVE_WINDOW_END_ET,
        "state_schema_version": MIDDAY_LIVE_SCHEMA_VERSION,
        "state_root": DEFAULT_MIDDAY_LIVE_STATE_ROOT,
        "folder_suffix": "1230ET",
        "scheduled_run_id_prefix": "midday_live",
        "manual_run_id_prefix": "manual_midday_live",
        "decision_time_et": time(12, 30),
        "validate_trend": True,
    },
}

# Midday sizing ladder: the SAME 0x -> 5x ladder the afternoon stream uses
# (StrategyConfig defaults), deliberately distinct from the morning 0x -> 3x
# ladder. Thresholds are fractions of the previous close.
MIDDAY_SIZING_TIERS: Tuple[Tuple[float, float], ...] = StrategyConfig().sizing_tiers
MIDDAY_MINIMUM_DROP = StrategyConfig().minimum_drop_vs_previous_close

# Midday budget caps. The midday stream owns these; they mirror the morning
# per-stream caps so midday activity is budgeted independently of both morning
# and the afternoon BUDGET_TIERS ledger.
DEFAULT_MIDDAY_DAILY_CAP_USD = 90.0
DEFAULT_MIDDAY_WEEKLY_CAP_USD = 450.0


@dataclass(frozen=True)
class MiddayConfig(MorningConfig):
    """Configuration for the midday live-execution stream.

    Subclasses :class:`MorningConfig` so it reuses every field the shared
    executor reads, overriding the ladder defaults to the afternoon 0x -> 5x
    ladder and adding ``prior_allocation_roots`` (the morning/manual roots the
    midday top-up guard scans for earlier same-day buys).
    """

    sizing_tiers: Tuple[Tuple[float, float], ...] = MIDDAY_SIZING_TIERS
    minimum_drop_vs_previous_close: float = MIDDAY_MINIMUM_DROP
    morning_daily_cap_usd: float = DEFAULT_MIDDAY_DAILY_CAP_USD
    morning_weekly_cap_usd: float = DEFAULT_MIDDAY_WEEKLY_CAP_USD
    # Roots scanned for same-day prior allocations. The morning live root catches
    # both scheduled morning-live runs and manual recovery runs
    # (load_prior_same_day_allocations rglobs recursively). The afternoon task
    # root ensures that manual fills recorded through the afternoon path before
    # 12:30 PM are also visible to the midday top-up guard.
    prior_allocation_roots: Tuple[str, ...] = (
        DEFAULT_MORNING_LIVE_STATE_ROOT,
        DEFAULT_AFTERNOON_TASK_ROOT,
    )


@dataclass(frozen=True)
class MiddayTopUpOutcome:
    """Per-ticker result of the midday same-day prior-run offset/top-up guard."""

    ticker: str
    desired_amount: float
    decision_price: Optional[float]
    prior_same_day_amount: float
    prior_same_day_reference_price: Optional[float]
    prior_same_day_run_id: Optional[str]
    prior_same_day_source: Optional[str]
    incremental_order_amount: float
    top_up_guard_result: str
    top_up_skipped: bool
    top_up_manual_review: bool = False


def load_midday_config(config_dir=None) -> MiddayConfig:
    """Read ``config/midday_dca.json`` and return a :class:`MiddayConfig`.

    Mirrors :func:`morning_dca.load_morning_config`'s graceful-fallback pattern:
    when the file is absent or unreadable it logs a warning and returns
    ``MiddayConfig()`` defaults.
    """
    if config_dir is None:
        import os as _os
        import sys as _sys
        env_dir = _os.environ.get("DCA_CONFIG_DIR")
        if env_dir:
            config_dir_path = Path(env_dir)
        else:
            _here = Path(__file__).resolve().parent
            _installed = Path(_sys.prefix) / "share" / "all-stock-dca" / "config"
            for candidate in (_here / "config", _installed, Path.cwd() / "config"):
                if candidate.is_dir():
                    config_dir_path = candidate
                    break
            else:
                config_dir_path = _here / "config"
    else:
        config_dir_path = Path(config_dir)

    cfg_path = config_dir_path / "midday_dca.json"
    if not cfg_path.exists():
        _log.warning("midday_dca.json not found at %s; using MiddayConfig defaults", cfg_path)
        return MiddayConfig()

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("could not parse %s (%s); using MiddayConfig defaults", cfg_path, exc)
        return MiddayConfig()

    kwargs: Dict[str, Any] = {}
    if "trend_min_bars" in raw:
        kwargs["trend_min_bars"] = int(raw["trend_min_bars"])

    sizing = raw.get("sizing_ladder", {})
    if "base_amount_usd" in sizing:
        kwargs["base_buy_amount_usd"] = float(sizing["base_amount_usd"])
    if "no_buy_below_pct" in sizing:
        kwargs["minimum_drop_vs_previous_close"] = float(sizing["no_buy_below_pct"]) / 100.0
    if "tiers" in sizing:
        kwargs["sizing_tiers"] = tuple(
            (float(t["min_drop_pct"]) / 100.0, float(t["multiplier"]))
            for t in sizing["tiers"]
        )

    budget = raw.get("budget_ledger", {})
    if "midday_daily_cap_usd" in budget:
        kwargs["morning_daily_cap_usd"] = float(budget["midday_daily_cap_usd"])
    if "midday_weekly_cap_usd" in budget:
        kwargs["morning_weekly_cap_usd"] = float(budget["midday_weekly_cap_usd"])

    live_task = raw.get("live_task", {})
    if "broker_account" in live_task:
        kwargs["target_account_number"] = str(live_task["broker_account"])

    artifact_paths = raw.get("artifact_paths", {})
    if "live_run_state_root" in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths["live_run_state_root"]

    roots = artifact_paths.get("prior_allocation_roots")
    if isinstance(roots, list) and roots:
        kwargs["prior_allocation_roots"] = tuple(str(r) for r in roots)

    return MiddayConfig(**kwargs)


class MiddayDCAExecutor(MorningDCAExecutor):
    """Midday live graded-DCA executor (12:30 PM ET Robinhood execution).

    Subclasses :class:`MorningDCAExecutor` to reuse the entire
    evaluate/build/write pipeline under the midday stream identity. Overrides the
    stream-identity class attributes and the top-up hooks so it applies the
    same-day prior-allocation offset against morning/manual earlier runs.
    """

    stream = MIDDAY_STREAM
    PROFILES: Dict[str, Dict[str, Any]] = MIDDAY_PROFILES
    ledger_namespace: str = MIDDAY_STREAM
    ladder_name: str = "midday_0x_to_5x"
    signals_csv_name: str = "midday_signals.csv"
    latest_pointer_name: str = "latest_midday_run.json"
    budget_daily_key: str = "midday_daily_cap_usd"
    budget_weekly_key: str = "midday_weekly_cap_usd"
    budget_label: str = "midday"
    manual_run_subdir: str = "manual_reruns"

    def __init__(
        self,
        config: Optional[MiddayConfig] = None,
        ledger=None,
        *,
        mode: str = MIDDAY_LIVE_MODE,
        config_dir: Optional[str] = None,
    ) -> None:
        super().__init__(config, ledger, mode=mode, config_dir=config_dir)

    def _load_default_config(self, config_dir: Optional[str], mode: str) -> MiddayConfig:
        return load_midday_config(config_dir)

    # -- Top-up guard (morning/manual prior-allocation offset) ---------------
    def _load_prior_allocations(self, now: datetime, trading_day: date) -> Dict[str, PriorSameDayAllocation]:
        """Aggregate morning/manual prior same-day allocations across all roots.

        When a ticker appears in more than one root the dollar amounts are summed
        and the lowest available reference price wins, so midday must beat the
        best earlier entry price before it tops up (mirrors
        :func:`load_prior_same_day_allocations`'s within-root merge).

        The scan is capped at the actual run time so replayed/backfilled midday
        runs never load fills that were created after the 12:30 decision window.
        """
        boundary_et = now.astimezone(EASTERN).time()
        merged: Dict[str, PriorSameDayAllocation] = {}
        roots = getattr(self.config, "prior_allocation_roots", (DEFAULT_MORNING_LIVE_STATE_ROOT,))
        for root in roots:
            allocations = load_prior_same_day_allocations(
                root, trading_day, boundary_et=boundary_et
            )
            for ticker, alloc in allocations.items():
                existing = merged.get(ticker)
                if existing is None:
                    merged[ticker] = alloc
                    continue
                if existing.reference_price is None or alloc.reference_price is None:
                    ref = None
                else:
                    ref = min(existing.reference_price, alloc.reference_price)
                merged[ticker] = PriorSameDayAllocation(
                    ticker=ticker,
                    amount_usd=existing.amount_usd + alloc.amount_usd,
                    reference_price=ref,
                    run_id=alloc.run_id or existing.run_id,
                    source=alloc.source or existing.source,
                )
        return merged

    def _evaluate_top_up(
        self, signal: MorningSignal, prior: Optional[PriorSameDayAllocation]
    ) -> MiddayTopUpOutcome:
        """Apply the same-day top-up rule to one midday buy candidate.

          * No earlier same-day allocation -> normal allocation (full desired).
          * Earlier allocation and midday price strictly below the prior
            reference -> buy only the incremental difference (desired - prior);
            $0 if <= 0.
          * Earlier allocation but midday price not below the reference -> $0.
          * Prior amount known but no comparable price -> conservative $0 with
            manual review flagged.
        """
        desired = round(signal.hypothetical_amount_usd, 2)
        price = signal.current_price

        def _outcome(
            incremental: float, result: str, *, skipped: bool, manual_review: bool = False
        ) -> MiddayTopUpOutcome:
            return MiddayTopUpOutcome(
                ticker=signal.ticker,
                desired_amount=desired,
                decision_price=price,
                prior_same_day_amount=round(prior.amount_usd, 2) if prior else 0.0,
                prior_same_day_reference_price=prior.reference_price if prior else None,
                prior_same_day_run_id=prior.run_id if prior else None,
                prior_same_day_source=prior.source if prior else None,
                incremental_order_amount=round(max(0.0, incremental), 2),
                top_up_guard_result=result,
                top_up_skipped=skipped,
                top_up_manual_review=manual_review,
            )

        if prior is None or prior.amount_usd <= 0:
            return _outcome(desired, TOP_UP_NO_PRIOR_RUN, skipped=desired <= 0)
        if prior.reference_price is None or price is None:
            return _outcome(0.0, TOP_UP_MISSING_PRIOR_PRICE, skipped=True, manual_review=True)
        if price >= prior.reference_price:
            return _outcome(0.0, TOP_UP_PRICE_NOT_BELOW, skipped=True)
        incremental = desired - round(prior.amount_usd, 2)
        if incremental <= 0:
            return _outcome(0.0, TOP_UP_NO_INCREMENTAL, skipped=True)
        return _outcome(incremental, TOP_UP_INCREMENTAL, skipped=False)

    # -- Reuse hooks ---------------------------------------------------------
    def _compute_top_up(
        self, now: datetime, signals: Sequence[MorningSignal]
    ) -> Dict[str, MiddayTopUpOutcome]:
        trading_day = now.astimezone(EASTERN).date()
        prior_allocations = self._load_prior_allocations(now, trading_day)
        outcomes: Dict[str, MiddayTopUpOutcome] = {}
        for signal in signals:
            if signal.action != Action.BUY or signal.multiplier <= 0:
                continue
            outcomes[signal.ticker] = self._evaluate_top_up(
                signal, prior_allocations.get(signal.ticker)
            )
        return outcomes

    def _effective_buy_amount(
        self, signal: MorningSignal, top_up_outcomes: Optional[Dict[str, Any]]
    ) -> float:
        if top_up_outcomes:
            outcome = top_up_outcomes.get(signal.ticker)
            if outcome is not None:
                return outcome.incremental_order_amount
        return signal.hypothetical_amount_usd

    def _augment_buy_candidate_row(
        self, row: Dict[str, Any], signal: MorningSignal, top_up_outcomes: Optional[Dict[str, Any]]
    ) -> None:
        outcome = top_up_outcomes.get(signal.ticker) if top_up_outcomes else None
        if outcome is None:
            return
        row["prior_same_day_amount"] = outcome.prior_same_day_amount
        row["prior_same_day_reference_price"] = outcome.prior_same_day_reference_price
        row["prior_same_day_run_id"] = outcome.prior_same_day_run_id
        row["prior_same_day_source"] = outcome.prior_same_day_source
        row["midday_desired_amount"] = outcome.desired_amount
        row["midday_decision_price"] = outcome.decision_price
        row["incremental_order_amount"] = outcome.incremental_order_amount
        row["top_up_guard_result"] = outcome.top_up_guard_result
        row["top_up_skipped"] = outcome.top_up_skipped
        row["top_up_manual_review"] = outcome.top_up_manual_review
        if outcome.top_up_manual_review:
            row["execution_status"] = EXEC_MANUAL_REVIEW_CANDIDATE

    def _augment_artifacts(
        self, artifacts: Dict[str, Any], top_up_outcomes: Optional[Dict[str, Any]]
    ) -> None:
        outcomes = list(top_up_outcomes.values()) if top_up_outcomes else []
        top_up_manual_reviews = sorted(o.ticker for o in outcomes if o.top_up_manual_review)
        artifacts["top_up_manual_review_tickers"] = top_up_manual_reviews
        artifacts["top_up_offset_tickers"] = sorted(
            o.ticker for o in outcomes if o.top_up_skipped and not o.top_up_manual_review
        )
        if top_up_manual_reviews:
            artifacts["counts"]["manual_review_candidates"] = (
                artifacts["counts"].get("manual_review_candidates", 0)
                + len(top_up_manual_reviews)
            )


__all__ = [
    "MIDDAY_STREAM",
    "MIDDAY_LIVE_CRON_ID",
    "MIDDAY_LIVE_MODE",
    "MIDDAY_LIVE_RUN_TYPE",
    "MIDDAY_LIVE_SCHEMA_VERSION",
    "MIDDAY_PROFILES",
    "MIDDAY_SIZING_TIERS",
    "DEFAULT_MIDDAY_LIVE_STATE_ROOT",
    "MiddayConfig",
    "MiddayTopUpOutcome",
    "MiddayDCAExecutor",
    "load_midday_config",
    "ACTIVE_TICKERS",
    "EXCLUDED_TICKERS",
]
