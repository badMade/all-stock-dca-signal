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
      ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` (supplied by the environment, never
      committed); Alpaca stays read-only.
    - Uses the **afternoon 0x -> 5x sizing ladder** (NOT the morning 0x -> 3x
      ladder) and the same $2 base amount.
    - Same active universe (excludes BYDDY) and the same confidence
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
``<cron_tracking_root>/midday_dca/YYYYMMDD_1230ET/`` and never
touches the morning or afternoon artifact trees.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from dca_config import (
    InvalidAccountNumberError,
    MissingConfigEnvError,
    get_config_dir,
    load_per_ticker_sizing,
    load_tickers,
    resolve_broker_account,
)
from dca_paths import rebase_bundled_roots_in, stream_root
from morning_dca import (
    EXEC_MANUAL_REVIEW_CANDIDATE,
    MorningConfig,
    MorningDCAExecutor,
    MorningSignal,
    TopUpOutcome,
    _append_dated_manual_override_cap_seed_roots,
    _coerce_live_execution_enabled,
    _load_approved_dates_fail_closed,
    default_prior_allocation_roots,
    load_max_per_order_usd,
    resolved_count_breakers,
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
DEFAULT_MIDDAY_LIVE_STATE_ROOT = stream_root("midday_dca")

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
        "state_root_stream": "midday_dca",
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

# Midday budget caps. Same shared Normal-tier ceiling every live stream draws
# from (near_close_dca.BUDGET_TIERS[0] = 180.0/900.0, mirrored in
# config/budgets.json and config/midday_dca.json's budget_ledger block) -- not
# an isolated per-stream half-share. Raised from the old 90.0/450.0 halves
# 2026-08-02 (Geoff: "caps aren't pooled the way the target design intends --
# fix it"); see morning_dca.DEFAULT_MORNING_DAILY_CAP_USD's comment and
# MiddayConfig's cap_seed_roots (config/midday_dca.json, added 2026-08-02) for
# how cross-stream spend is folded in. This does NOT add midday's own dynamic
# Elevated/High-volatility/Stress escalation -- only the 3:30 final run
# escalates past Normal, from its fuller same-day view; midday conservatively
# shares the flat Normal floor.
DEFAULT_MIDDAY_DAILY_CAP_USD = 180.0
DEFAULT_MIDDAY_WEEKLY_CAP_USD = 900.0


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
    #
    # default_factory, not a literal tuple: a literal binds
    # DEFAULT_MARKET_OPEN_TASK_ROOT/etc at CLASS-DEFINITION (import) time, so a
    # $DCA_CRON_TRACKING_ROOT set afterward left every MiddayConfig() built
    # without an explicit override -- including load_midday_config's
    # config-missing fallback -- scanning the OLD tree for same-day prior
    # allocations, and _compute_top_up.uses_default_prior_allocation_roots'
    # exact-tuple comparison then read that staleness as "the caller
    # explicitly configured a different root," missing earlier same-day fills
    # instead of merging them (Codex review, PR #219).
    # default_prior_allocation_roots() -- called fresh for each new instance
    # -- returns the identical four roots in the identical order, so this is
    # a no-op under an unmodified environment and a real fix once the
    # variable is set between import and construction.
    prior_allocation_roots: Tuple[str, ...] = field(default_factory=default_prior_allocation_roots)


# Backwards-compatibility alias; the type now lives in morning_dca so both
# streams share the same guard shape and evaluation logic.
MiddayTopUpOutcome = TopUpOutcome


def load_midday_config(config_dir=None, *, allow_unpinned_account: bool = False) -> MiddayConfig:
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
        # morning_run_state_root deliberately stays None here: see
        # morning_dca.load_morning_config's equivalent fallback for why
        # baking a real path in at this point defeats run_dca.py's
        # "did this stream's config actually configure a root" containment
        # check (Codex/self-review, PR #219). The staleness that would
        # otherwise cause is fixed at the call site instead (inherited
        # MorningDCAExecutor.__init__, via state_root_stream in
        # MIDDAY_PROFILES).
        return MiddayConfig(
            approved_test_dates=_load_approved_dates_fail_closed(config_dir_path),
            model_usage_log_root=stream_root("model_usage"),
            # Fail closed to shadow mode rather than inheriting
            # MiddayConfig.live_execution_enabled's True default. Midday IS a
            # live stream (config/midday_dca.json sets live_execution_enabled
            # true), but a run that cannot read its config does not know its
            # caps, ticker universe, target account, or approved dates, and
            # must not place real orders in that state. Note this is NOT
            # covered by _load_approved_dates_fail_closed above: that guards a
            # broken trading_window.json, a different file, so a missing
            # midday_dca.json alone previously reached live placement with
            # hardcoded defaults (Codex review: "Keep disabled streams
            # disabled when config loading fails" -- load_stream_config's
            # equivalent guard, which midday's separate loader never had).
            live_execution_enabled=False,
        )

    approved_date_overrides = {
        "approved_test_dates": _load_approved_dates_fail_closed(config_dir_path),
        # stream_root() at LOAD time, not the import-time constant:
        # $DCA_CRON_TRACKING_ROOT set between import and config load
        # rebased the run-state roots while leaving model-usage evidence
        # (and the attempt-log sibling derived from it) under the old
        # tree, where the call-time credit-usage report never looks
        # (Codex review, PR #219).
        "model_usage_log_root": stream_root("model_usage"),
    }

    try:
        raw = json.loads(cfg_path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        _log.warning("could not parse %s (%s); using MiddayConfig defaults", cfg_path, exc)
        # Fail closed to shadow mode -- see the not-exists branch above for why
        # an unreadable config must never reach live broker placement.
        return MiddayConfig(**approved_date_overrides, live_execution_enabled=False)

    # See load_morning_config: bundled absolute artifact paths follow
    # $DCA_CRON_TRACKING_ROOT instead of overriding it. No-op when unset.
    raw = rebase_bundled_roots_in(raw)

    kwargs: Dict[str, Any] = dict(approved_date_overrides)
    if "trend_min_bars" in raw:
        kwargs["trend_min_bars"] = int(raw["trend_min_bars"])

    # Master placement switch. Deliberately defaults to True (MiddayConfig's
    # inherited default) when the key is absent from an OTHERWISE READABLE
    # config, unlike load_stream_config's shadow-first streams: midday is a
    # currently-functioning live execution, so treating an omitted key as
    # "disabled" here would silently stop a trading stream. Fail-closed applies
    # only to the unreadable/missing-config paths above, where nothing about
    # the stream's configuration can be trusted.
    if "live_execution_enabled" in raw:
        kwargs["live_execution_enabled"] = _coerce_live_execution_enabled(
            raw["live_execution_enabled"], source_label="midday_dca.json"
        )

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
        # See load_morning_config: the account number lives in the environment,
        # not in git. Midday is a live-only stream, so an unresolvable account
        # takes the same route the morning live path takes -- left unset so the
        # executor reaches account_pinning_failure() and writes durable blocked
        # evidence, rather than raising before an executor exists and leaving the
        # scheduled slot with no artifact at all (Codex review, PR #219).
        source = "midday live_task.broker_account"
        try:
            kwargs["target_account_number"] = resolve_broker_account(
                live_task["broker_account"], source=source
            )
        except (MissingConfigEnvError, InvalidAccountNumberError):
            if not allow_unpinned_account:
                raise
            # Explicitly None, NOT merely left out: the dataclass default is the
            # IMPORT-TIME module constant, so omitting the kwarg would silently
            # fall back to whatever account the environment held when this module
            # was first imported -- which for a malformed configured account is a
            # real, usable account number. That would turn a pinning failure into
            # a live run against an unintended account, the exact bypass this
            # whole path exists to close.
            kwargs["target_account_number"] = None
            logging.warning(
                "%s could not be resolved to a usable account; the live run will "
                "fail closed and record account_pinning_failed",
                source,
            )

    artifact_paths = raw.get("artifact_paths", {})
    if "live_run_state_root" in artifact_paths:
        kwargs["morning_run_state_root"] = artifact_paths["live_run_state_root"]

    roots = artifact_paths.get("prior_allocation_roots")
    if isinstance(roots, list):
        # Preserve [] as () so an explicit empty array in JSON fully disables
        # the guard (consistent with load_morning_config).
        kwargs["prior_allocation_roots"] = tuple(str(r) for r in roots)
    # cap_seed_roots (added 2026-08-02, mirrors load_morning_config): without
    # this, config/midday_dca.json's artifact_paths.cap_seed_roots was silently
    # never read, leaving MiddayConfig.cap_seed_roots at its inherited None
    # default and the midday budget ledger fully isolated from every other
    # stream's same-day spend (Geoff: "caps aren't pooled the way the target
    # design intends -- fix it").
    cap_roots = artifact_paths.get("cap_seed_roots")
    if isinstance(cap_roots, list):
        # Preserve an explicit [] as () so it fully disables cap seeding
        # rather than falling through to MorningConfig's default_factory
        # (every sibling stream root), consistent with prior_allocation_roots.
        # Also append any existing dated manual-override root (e.g. config/
        # scheduled_tasks.json's aug18_manual_cap_override) not already
        # present -- config/midday_dca.json's own explicit list independently
        # replaces default_pooled_cap_seed_roots()'s factory output here, the
        # same way load_morning_config's/load_stream_config's configured-list
        # overlays do, so it needs the same append (Codex review, PR #656
        # follow-up: "Append override seeds in the midday loader") or the
        # midday stream's shared cap ledger stays blind to that spend.
        kwargs["cap_seed_roots"] = _append_dated_manual_override_cap_seed_roots(
            tuple(str(r) for r in cap_roots)
        )

    # model_usage_log_root: defaults to stream_root("model_usage") resolved above
    # when this loader runs
    # (consistent with load_morning_config); an explicit
    # artifact_paths.model_usage_log_root overrides it, null/empty disables it.
    if "model_usage_log_root" in artifact_paths:
        configured_log_root = artifact_paths["model_usage_log_root"]
        kwargs["model_usage_log_root"] = str(configured_log_root) if configured_log_root else None

    max_order = load_max_per_order_usd(config_dir_path)
    if max_order is not None:
        kwargs["max_per_order_usd"] = max_order

    # Both COUNT limits from budgets.json: the daily order-ATTEMPT ceiling
    # (circuit_breakers.max_orders_per_day) and the consecutive-placement-
    # failure breaker. This loader is a THIRD, separate one -- load_morning_config
    # and load_stream_config each have their own, and midday goes through
    # neither -- so a knob added only to those two left MiddayConfig at its
    # None default and neither limit ever applied to the functioning midday
    # stream (Codex review, PR #786: "Load the streak breaker in the midday
    # configuration").
    #
    # ...and the SHIPPED fallback for both, where there is no `budgets.json`
    # at all. Resolving the raw loader alone left `MiddayConfig` at its
    # dataclass default of `None` -- which means DISABLED -- on a stream that
    # ships `live_execution_enabled=True`, so a broker-wide outage attempted
    # the whole candidate batch instead of stopping at the shipped threshold.
    # Measured: the bundled midday config with no budgets file loaded as
    # `live_execution_enabled=True, max_consecutive_placement_failures=None`
    # (Codex review, PR #786: "Apply the fallback breaker in the midday
    # loader").
    #
    # Through the SAME resolver the morning-live overrides and the stream
    # loaders use, rather than a fourth copy of the rule: this knob has five
    # live readers and the fallback has now been missed by three of them.
    kwargs.update(resolved_count_breakers(config_dir_path))

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
    kwargs.update(load_per_ticker_sizing(config_dir_path))

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
    # Midday has never written a budget_ledger.json -- config/midday_dca.json has
    # no ledger_dir/ledger_filename block, unlike config/morning_dca.json and the
    # market-open/intraday task manifests. Preserved explicitly now that the base
    # class flag, not a `stream == MORNING_STREAM` check, gates that write.
    writes_budget_ledger: bool = False

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
        # Midday is live-only: tolerate an unresolvable account so the run
        # reaches its fail-closed path and leaves blocked evidence. See
        # load_morning_config's allow_unpinned_account note.
        return load_midday_config(config_dir, allow_unpinned_account=True)

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
