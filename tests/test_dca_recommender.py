"""Recommendation parity for the primary's real signal and sizing chain."""

import json
import sys
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dca_rec_models import BrokerState, MarketSnapshot
from dca_recommender import RecommendationEngine, slots_for

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "tests"))
from market_data_fixtures import DAY as FIXTURE_DAY  # noqa: E402
from market_data_fixtures import SLOT_TIMES, market_data

import dca_config  # noqa: E402
import dca_order_plan  # noqa: E402
import dca_runner  # noqa: E402
from near_close_dca import BUDGET_TIERS  # noqa: E402
from scripts import backup_planner, backup_stream_specs, generate_buy_candidates, same_day_top_up_gate  # noqa: E402
from scripts.assemble_order_pages import _order_rows  # noqa: E402
from scripts.run_classification import build_scheduled_classification  # noqa: E402
from scripts.stream_candidates import _calibration_fields, build_document  # noqa: E402

ET = ZoneInfo("America/New_York")
FIXTURE_DATE = date.fromisoformat(FIXTURE_DAY)
TAPE = {"VT": (100.0, 97.0), "MSFT": (100.0, 98.5), "AAPL": (100.0, 100.0)}
EMPTY_PAGES = [{"orders": []}]


def _now(slot: str, *, shifted: bool = False) -> datetime:
    if slot == "1530":
        return datetime.combine(FIXTURE_DATE, time(15, 30), ET) + (time_delta(5) if shifted else time_delta(0))
    (hour, minute), _quote = SLOT_TIMES[slot]
    return datetime.combine(FIXTURE_DATE, time(hour, minute), ET) + (time_delta(5) if shifted else time_delta(0))


def time_delta(minutes: int):
    from datetime import timedelta

    return timedelta(minutes=minutes)


def _snapshot(slot: str, *, tickers=TAPE, bidask=None, market=None, shifted=False):
    now = _now(slot, shifted=shifted)
    payload = market if market is not None else market_data(tickers, slot=slot)
    sides = bidask if bidask is not None else {symbol: [price - 0.01, price + 0.01] for symbol, (_close, price) in tickers.items()}
    return MarketSnapshot(FIXTURE_DATE, now, "robinhood_agent_fetch", payload, sides), now


def _broker(pages=None, *, buying_power=1000.0):
    return BrokerState(FIXTURE_DATE, datetime.combine(FIXTURE_DATE, time(15, 30), ET), "fixture-account",
                       {"account_number": "fixture-account", "trading_day": FIXTURE_DAY,
                        "pages": EMPTY_PAGES if pages is None else pages}, buying_power)


def _orders(pages):
    result = []
    seen = set()
    for page in pages:
        for row in _order_rows(page):
            order_id = str(row.get("id")) if isinstance(row, dict) else ""
            if order_id and order_id in seen:
                continue
            seen.add(order_id)
            result.append(backup_planner._to_broker_order(row))
    return result


