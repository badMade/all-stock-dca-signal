"""Broker-free recommendation computation using the primary's existing gates."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import dca_config
import dca_order_plan
import dca_risk
import dca_runner
from dca_market_sources import ET
from dca_rec_models import (
    SLOT_IDS,
    BrokerState,
    ExpectedPrice,
    ExpectedPriceSet,
    MarketSnapshot,
    Recommendation,
    RecommendationSet,
)
from dca_rec_store import RecommendationStore
from near_close_dca import BUDGET_TIERS
from scripts import backup_planner, backup_stream_specs, generate_buy_candidates, same_day_top_up_gate
from scripts.assemble_order_pages import _order_rows
from scripts.stream_candidates import _calibration_fields, build_document
from scripts.validate_native_placement import native_ref_id


def slots_for(now: datetime, lookahead: timedelta) -> list[str]:
    """Return the active slot, then the next slot opening within ``lookahead``."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(lookahead, timedelta) or lookahead < timedelta(0):
        raise ValueError("lookahead must be a non-negative timedelta")
    instant = now.astimezone(ET)
    day = instant.date()
    active = None
    upcoming = None
    for slot in SLOT_IDS:
        start, end = dca_order_plan.placement_window(slot, day)
        if start <= instant < end:
            active = slot
            continue
        if start > instant:
            upcoming = slot if start <= instant + lookahead else None
            break
    return [slot for slot in (active, upcoming) if slot is not None]


@dataclass(frozen=True)
class EngineResult:
    """One closed result from recommendation evaluation."""

    status: Literal["OK", "NO_SLOT", "ABORT", "END_SILENTLY"]
    slot: str | None
    recommendations: RecommendationSet | None
    expected: ExpectedPriceSet | None
    reasons: tuple[str, ...] = ()


