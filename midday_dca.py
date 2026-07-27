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
      ladder) and the same $2 base amount.
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
from datetime import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from dca_config import get_config_dir, load_tickers
from morning_dca import (
    DEFAULT_AFTERNOON_TASK_ROOT,
    DEFAULT_INTRADAY_TASK_ROOT,
    DEFAULT_MARKET_OPEN_TASK_ROOT,
    DEFAULT_MODEL_USAGE_LOG_ROOT,
    DEFAULT_MORNING_LIVE_STATE_ROOT,
    EXEC_MANUAL_REVIEW_CANDIDATE,
    MorningConfig,
    MorningDCAExecutor,
    MorningSignal,
    TopUpOutcome,
    _load_approved_dates_fail_closed,
    load_max_per_order_usd,
)
from near_close_dca import (
    ACTIVE_TICKERS,
    EXCLUDED_TICKERS,
    StrategyConfig,
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
    # 12:30 PM are also visible to the midday top-up guard. The market-open
    # root catches the 9:35 AM fill (Codex review: "Feed market-open fills
    # into later top-up guards") and the intraday root catches the 11:30 AM
    # slot's fill (Codex review: "Include the 11:30 intraday run in midday
    # context") -- both precede the 12:30 midday decision.
    prior_allocation_roots: Tuple[str, ...] = (
        DEFAULT_MARKET_OPEN_TASK_ROOT,
        DEFAULT_MORNING_LIVE_STATE_ROOT,
        DEFAULT_INTRADAY_TASK_ROOT,
        DEFAULT_AFTERNOON_TASK_ROOT,
    )


# Backwards-compatibility alias; the type now lives in morning_dca so both
# streams share the same guard shape and evaluation logic.
MiddayTopUpOutcome = TopUpOutcome


def load_midday_config(config_dir=None) -> MiddayConfig:
    """Read ``config/midday_dca.json`` and return a :class:`MiddayConfig`.

    Mirrors :func:`morning_dca.load_morning_config`'s graceful-fallback pattern:
    when the file is absent or unreadable it logs a warning and returns
    ``MiddayConfig()`` defaults.
    """
    if config_dir is None:
        config_dir_path = get_config_dir()
    else:
        config_dir_path = Path(config_dir)

    cfg_path = config_dir_path / "midday_dca.json"
    if not cfg_path.exists():
        _log.warning("midday_dca.json not found at %s; using MiddayConfig defaults", cfg_path)
        return MiddayConfig(
            approved_test_dates=_load_approved_dates_fail_closed(config_dir_path),
            model_usage_log_root=DEFAULT_MODEL_USAGE_LOG_ROOT,
        )

    approved_date_overrides = {
        "approved_test_dates": _load_approved_dates_fail_closed(config_dir_path),
        "model_usage_log_root": DEFAULT_MODEL_USAGE_LOG_ROOT,
    }

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _log.warning("could not parse %s (%s); using MiddayConfig defaults", cfg_path, exc)
        return MiddayConfig(**approved_date_overrides)

    kwargs: Dict[str, Any] = dict(approved_date_overrides)
    if "trend_min_bars" in raw:
        kwargs["trend_min_bars"] = int(raw["trend_min_bars"])

    sizing = raw.get("sizing_ladder", {})
    if "base_amount_usd" in sizing:
        kwargs["base_buy_amount_usd"] = float(sizing["base_amount_usd"])
    # Mirrors load_morning_config: without this, an operator-configured
    # base_amount_below_close_usd silently has no effect on the midday stream and
    # below-close-only midday orders fall back to MiddayConfig's hardcoded $1
    # default regardless of what the config says (Codex review: "Route the
    # reduced base through the midday configuration").
    if "base_amount_below_close_usd" in sizing:
        kwargs["base_buy_amount_below_close_usd"] = float(sizing["base_amount_below_close_usd"])
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
    if isinstance(roots, list):
        # Preserve [] as () so an explicit empty array in JSON fully disables
        # the guard (consistent with load_morning_config).
        kwargs["prior_allocation_roots"] = tuple(str(r) for r in roots)

    # model_usage_log_root: defaults to DEFAULT_MODEL_USAGE_LOG_ROOT above
    # (consistent with load_morning_config); an explicit
    # artifact_paths.model_usage_log_root overrides it, null/empty disables it.
    if "model_usage_log_root" in artifact_paths:
        configured_log_root = artifact_paths["model_usage_log_root"]
        kwargs["model_usage_log_root"] = str(configured_log_root) if configured_log_root else None

    max_order = load_max_per_order_usd(config_dir_path)
    if max_order is not None:
        kwargs["max_per_order_usd"] = max_order

    # Overlay trading_window.json so scheduled live runs respect the approved date
    # window (empty frozenset = gate disabled; non-empty = only approved dates allowed).
    approved_dates = _load_approved_dates_fail_closed(config_dir_path)
    if approved_dates:
        kwargs["approved_test_dates"] = approved_dates

    # Overlay tickers.json so config-only emergency exclusions take effect without
    # a code deploy (mirrors the afternoon load_strategy_config ticker overlay).
    ticker_cfg = load_tickers(config_dir_path)
    if "tickers" in ticker_cfg:
        kwargs["tickers"] = ticker_cfg["tickers"]
    if "excluded_tickers" in ticker_cfg:
        kwargs["excluded_tickers"] = ticker_cfg["excluded_tickers"]

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
    # manual_reruns/ here holds post-12:30 backfill artifacts documented (see
    # near_close_dca.py) as prior-allocation context for the afternoon top-up
    # guard ONLY -- never evidence for midday's own scheduled cap ledger.
    # Folding them into _seed_ledger_from_prior_runs would let an unrelated
    # manual override eat into the cap a scheduled midday retry checks
    # against, wrongly blocking buys the original 12:30 run would have allowed.
    scan_manual_reruns_for_ledger: bool = False

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

    # -- Reuse hooks (stream-specific field names) ---------------------------
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
        # The guard's effective base (the preserved first-allocation base when
        # reused) actually produced desired/incremental amounts above -- it must
        # replace to_row()'s freshly recomputed selected_base_amount_usd so a
        # later reader sees the base that was ACTUALLY used, mirroring
        # MorningDCAExecutor._augment_buy_candidate_row (Codex review:
        # "Persist the effective base in midday candidate rows").
        row["selected_base_amount_usd"] = outcome.effective_base_amount_usd
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
        # Only count tickers not already in manual_review_tickers to avoid
        # double-counting trend-unvalidated tickers that also hit the missing-price path.
        existing_manual_review = set(artifacts.get("manual_review_tickers", []))
        new_top_up_manual = [t for t in top_up_manual_reviews if t not in existing_manual_review]
        if new_top_up_manual:
            artifacts["counts"]["manual_review_candidates"] = (
                artifacts["counts"].get("manual_review_candidates", 0)
                + len(new_top_up_manual)
            )
            artifacts["manual_review_tickers"] = (
                artifacts.get("manual_review_tickers", []) + new_top_up_manual
            )
            # Pull the full enriched row from buy_candidates (which already has
            # prior_same_day_*, incremental_order_amount, top_up_guard_result, etc.
            # attached by _attach_top_up_fields) so consumers of
            # manual_review_candidates see the same shape as trend-unvalidated rows.
            new_set = set(new_top_up_manual)
            extra_rows = [
                row for row in artifacts.get("buy_candidates", [])
                if row.get("ticker") in new_set
            ]
            if extra_rows:
                manual_rows = list(artifacts.get("manual_review_candidates", []))
                by_ticker = {r.get("ticker"): r for r in manual_rows if r.get("ticker")}
                for row in extra_rows:
                    ticker = row.get("ticker")
                    if ticker in by_ticker:
                        by_ticker[ticker].update(row)
                    else:
                        new_row = dict(row)
                        manual_rows.append(new_row)
                        by_ticker[ticker] = new_row
                artifacts["manual_review_candidates"] = manual_rows


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