def _primary_candidates(slot, payload, config, now, deployed):
    if slot == "1530":
        rows = generate_buy_candidates.generate_candidates(
            slot="1530ET", now=now, config=config, market_data_by_source=payload["sources"],
            source_errors=payload.get("source_errors", []), ticker_errors=payload.get("ticker_errors"),
            already_covered_tickers=(), decision_time_et=now.time(),
        )
        output = []
        for row in rows:
            if row["action"] != "BUY":
                continue
            planner = [row["ticker"], row["suggested_amount_usd"], row["buy_multiplier"], row["multiplier_drop_pct"]]
            calibration = _calibration_fields(row["ticker"], row["buy_multiplier"], row["selected_base_amount_usd"])
            if calibration:
                planner += [calibration["sizing_base_usd"], calibration["sizing_min_drop_pct"], calibration["sizing_max_drop_pct"]]
            if generate_buy_candidates._planner_incompatible(planner):
                continue
            prior = backup_planner._prior_bar(payload["sources"], row["ticker"], str(row.get("target_price_source") or ""))
            candidate = {"ticker": row["ticker"], "desired_usd": row["suggested_amount_usd"],
                           "multiplier": row["buy_multiplier"], "drop_pct": row["multiplier_drop_pct"],
                           "current_price": row["target_price"], "prior_close": prior.get("close") if prior else None,
                           "prior_low": prior.get("low") if prior and prior.get("low", 0) > 0 else None}
            if calibration:
                candidate.update({
                    "sizing_base_usd": calibration["sizing_base_usd"],
                    "sizing_min_drop_pct": calibration["sizing_min_drop_pct"],
                    "sizing_max_drop_pct": calibration["sizing_max_drop_pct"],
                })
            output.append(candidate)
        return output
    spec = backup_stream_specs.stream_spec(slot)
    config = spec.config_loader(dca_config.get_config_dir(), allow_unpinned_account=True)
    document = build_document(
        spec, trading_day=FIXTURE_DATE, config=config, market_data=payload["sources"][spec.source.value],
        source_errors=payload.get("source_errors", []),
        ticker_errors=(payload.get("ticker_errors") or {}).get(spec.source.value, []),
        already_covered_tickers=(), deployed_last_7d_usd=deployed if spec.requires_deployed_last_7d_usd else None,
        decision_time_et=now.time(), config_dir=dca_config.get_config_dir(),
    )
    withheld = set(document.get("planner_incompatible_tickers") or ())
    return [{"ticker": row["ticker"], "desired_usd": row["desired_usd"], "multiplier": row["multiplier"],
             "drop_pct": row["drop_pct"], "current_price": row["current_price"],
             "prior_close": row["prior_close"], "prior_low": row.get("prior_low"), **{
                 key: row[key] for key in (
                     "sizing_base_usd", "sizing_min_drop_pct", "sizing_max_drop_pct"
                 ) if key in row
             }} for row in document["cands"] if row["ticker"] not in withheld]


def _hand_chain(slot, snapshot, broker, now, config_dir):
    pages = broker.orders_envelope["pages"]
    orders = _orders(pages)
    deployed = dca_order_plan.trailing_deployed_usd(orders, now=now)
    config = dca_config.load_strategy_config(config_dir, now=now, deployed_last_7d_usd=deployed)
    candidates = _primary_candidates(slot, snapshot.payload, config, now, deployed)
    if slot == "1530":
        verdict = dca_runner.assess(
            slot, orders, RecommendationEngine._candidate_rows(candidates),
            now=max(now, dca_order_plan.placement_window(slot, FIXTURE_DATE)[0]),
            buying_power_usd=broker.buying_power_usd, weekly_rollover_reserve_usd=0.0,
        )
        if verdict["verdict"] != "PROCEED":
            return verdict["verdict"], {}
        planned = [(row["ticker"], row["amount_usd"]) for row in verdict["plan"]["to_place"]]
    else:
        if broker.buying_power_usd is None:
            return "ABORT", {}
        parsed = dca_runner._parse_candidate_rows(RecommendationEngine._candidate_rows(candidates))
        normal = next(tier for tier in BUDGET_TIERS if tier.name.lower() == "normal")
        budget = dca_runner.budget_snapshot(orders, FIXTURE_DATE)
        plan = dca_runner.plan_orders(parsed, budget, normal, buying_power_usd=broker.buying_power_usd)
        planned = [(row.ticker, row.amount_usd) for row in plan.to_place]
    by_ticker = {row["ticker"]: row for row in candidates}
    topup = same_day_top_up_gate.evaluate_slot(
        pages=pages,
        candidates=[dict(by_ticker[ticker], desired_usd=amount) for ticker, amount in planned],
        account_number=broker.account_number, trading_day=FIXTURE_DAY, config_dir=config_dir,
    )
    if topup["verdict"] == "ABORT":
        return "ABORT", {}
    survivors = []
    config = dca_config.load_strategy_config(config_dir, now=now, deployed_last_7d_usd=deployed)
    for decision in topup["decisions"]:
        source = by_ticker.get(decision["ticker"])
        amount = decision["buy_usd"]
        if source and amount > 0 and dca_order_plan._spread_reason(
            snapshot.quote_sides.get(decision["ticker"]), config.max_bid_ask_spread_pct
        ) is None:
            survivors.append({**source, "buy_usd": amount})
    ladder = dca_order_plan.current_ladder_version(config_dir)
    result = dca_order_plan.plan_from_survivors(
        survivors, config=config, trading_day=FIXTURE_DATE, slot=slot, generated_at_et=now, ladder_version=ladder,
    )
    assert not result.errors and result.plan
    return "OK", {row["symbol"]: row["notional_usd"] for row in result.plan["orders"]}