class RecommendationEngine:
    """Compute the primary signal, budget, top-up, spread, and plan chain."""

    def __init__(self, config_dir: str | Path | None = None, cron_root: str | Path | None = None) -> None:
        self.config_dir = Path(config_dir) if config_dir is not None else dca_config.get_config_dir()
        if cron_root is None:
            import dca_paths

            cron_root = dca_paths.cron_tracking_root()
        self.cron_root = Path(cron_root)

    def slot_for(self, now: datetime) -> str | None:
        """Return the current or next unopened slot window for this ET day."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        instant = now.astimezone(ET)
        day = instant.date()
        for slot in dca_runner.SLOTS:
            start, end = dca_order_plan.placement_window(slot, day)
            if start <= instant < end:
                return slot
            if instant < start:
                return slot
        return None

    @staticmethod
    def _broker_orders(pages: list[Any]) -> list[Any]:
        """Parse history once per order id, matching backup_planner's pagination dedupe."""
        orders = []
        seen_order_ids: set[str] = set()
        for page in pages:
            for row in _order_rows(page):
                order_id = str(row.get("id")) if isinstance(row, dict) else ""
                if order_id and order_id in seen_order_ids:
                    continue
                seen_order_ids.add(order_id)
                orders.append(backup_planner._to_broker_order(row))
        return orders

    @staticmethod
    def _candidate_rows(candidates: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        """Keep calibration evidence attached for dca_runner's own Candidate parser."""
        rows = []
        for row in candidates:
            values = (row["ticker"], row["desired_usd"], row["multiplier"], row["drop_pct"])
            if "sizing_base_usd" in row:
                values += (row["sizing_base_usd"], row["sizing_min_drop_pct"], row["sizing_max_drop_pct"])
            rows.append(values)
        return rows

    def _deployed(self, orders: list[Any], now: datetime) -> tuple[float, str]:
        """Use risk artifacts unless any loaded artifact has unusable notional."""
        states = []
        for path in dca_risk.find_run_state_files(self.cron_root):
            try:
                state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=dca_config._reject_duplicate_keys)
            except (OSError, ValueError, UnicodeError):
                # An unreadable artifact is unknown evidence, never a zero deployment.
                return dca_order_plan.trailing_deployed_usd(orders, now=now), "broker_history_fallback"
            if not isinstance(state, dict):
                return dca_order_plan.trailing_deployed_usd(orders, now=now), "broker_history_fallback"
            states.append(state)
        # No run-state files is missing evidence, not proof nothing was deployed (a new or moved
        # cron tree): fall back to broker history like an unreadable file does (PR #1667 review).
        if not states or any(dca_risk.has_unusable_deployed_notional(state) for state in states):
            return dca_order_plan.trailing_deployed_usd(orders, now=now), "broker_history_fallback"
        return dca_risk.seven_day_deployed(dca_risk.deployed_by_day(states), now.astimezone(ET).date()), "run_state_artifacts"

    def compute(self, snapshot: MarketSnapshot, broker: BrokerState, now: datetime, *, slot: str | None = None) -> EngineResult:
        """Run Gate 5 → Gate 6 → Gate 6.5 → Gate 6.7 → plan at ``now``."""
        if now.tzinfo is None or now.utcoffset() is None:
            return EngineResult("ABORT", None, None, None, ("clock_not_timezone_aware",))
        instant = now.astimezone(ET)
        day = instant.date()
        if slot is not None and slot not in SLOT_IDS:
            return EngineResult("ABORT", None, None, None, ("invalid_slot",))
        slot = slot if slot is not None else self.slot_for(instant)
        if slot is None:
            return EngineResult("NO_SLOT", None, None, None, ("no_remaining_slot_window",))
        try:
            envelope = broker.orders_envelope
            pages = envelope["pages"]
            orders = self._broker_orders(pages)
            deployed, deployed_source = self._deployed(orders, instant)
            account_config = dca_config.load_strategy_config(
                self.config_dir, now=instant, deployed_last_7d_usd=deployed
            )
            payload = snapshot.payload
            if slot == "1530":
                config = account_config
                generated = generate_buy_candidates.generate_candidates(
                    slot="1530ET", now=instant, config=config, market_data_by_source=payload["sources"],
                    source_errors=payload.get("source_errors", []), ticker_errors=payload.get("ticker_errors"),
                    already_covered_tickers=(), decision_time_et=instant.time(),
                )
                candidates = []
                for row in generated:
                    if row.get("action") != "BUY":
                        continue
                    planner_row = [row["ticker"], row["suggested_amount_usd"], row["buy_multiplier"], row["multiplier_drop_pct"]]
                    calibration = _calibration_fields(
                        row["ticker"], row["buy_multiplier"], row["selected_base_amount_usd"]
                    )
                    if calibration:
                        planner_row += [calibration["sizing_base_usd"], calibration["sizing_min_drop_pct"], calibration["sizing_max_drop_pct"]]
                    if generate_buy_candidates._planner_incompatible(planner_row):
                        continue
                    prior = backup_planner._prior_bar(payload["sources"], row["ticker"], str(row.get("target_price_source") or ""))
                    candidate = {"ticker": row["ticker"], "desired_usd": row["suggested_amount_usd"],
                                       "multiplier": row["buy_multiplier"], "drop_pct": row["multiplier_drop_pct"],
                                       "current_price": row["target_price"], "prior_close": prior.get("close") if prior else None,
                                       "prior_low": prior.get("low") if prior and isinstance(prior.get("low"), (int, float))
                                       and prior["low"] > 0 else None}
                    if calibration:
                        candidate.update({
                            "sizing_base_usd": calibration["sizing_base_usd"],
                            "sizing_min_drop_pct": calibration["sizing_min_drop_pct"],
                            "sizing_max_drop_pct": calibration["sizing_max_drop_pct"],
                        })
                    candidates.append(candidate)
            else:
                spec = backup_stream_specs.stream_spec(slot)
                # Stream gates need the slot-specific MorningConfig; build_document loads the
                # account StrategyConfig itself when applying this slot's deployment guard.
                config = spec.config_loader(self.config_dir, allow_unpinned_account=True)
                source_name = spec.source.value
                document = build_document(
                    spec, trading_day=day, config=config,
                    market_data=payload["sources"].get(source_name, {}),
                    source_errors=payload.get("source_errors", []),
                    ticker_errors=(payload.get("ticker_errors") or {}).get(source_name, []),
                    already_covered_tickers=(), config_dir=self.config_dir,
                    deployed_last_7d_usd=deployed if spec.requires_deployed_last_7d_usd else None,
                    decision_time_et=instant.time(),
                )
                withheld = set(document.get("planner_incompatible_tickers") or ())
                candidates = [{"ticker": row["ticker"], "desired_usd": row["desired_usd"],
                               "multiplier": row["multiplier"], "drop_pct": row["drop_pct"],
                               "current_price": row["current_price"], "prior_close": row["prior_close"],
                               "prior_low": row.get("prior_low"), **{
                                   key: row[key] for key in (
                                       "sizing_base_usd", "sizing_min_drop_pct", "sizing_max_drop_pct"
                                   ) if key in row
                               }}
                              for row in document["cands"] if row["ticker"] not in withheld]
            if slot == "1530":
                # Only 15:30 escalates, using assess()'s count-only tier selection.
                # Reserve zero caps Stress to Normal because the artifact reserve is not read here.
                # V=max(T, window start) assesses a precomputed plan at its serving window; placement reruns
                # the real clock and sole-executor gates immediately before submission.
                assess_now = max(instant, dca_order_plan.placement_window(slot, day)[0])
                verdict = dca_runner.assess(
                    slot, orders, self._candidate_rows(candidates),
                    now=assess_now, buying_power_usd=broker.buying_power_usd, weekly_rollover_reserve_usd=0.0,
                )
                status = verdict.get("verdict")
                if status in {"ABORT", "END_SILENTLY"}:
                    return EngineResult(status, slot, None, None, (str(verdict.get("reason", status)),))
                if status != "PROCEED":
                    return EngineResult("ABORT", slot, None, None, ("unknown_assess_verdict",))
                planned = verdict.get("plan", {}).get("to_place", [])
                planned_rows = [(row["ticker"], row["amount_usd"]) for row in planned]
                selected_budget = verdict.get("budget", {})
                budget_record = {
                    "budget_tier": selected_budget.get("tier"),
                    "daily_cap_usd": selected_budget.get("daily_cap_usd"),
                    "weekly_cap_usd": selected_budget.get("weekly_cap_usd"),
                }
            else:
                # Rev 4 pins all six stream prompts to Normal; assess() would escalate dynamically.
                if broker.buying_power_usd is None:
                    return EngineResult("ABORT", slot, None, None, ("buying_power_unavailable",))
                converted = dca_runner._parse_candidate_rows(
                    self._candidate_rows(candidates)
                )
                budget = dca_runner.budget_snapshot(orders, day)
                normal_tier = next(tier for tier in BUDGET_TIERS if tier.name.lower() == "normal")
                budget_record = {
                    "budget_tier": normal_tier.name,
                    "daily_cap_usd": normal_tier.daily_cap_usd,
                    "weekly_cap_usd": normal_tier.weekly_cap_usd,
                }
                plan = dca_runner.plan_orders(
                    converted, budget, normal_tier, buying_power_usd=broker.buying_power_usd,
                )
                planned_rows = [(order.ticker, order.amount_usd) for order in plan.to_place]
            by_ticker = {row["ticker"]: row for row in candidates}
            planned = [{"ticker": ticker, "amount_usd": amount} for ticker, amount in planned_rows]
            topup_rows = [dict(by_ticker[row["ticker"]], desired_usd=row["amount_usd"]) for row in planned]
            # Bind each repeat's logged decision price to its broker order, as the live prompts do,
            # so Gate 6.5 can resize a cross-slot top-up instead of returning
            # UNLOGGED_REFERENCE_PRICE / $0 (PR #1667 review). The source is the program's own records
            # of plan-first buys. Legacy slots' artifacts are not read, so their repeats keep the
            # gate's conservative $0. The gate binds only when the order id is the broker's latest.
            references = RecommendationStore(self.cron_root / "recommendations").logged_references(day)
            for row in topup_rows:
                if row["ticker"] in references:
                    order_id, price = references[row["ticker"]]
                    row["prior_logged_reference_price"] = price
                    row["prior_logged_reference_order_id"] = order_id
            topup = same_day_top_up_gate.evaluate_slot(
                pages=pages, candidates=topup_rows, account_number=broker.account_number,
                trading_day=day.isoformat(), config_dir=self.config_dir,
            )
            if topup.get("verdict") == "ABORT":
                return EngineResult("ABORT", slot, None, None, ("top_up_abort",))
            rows = []
            for row in topup.get("decisions", []):
                buy_usd = row.get("buy_usd")
                source = by_ticker.get(row.get("ticker"))
                if not source or not isinstance(buy_usd, (int, float)) or isinstance(buy_usd, bool) or buy_usd <= 0:
                    continue
                sides = (snapshot.quote_sides or {}).get(source["ticker"])
                reason = dca_order_plan._spread_reason(sides, config.max_bid_ask_spread_pct)
                if reason:
                    continue
                rows.append({**source, "buy_usd": buy_usd})
            ladder = dca_order_plan.current_ladder_version(self.config_dir)
            planned_result = dca_order_plan.plan_from_survivors(
                rows, config=config, trading_day=day, slot=slot, generated_at_et=instant, ladder_version=ladder,
            )
            if planned_result.errors or planned_result.plan is None:
                return EngineResult("ABORT", slot, None, None, tuple(error.code for error in planned_result.errors))
            orders_out = planned_result.plan["orders"]
            rec = RecommendationSet(day, instant, slot, snapshot.source, ladder,
                                   dca_order_plan.universe_hash(config.tickers),
                                   tuple(Recommendation(row["symbol"], row["notional_usd"], native_ref_id(slot, day.isoformat(), row["symbol"])) for row in orders_out))
            expected = ExpectedPriceSet(day, instant, slot, deployed_source,
                                        tuple(ExpectedPrice(row["symbol"], row["decision_price"], row["prior_close"],
                                                            row["prior_low"], row["multiplier"], row["drop_pct"],
                                                            row["notional_usd"]) for row in orders_out), budget_record)
            return EngineResult("OK", slot, rec, expected, ("rollover_reserve_not_read",))
        except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
            return EngineResult("ABORT", slot, None, None, (type(exc).__name__,))