@pytest.mark.parametrize("slot", ("0935", "1030", "1130", "1230", "1330", "1430", "1530"))
def test_real_primary_chain_parity_at_each_slot_anchor(tmp_path, slot):
    snapshot, now = _snapshot(slot)
    broker = _broker()
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    actual = engine.compute(snapshot, broker, now)
    expected_status, expected = _hand_chain(slot, snapshot, broker, now, dca_config.get_config_dir())
    assert actual.status == expected_status
    actual_map = {} if actual.recommendations is None else {item.ticker: item.usd for item in actual.recommendations.items}
    # Two empty maps would "agree" trivially; the fixture tape must actually produce buys.
    assert expected, f"{slot}: fixture produced no buys, so parity proves nothing"
    assert actual_map == expected


@pytest.mark.parametrize("slot", ("0935", "1030", "1130", "1230", "1330", "1430"))
def test_stream_slots_use_normal_tier_even_when_signal_count_would_escalate(tmp_path, slot):
    import near_close_dca

    symbols = list(dca_config.load_tickers(dca_config.get_config_dir())["tickers"])
    # The wider valid tape pushes Normal's daily cap; 20 signals alone do not spend $180.
    tickers = {symbol: (100.0, 97.0) for symbol in symbols}
    snapshot, now = _snapshot(slot, tickers=tickers)
    result = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path).compute(
        snapshot, _broker(), now,
    )
    assert result.status == "OK"
    total = sum(item.usd for item in result.recommendations.items)
    assert 170.0 < total <= 180.0
    # Every set carries the kill-switch state it was sized under, for the backup's poll-2 recheck.
    guard = result.recommendations.sizing_guard
    assert guard is not None and guard["max_multiplier"] >= 1.0 and guard["deployed_7d_usd"] >= 0.0
    normal = next(tier for tier in near_close_dca.BUDGET_TIERS if tier.name.lower() == "normal")
    assert normal.daily_cap_usd == 180.0


def test_final_slot_escalates_on_more_than_seventeen_high_multiplier_signals(tmp_path):
    # The same full-universe tape that holds every stream slot at Normal's $180 (above).
    symbols = list(dca_config.load_tickers(dca_config.get_config_dir())["tickers"])
    tickers = {symbol: (100.0, 97.0) for symbol in symbols}
    snapshot, now = _snapshot("1530", tickers=tickers)
    result = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path).compute(
        snapshot, _broker(), now,
    )
    assert result.status == "OK"
    assert len(result.recommendations.items) > 18
    # Escalation must actually lift spend past Normal's $180, the cap the stream slots keep.
    assert sum(item.usd for item in result.recommendations.items) > 180.0


def test_buying_power_none_aborts_stream_slot(tmp_path):
    snapshot, now = _snapshot("0935")
    result = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path).compute(
        snapshot, _broker(buying_power=None), now,
    )
    assert (result.status, result.reasons) == ("ABORT", ("buying_power_unavailable",))


def test_incomplete_pages_abort_at_real_top_up_gate(tmp_path):
    snapshot, now = _snapshot("0935")
    result = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path).compute(
        snapshot, _broker(pages=[]), now,
    )
    assert result.status == "ABORT"
    assert result.reasons == ("top_up_abort",)


def test_deployed_source_uses_artifacts_then_real_broker_fallback(tmp_path):
    now = datetime.combine(FIXTURE_DATE, time(12, 45), ET)
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    # No run-state files at all is missing evidence, not zero deployment (PR #1667 review).
    assert engine._deployed([], now) == (0.0, "broker_history_fallback")
    (tmp_path / "run_state_broken.json").write_text("not json", encoding="utf-8")
    assert engine._deployed([], now) == (0.0, "broker_history_fallback")


def test_repeated_order_id_across_pages_is_counted_once(tmp_path):
    snapshot, now = _snapshot("0935")
    row = {"id": "fixture-order", "symbol": "AAPL", "side": "buy", "state": "filled",
           "dollar_based_amount": {"amount": "100", "currency_code": "USD"},
           "created_at": "2026-10-07T13:40:00+00:00", "placed_agent": "agentic"}
    broker = _broker([{"orders": [row], "next_cursor": "c1"}, {"orders": [row]}])
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    orders = engine._broker_orders(broker.orders_envelope["pages"])
    assert len(orders) == 1
    assert dca_runner.budget_snapshot(orders, FIXTURE_DATE).day_notional_usd == 100.0
    assert engine.compute(snapshot, broker, now).status == "OK"


def test_wide_and_missing_spreads_drop_tickers(tmp_path):
    market, now = _snapshot("0935", bidask={"VT": [70.0, 100.0], "MSFT": None, "AAPL": [99.9, 100.1]})
    result = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path).compute(
        market, _broker(), now,
    )
    assert result.status == "OK"
    assert {item.ticker for item in result.recommendations.items} == set()


def test_moved_time_uses_generator_cutoff_for_a_stream(tmp_path):
    snapshot, now = _snapshot("1030", shifted=True)
    baseline_payload = snapshot.payload
    later = dict(baseline_payload)
    later["sources"] = {name: dict(rows) for name, rows in baseline_payload["sources"].items()}
    ticker = next(iter(later["sources"]["robinhood"]))
    future = dict(later["sources"]["robinhood"][ticker])
    future["intraday_bars"] = list(future["intraday_bars"]) + [{
        "timestamp": datetime.combine(FIXTURE_DATE, time(15, 59), ET).isoformat(),
        "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1000,
    }]
    later["sources"]["robinhood"][ticker] = future
    shifted_snapshot = MarketSnapshot(FIXTURE_DATE, now, snapshot.source, later, snapshot.quote_sides)
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    assert engine.compute(snapshot, _broker(), now).recommendations == engine.compute(shifted_snapshot, _broker(), now).recommendations


def test_quote_sides_model_round_trip():
    snapshot, _ = _snapshot("0935")
    assert MarketSnapshot.from_json(snapshot.to_json()) == snapshot


def test_slot_for_window_boundaries_and_after_last(tmp_path):
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    for slot in dca_runner.SLOTS:
        start, end = dca_order_plan.placement_window(slot, FIXTURE_DATE)
        assert engine.slot_for(start) == slot
        assert engine.slot_for(end - time_delta(1)) == slot
    final_slot = list(dca_runner.SLOTS)[-1]
    final_end = dca_order_plan.placement_window(final_slot, FIXTURE_DATE)[1]
    assert engine.slot_for(final_end) is None
    assert engine.slot_for(datetime.combine(FIXTURE_DATE, time(8, 0), ET)) == next(iter(dca_runner.SLOTS))
    with pytest.raises(ValueError, match="timezone-aware"):
        engine.slot_for(datetime.combine(FIXTURE_DATE, time(9, 0)))


def test_slots_for_active_and_next_window_opening():
    start, _end = dca_order_plan.placement_window("0935", FIXTURE_DATE)
    assert slots_for(start - time_delta(35), time_delta(35)) == ["0935"]
    assert slots_for(start, time_delta(54)) == ["0935"]
    assert slots_for(start, time_delta(55)) == ["0935", "1030"]
    assert slots_for(datetime.combine(FIXTURE_DATE, time(16, 0), ET), time_delta(10)) == []
    with pytest.raises(ValueError, match="timezone-aware"):
        slots_for(datetime.combine(FIXTURE_DATE, time(9, 0)), time_delta(5))
    with pytest.raises(ValueError, match="lookahead"):
        slots_for(start, time_delta(-1))


def test_compute_accepts_validated_future_slot_and_preserves_decision_time(tmp_path, monkeypatch):
    import dca_recommender

    seen = {}

    def build(spec, **kwargs):
        seen["slot"] = spec.runner_slot_id
        seen["decision_time_et"] = kwargs["decision_time_et"]
        return {"cands": [], "planner_incompatible_tickers": []}

    monkeypatch.setattr(dca_recommender, "build_document", build)
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    snapshot, _ = _snapshot("0935")
    now = datetime.combine(FIXTURE_DATE, time(9, 30), ET)
    result = engine.compute(snapshot, _broker(), now, slot="0935")
    assert result.slot == "0935"
    assert (seen["slot"], seen["decision_time_et"]) == ("0935", now.time())
    invalid = engine.compute(snapshot, _broker(), now, slot="bad")
    assert invalid.status == "ABORT" and invalid.reasons == ("invalid_slot",)


def test_compute_rejects_naive_clock_and_returns_no_slot(tmp_path):
    snapshot, now = _snapshot("0935")
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    assert engine.compute(snapshot, _broker(), datetime.combine(FIXTURE_DATE, time(9, 0))).reasons == (
        "clock_not_timezone_aware",
    )
    after_close = datetime.combine(FIXTURE_DATE, time(16, 0), ET)
    assert engine.compute(snapshot, _broker(), after_close).status == "NO_SLOT"


def test_deployed_source_uses_valid_run_state_figure(tmp_path):
    from datetime import timedelta

    day = FIXTURE_DAY
    state = {
        "trading_day": day,
        "generated_at": f"{day}T15:20:00-04:00",
        "run_classification": build_scheduled_classification(day),
        "buy_candidates": [{"ticker": "AAPL", "status": "submitted", "planned_amount_usd": 12.5}],
    }
    (tmp_path / "run_state_valid.json").write_text(json.dumps(state), encoding="utf-8")
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    value, source = engine._deployed([], datetime.combine(FIXTURE_DATE, time(15, 30), ET) + timedelta(minutes=1))
    assert (value, source) == (12.5, "run_state_artifacts")


@pytest.mark.parametrize("contents", ("[]", '{"buy_candidates":[{"status":"filled"}]}'))
def test_invalid_artifact_state_uses_broker_history_fallback(tmp_path, contents):
    (tmp_path / "run_state_invalid.json").write_text(contents, encoding="utf-8")
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    value, source = engine._deployed([], datetime.combine(FIXTURE_DATE, time(15, 30), ET))
    assert (value, source) == (0.0, "broker_history_fallback")


def test_real_top_up_gate_resizes_same_day_repeat(tmp_path):
    snapshot, now = _snapshot("0935")
    fill = {"id": "prior-msft", "symbol": "MSFT", "side": "buy", "state": "filled",
            "quantity": "0.01", "cumulative_quantity": "0.01", "average_price": "100",
            "dollar_based_amount": {"amount": "1", "currency_code": "USD"},
            "created_at": datetime.combine(FIXTURE_DATE, time(13, 0), ZoneInfo("UTC")).isoformat(),
            "placed_agent": "agentic"}
    broker = _broker([{"data": {"orders": [fill]}}])
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    result = engine.compute(snapshot, broker, now)
    assert result.status == "OK"
    # The engine has no logged reference artifact input, so the unbound broker fill fails closed.
    assert "MSFT" not in {row.ticker for row in result.recommendations.items}
    config = dca_config.load_strategy_config(dca_config.get_config_dir(), now=now, deployed_last_7d_usd=0.0)
    candidate = next(row for row in _primary_candidates("0935", snapshot.payload, config, now, 0.0)
                     if row["ticker"] == "MSFT")
    candidate.update(prior_logged_reference_price=100.0, prior_logged_reference_order_id="prior-msft")
    verdict = same_day_top_up_gate.evaluate_slot(
        pages=broker.orders_envelope["pages"], candidates=[candidate], account_number=broker.account_number,
        trading_day=FIXTURE_DAY, config_dir=dca_config.get_config_dir(),
    )
    assert verdict["verdict"] == "PROCEED"
    assert verdict["decisions"][0]["buy_usd"] == 2.96


def test_final_slot_assess_aborts_when_buying_power_is_unavailable(tmp_path):
    snapshot, now = _snapshot("1530")
    result = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path).compute(
        snapshot, _broker(buying_power=None), now,
    )
    assert result.status == "ABORT"


def test_malformed_envelope_fails_closed(tmp_path):
    snapshot, now = _snapshot("0935")
    broker = BrokerState(FIXTURE_DATE, now, "fixture-account", {}, 1000.0)
    result = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path).compute(
        snapshot, broker, now,
    )
    assert result.status == "ABORT"
    assert result.reasons == ("KeyError",)


def test_program_logged_reference_lets_the_real_gate_resize_a_repeat(tmp_path):
    # PR #1667 review: bind the latest plan-first buy's decision price to its broker order, so
    # Gate 6.5 resizes a cross-slot repeat the way the live prompts do instead of returning $0.
    from dca_rec_models import ExpectedPrice, ExpectedPriceSet, Recommendation, RecommendationSet
    from dca_rec_store import RecommendationStore
    from scripts.validate_native_placement import native_ref_id

    snapshot, now = _snapshot("0935")
    created = datetime.combine(FIXTURE_DATE, time(13, 0), ZoneInfo("UTC")).isoformat()
    fill = {"id": "prior-msft", "symbol": "MSFT", "side": "buy", "state": "filled",
            "quantity": "0.01", "cumulative_quantity": "0.01", "average_price": "100",
            "dollar_based_amount": {"amount": "1", "currency_code": "USD"},
            "created_at": created, "placed_agent": "agentic"}
    store = RecommendationStore(tmp_path / "recommendations")
    as_of = datetime.combine(FIXTURE_DATE, time(8, 55), ET)
    earlier = RecommendationSet(FIXTURE_DATE, as_of, "0935", "robinhood_agent_fetch", "v", "h",
                                (Recommendation("MSFT", 1.0, native_ref_id("0935", FIXTURE_DAY, "MSFT")),))
    store.write(earlier, ExpectedPriceSet(FIXTURE_DATE, as_of, "0935", "run_state_artifacts",
                                          (ExpectedPrice("MSFT", 100.0, 101.5, None, 1.0, 1.48, 1.0),),
                                          {"budget_tier": "normal", "daily_cap_usd": 180.0, "weekly_cap_usd": 900.0}))
    store.record_handed(earlier)
    placement = store.placements_dir(FIXTURE_DATE, "0935") / "MSFT.json"
    placement.write_text(json.dumps({"data": {"order": {"id": "prior-msft", "created_at": created}}}), encoding="utf-8")

    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    result = engine.compute(snapshot, _broker([{"data": {"orders": [fill]}}]), now)
    assert result.status == "OK"
    assert {row.ticker: row.usd for row in result.recommendations.items}["MSFT"] == 2.96


def test_order_bearing_artifact_without_deployment_amounts_falls_back_to_broker_history(tmp_path):
    # PR #1815 review: plan_first_slot finish writes orders_submitted_count but neither buy_candidates nor
    # budget.day_notional_usd, so the risk reader counts its purchases as $0 and the seven-day guard disengaged.
    now = datetime.combine(FIXTURE_DATE, time(12, 45), ET)
    engine = RecommendationEngine(config_dir=dca_config.get_config_dir(), cron_root=tmp_path)
    (tmp_path / "run_state.json").write_text(json.dumps({
        "trading_day": FIXTURE_DATE.isoformat(), "orders_submitted_count": 2, "order_submit_status": "submitted",
        "classification": {"is_scheduled": True},
    }), encoding="utf-8")
    assert engine._orders_without_amounts(json.loads((tmp_path / "run_state.json").read_text()))
    assert engine._deployed([], now) == (0.0, "broker_history_fallback")
    assert not engine._orders_without_amounts({"orders_submitted_count": 0})
    assert not engine._orders_without_amounts({"orders_submitted_count": True})
