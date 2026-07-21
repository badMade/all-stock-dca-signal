"""Self-tests for the midday graded-DCA executor (12:30 PM ET live execution).

Covers the midday stream identity (the shared $2-base 0x-to-5x ladder used by
all live streams: morning, midday, and afternoon), the run-state
path/classification, the live-mode confidence rule (inherited), the same-day
top-up guard against morning/manual prior allocations, and the guarantee that a
midday run stays isolated from the morning and afternoon artifact trees.

Runnable with ``pytest`` or ``python test_midday_dca.py``.
"""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from midday_dca import (
    DEFAULT_MIDDAY_LIVE_STATE_ROOT,
    MIDDAY_LIVE_CRON_ID,
    MIDDAY_SIZING_TIERS,
    MiddayConfig,
    MiddayDCAExecutor,
    load_midday_config,
)
from morning_dca import (
    DEFAULT_MORNING_LIVE_STATE_ROOT,
    EXEC_AUTO_BUY_ELIGIBLE,
    EXEC_MANUAL_REVIEW_CANDIDATE,
)
from near_close_dca import (
    EASTERN,
    TOP_UP_INCREMENTAL,
    TOP_UP_MISSING_PRIOR_PRICE,
    TOP_UP_NO_PRIOR_RUN,
    TOP_UP_PRICE_NOT_BELOW,
    Action,
    Bar,
    SourceName,
    StrategyConfig,
    graded_buy_multiplier,
)


def _now_midday():
    """Decision timestamp for the 12:30 PM ET live execution."""
    return datetime.combine(date(2026, 7, 8), time(12, 30), tzinfo=EASTERN)


class FakeLiveBroker:
    """Minimal successful broker for scheduled midday live tests."""

    def account_number(self):
        from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    def available_buying_power(self):
        return 500.0

    def same_day_strategy_order_exists(self, ticker, trading_day):
        return False

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        # No duplicate: this fake broker never has prior orders on file, so
        # guarded top-ups always proceed as fresh placements.
        return None

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        return f"ORDER-{ticker}"


class FixedDropProvider:
    """Deterministic provider: each ticker drops a fixed fraction vs. prior close."""

    def __init__(self, source: SourceName, previous_close: float, drops: dict) -> None:
        self.source = source
        self._prev = previous_close
        self._drops = drops

    def get_previous_close_bar(self, ticker: str, as_of: datetime):
        ts = datetime.combine(
            as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN
        )
        p = self._prev
        return Bar(timestamp=ts, open=p, high=p, low=p, close=p, volume=0.0)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        bars = []
        cursor = start
        while cursor <= end:
            bars.append(Bar(timestamp=cursor, open=price, high=price, low=price, close=price, volume=100.0))
            cursor += timedelta(minutes=interval_minutes)
        return bars


class DecliningBarsProvider(FixedDropProvider):
    """Provider that generates strictly declining intraday bars.

    Bars start at the midpoint between previous close and the final dropped price,
    declining linearly to the final price. Satisfies the strict downward-trend
    requirement in `_trend_validated` (last close < first close).
    """

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        drop = self._drops.get(ticker, 0.0)
        final_price = self._prev * (1.0 - drop)
        start_price = self._prev * (1.0 - drop * 0.5) if drop > 0 else final_price
        timestamps = []
        cursor = start
        while cursor <= end:
            timestamps.append(cursor)
            cursor += timedelta(minutes=interval_minutes)
        n = len(timestamps)
        # Interpolate so the bar at end-interval_minutes (the completed-bar anchor
        # returned by latest_bar_at_or_before) is exactly final_price.  The last
        # bar (at end) is also set to final_price.
        anchor_idx = n - 2 if n >= 2 else 0
        bars = []
        for i, ts in enumerate(timestamps):
            if i <= anchor_idx and anchor_idx > 0:
                price = start_price + (final_price - start_price) * i / anchor_idx
            else:
                price = final_price
            bars.append(Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=100.0))
        return bars


def _write_prior_morning_run(
    root: Path,
    *,
    ticker: str,
    planned_amount: float,
    fill_price=None,
    quantity=None,
    trading_day=date(2026, 7, 8),
    generated_at="2026-07-08T10:30:00-04:00",
):
    """Write a synthetic morning/manual prior-allocation artifact under ``root``.

    When ``fill_price``/``quantity`` are given an ``orders.csv`` fill is written
    too, so ``load_prior_same_day_allocations`` resolves a known reference price;
    otherwise the reference price stays unknown (manual-review path).
    """
    run_dir = root / f"{trading_day.strftime('%Y%m%d')}_1030ET"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_state = {
        "trading_day": trading_day.isoformat(),
        "generated_at": generated_at,
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "morning",
            "run_id": "manual_morning_live_test",
        },
        "buy_candidates": [
            {"ticker": ticker, "status": "filled", "planned_amount_usd": planned_amount}
        ],
    }
    (run_dir / "run_state.json").write_text(json.dumps(run_state), encoding="utf-8")
    if fill_price is not None and quantity is not None:
        (run_dir / "orders.csv").write_text(
            "ticker,status,avg_fill_price,quantity_filled,order_id\n"
            f"{ticker},filled,{fill_price},{quantity},ord_test_1\n",
            encoding="utf-8",
        )
    return run_dir


# --- Ladder identity --------------------------------------------------------
def test_midday_ladder_matches_afternoon_and_morning():
    from morning_dca import MORNING_SIZING_TIERS

    ex = MiddayDCAExecutor(MiddayConfig())
    # As of the Jul 10 2026 update all live streams (morning, midday, afternoon)
    # share the SAME $2-base 0x-to-5x ladder.
    assert MIDDAY_SIZING_TIERS == StrategyConfig().sizing_tiers
    assert ex.multiplier_for_drop(0.08) == 5.0
    assert graded_buy_multiplier(0.08, StrategyConfig()) == 5.0
    assert MIDDAY_SIZING_TIERS == MORNING_SIZING_TIERS


def test_midday_ladder_brackets():
    ex = MiddayDCAExecutor(MiddayConfig(base_buy_amount_usd=2.0))
    cases = {
        0.0005: 0.0,
        0.001: 1.0,
        0.01: 1.5,
        0.02: 2.0,
        0.03: 2.5,
        0.04: 3.0,
        0.05: 3.5,
        0.06: 4.0,
        0.07: 4.5,
        0.08: 5.0,
        0.20: 5.0,
    }
    for drop, expected in cases.items():
        assert ex.multiplier_for_drop(drop) == expected, (drop, expected)


# --- Run-state path + classification ----------------------------------------
def test_midday_run_state_path_and_classification(tmp_path):
    cfg = MiddayConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())

    run_state_path = Path(res["paths"]["run_state"])
    assert run_state_path.parent.name == "20260708_1230ET"
    assert run_state_path.exists()

    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "scheduled"
    assert rc["run_type"] == "midday_dca_live"
    assert rc["stream"] == "midday"
    assert rc["trades"] is True
    assert rc["scheduled_for_et"] == "12:30"
    assert rc["scheduled_window_start_et"] == "12:25"
    assert rc["scheduled_window_end_et"] == "12:35"
    assert rc["state_schema_version"] == "midday-live-v1"
    assert rc["run_id"] == "midday_live_20260708_1230ET"
    assert rc["cron_id"] == MIDDAY_LIVE_CRON_ID
    assert rc["supersedes_cron_id"] is None
    assert rc["valid_afternoon_evidence"] is False
    # Codex review (PR #78): MiddayDCAExecutor inherits run_classification()
    # from MorningDCAExecutor unchanged; valid_morning_evidence must be scoped
    # to self.stream == "morning", not hardcoded True, or a midday artifact
    # would misreport itself as valid MORNING evidence.
    assert rc["valid_morning_evidence"] is False

    art = res["artifacts"]
    assert art["mode"] == "live"
    assert art["trades"] is True
    assert art["ladder"]["name"] == "midday_0x_to_5x"
    assert art["budget"]["ledger_namespace"] == "midday"
    assert "midday_daily_cap_usd" in art["budget"]
    # Scheduled pointer uses the midday-specific filename.
    assert "latest_midday_run" in res["paths"]
    assert (Path(tmp_path) / "latest_midday_run.json").exists()


def test_midday_manual_run_id_and_no_pointer(tmp_path):
    cfg = MiddayConfig(tickers=("SMALL",), morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_midday(), data, manual=True)
    rc = res["artifacts"]["run_classification"]
    assert rc["run_origin"] == "manual"
    assert rc["manual_run"] is True
    assert rc["run_id"].startswith("manual_midday_live_")
    assert "latest_midday_run" not in res["paths"]
    assert not (Path(tmp_path) / "latest_midday_run.json").exists()


# --- Live confidence rule (inherited) ---------------------------------------
def test_midday_confidence_rule_auto_buys_with_validated_trend():
    ex = MiddayDCAExecutor(MiddayConfig(tickers=("BIG",), base_buy_amount_usd=2.0))
    # Use declining bars so the strict downward-trend check (last close < first close) passes.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now_midday(), data)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is True
    assert sig.execution_status == EXEC_AUTO_BUY_ELIGIBLE
    # 0x-to-5x ladder: 10% drop -> 5x -> $10.
    assert sig.hypothetical_amount_usd == 10.0


class FewBarsProvider(FixedDropProvider):
    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        price = self._prev * (1.0 - self._drops.get(ticker, 0.0))
        anchor = end - timedelta(minutes=interval_minutes)
        return [
            Bar(timestamp=anchor - timedelta(minutes=interval_minutes),
                open=price, high=price, low=price, close=price, volume=100.0),
            Bar(timestamp=anchor, open=price, high=price, low=price, close=price, volume=100.0),
        ]


def test_midday_confidence_rule_withholds_when_trend_unvalidated(tmp_path):
    cfg = MiddayConfig(tickers=("BIG",), base_buy_amount_usd=2.0,
                       morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = FewBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now_midday(), data)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is False
    assert sig.execution_status == EXEC_MANUAL_REVIEW_CANDIDATE

    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    art = res["artifacts"]
    assert art["counts"]["manual_review_candidates"] == 1
    assert art["manual_review_tickers"] == ["BIG"]


# --- Same-day top-up guard --------------------------------------------------
def test_top_up_no_prior_buys_full_amount(tmp_path):
    prior_root = tmp_path / "morning"
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_NO_PRIOR_RUN
    assert row["incremental_order_amount"] == 10.0
    # Ledger recorded the full (un-offset) amount.
    day = _now_midday().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(day) == 10.0


def test_top_up_incremental_when_price_below_prior(tmp_path):
    prior_root = tmp_path / "morning"
    # Morning bought ~$2 at a fill price of 95.0.
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    # Midday price 90.0 is strictly below the 95.0 morning reference.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_INCREMENTAL
    assert row["prior_same_day_amount"] == 2.0
    assert row["prior_same_day_reference_price"] == 95.0
    assert row["midday_decision_price"] == 90.0
    # Desired $10 minus prior $2 = $8 incremental.
    assert row["incremental_order_amount"] == 8.0
    day = _now_midday().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(day) == 8.0


class _WideSpreadProvider(DecliningBarsProvider):
    """A provider exposing a live bid/ask quote with an excessive spread."""

    def __init__(self, *args, spread_pct=5.0, **kwargs):
        super().__init__(*args, **kwargs)
        self._spread_pct = spread_pct

    def get_bid_ask(self, ticker, as_of):
        midpoint = 100.0
        half_spread = midpoint * (self._spread_pct / 100.0) / 2.0
        return (midpoint - half_spread, midpoint + half_spread)


def test_midday_spread_breaker_blocks_wide_spread(tmp_path):
    # Codex review #12 on PR #22: the spread circuit breaker added to the
    # afternoon stream (PR #37) and the morning stream must also cover
    # midday, since MiddayDCAExecutor shares _place_broker_orders with
    # MorningDCAExecutor.
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        max_bid_ask_spread_pct=1.0,
    )
    ex = MiddayDCAExecutor(cfg)
    data = _WideSpreadProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10}, spread_pct=5.0)
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["status"] == "blocked"
    outcome = res["artifacts"]["broker_outcomes"]["BIG"]
    assert "spread" in outcome["skip_reason"]


class _TopUpDuplicateBroker:
    def __init__(self, duplicate: bool) -> None:
        self.duplicate = duplicate
        self.broad_duplicate_calls = 0
        self.same_stream_calls = 0
        self.placed = []

    def account_number(self):
        from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

        return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    def available_buying_power(self):
        return 500.0

    def same_day_strategy_order_exists(self, ticker, trading_day):
        self.broad_duplicate_calls += 1
        return True

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        self.same_stream_calls += 1
        assert ref_id_prefixes[0] == f"midday-dca-topup-{trading_day.isoformat()}-{ticker}"
        if self.duplicate:
            return {
                "id": f"EXISTING-ORDER-{ticker}",
                "state": "confirmed",
                "dollar_amount": "12.00",
                "ref_id": ref_id_prefixes[0],
            }
        return None

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.placed.append((ticker, notional_usd, ref_id))
        return f"ORDER-{ticker}"


def test_guarded_midday_top_up_checks_same_stream_duplicate_before_placing(tmp_path):
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MiddayDCAExecutor(cfg)
    broker = _TopUpDuplicateBroker(duplicate=True)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    res = ex.run(_now_midday(), data, broker=broker)

    assert broker.broad_duplicate_calls == 0
    assert broker.same_stream_calls == 1
    assert broker.placed == []
    outcome = res["artifacts"]["broker_outcomes"]["BIG"]
    assert outcome["status"] == "submitted"
    assert outcome["skip_reason"] == "same-stream broker order exists"
    assert outcome["order_id"] == "EXISTING-ORDER-BIG"
    assert outcome["submitted_amount_usd"] == 12.0
    assert res["artifacts"]["buy_candidates"][0]["status"] == "submitted"


def test_manual_retry_of_guarded_top_up_duplicate_is_skipped_not_submitted(tmp_path):
    # Codex review on PR #22: a manual/recovery rerun always writes to its own
    # fresh, uuid-suffixed artifact directory (see run_id()) rather than
    # overwriting the original scheduled run's run_state.json. If a same-stream
    # duplicate found on a MANUAL retry were still recorded as a fresh
    # "submitted" candidate (the way a same-path SCHEDULED retry legitimately
    # is), load_prior_same_day_allocations() would find both files and
    # double-count the same broker order as two separate same-day allocations.
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MiddayDCAExecutor(cfg)
    broker = _TopUpDuplicateBroker(duplicate=True)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    res = ex.run(_now_midday(), data, broker=broker, manual=True)

    assert broker.same_stream_calls == 1
    assert broker.placed == []
    outcome = res["artifacts"]["broker_outcomes"]["BIG"]
    assert outcome["status"] == "skipped"
    assert "same-stream broker order exists" in outcome["skip_reason"]
    assert "submitted_amount_usd" not in outcome
    assert res["artifacts"]["buy_candidates"][0]["status"] == "skipped"


class _TopUpBrokerWithoutSameStreamCheck(_TopUpDuplicateBroker):
    same_day_strategy_order_exists_with_ref_id_prefix = None


def test_guarded_midday_top_up_blocks_when_same_stream_checker_unavailable(tmp_path):
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MiddayDCAExecutor(cfg)
    broker = _TopUpBrokerWithoutSameStreamCheck(duplicate=False)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    res = ex.run(_now_midday(), data, broker=broker)

    outcome = res["artifacts"]["broker_outcomes"]["BIG"]
    assert broker.placed == []
    assert outcome["status"] == "blocked"
    assert outcome["skip_reason"].startswith("duplicate-check failed: broker does not support same-stream")


def test_guarded_midday_top_up_ignores_cross_stream_duplicate_for_incremental_order(tmp_path):
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=95.0, quantity=round(2.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    ex = MiddayDCAExecutor(cfg)
    broker = _TopUpDuplicateBroker(duplicate=False)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})

    res = ex.run(_now_midday(), data, broker=broker)

    assert broker.broad_duplicate_calls == 0
    assert broker.same_stream_calls == 1
    assert broker.placed == [("BIG", 8.0, "midday-dca-topup-2026-07-08-BIG")]
    assert res["artifacts"]["broker_outcomes"]["BIG"]["status"] == "submitted"


def test_top_up_zero_when_price_not_below_prior(tmp_path):
    prior_root = tmp_path / "morning"
    # Morning fill price 50.0 (below the midday price of 90.0).
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=2.0,
        fill_price=50.0, quantity=round(2.0 / 50.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    # DecliningBarsProvider (not the flat FixedDropProvider): the intraday trend
    # gate added on base rejects flat-or-rising sessions, and this test needs a
    # trend-validated candidate so it lands in buy_candidates rather than
    # manual_review_candidates. broker=FakeLiveBroker() satisfies the newer
    # "missing broker adapter" gate for scheduled live runs.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_PRICE_NOT_BELOW
    assert row["incremental_order_amount"] == 0.0
    assert row["top_up_skipped"] is True
    assert res["artifacts"]["top_up_offset_tickers"] == ["BIG"]
    day = _now_midday().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(day) == 0.0


def test_top_up_manual_review_when_prior_price_unknown(tmp_path):
    prior_root = tmp_path / "morning"
    # Prior amount known but no orders.csv -> unknown reference price.
    _write_prior_morning_run(prior_root, ticker="BIG", planned_amount=2.0)
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=2.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    # Same trend-validated-provider + broker rationale as the test above.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_MISSING_PRIOR_PRICE
    assert row["incremental_order_amount"] == 0.0
    assert row["top_up_manual_review"] is True
    assert res["artifacts"]["top_up_manual_review_tickers"] == ["BIG"]


def test_augment_artifacts_appends_top_up_manual_review_candidate_row():
    """_augment_artifacts must promote the full buy_candidates row to manual_review_candidates."""
    from morning_dca import TopUpOutcome
    cfg = MiddayConfig(tickers=("BIG",))
    ex = MiddayDCAExecutor(cfg)
    outcome = TopUpOutcome(
        ticker="BIG",
        desired_amount=6.0,
        decision_price=99.0,
        prior_same_day_amount=3.0,
        prior_same_day_reference_price=None,
        prior_same_day_run_id="sched_am",
        prior_same_day_source="morning",
        incremental_order_amount=0.0,
        top_up_guard_result="missing_prior_price",
        top_up_skipped=True,
        top_up_manual_review=True,
    )
    candidate_row = {
        "ticker": "BIG",
        "prior_same_day_amount": 3.0,
        "prior_same_day_reference_price": None,
        "top_up_guard_result": "missing_prior_price",
        "incremental_order_amount": 0.0,
        "execution_status": EXEC_MANUAL_REVIEW_CANDIDATE,
    }
    artifacts = {
        "counts": {"manual_review_candidates": 0},
        "manual_review_tickers": [],
        "manual_review_candidates": [],
        "buy_candidates": [candidate_row],
    }
    ex._augment_artifacts(artifacts, {"BIG": outcome})
    assert artifacts["counts"]["manual_review_candidates"] == 1
    assert artifacts["manual_review_tickers"] == ["BIG"]
    assert len(artifacts["manual_review_candidates"]) == 1
    row = artifacts["manual_review_candidates"][0]
    assert row["ticker"] == "BIG"
    assert row["top_up_guard_result"] == "missing_prior_price"
    assert row["prior_same_day_amount"] == pytest.approx(3.0)


def test_manual_review_candidate_row_keeps_top_up_context_when_trend_unvalidated(tmp_path):
    # Codex review on the PR #43 merge: a trend-unvalidated BUY can still have a
    # same-day top-up outcome (_compute_top_up only checks action/multiplier, not
    # trend_validated). Withholding the row from buy_candidates must not also
    # strip the prior-allocation/top-up fields from the manual_review_candidates
    # row -- otherwise a reviewer looking at a 12:30 manual-review case after an
    # earlier morning buy has no way to tell what incremental top-up amount, if
    # any, would be safe.
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=3.0,
        fill_price=95.0, quantity=round(3.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    # FixedDropProvider is flat, so _trend_validated() (>= rejects flat-or-rising)
    # withholds this candidate to manual_review_candidates -- but the ticker still
    # has a resolvable prior fill at $95.0, current price $90.0 is below that
    # reference, so _compute_top_up still produces a real TOP_UP_INCREMENTAL outcome.
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())

    assert res["artifacts"]["buy_candidates"] == []
    [row] = res["artifacts"]["manual_review_candidates"]
    assert row["ticker"] == "BIG"
    assert row["prior_same_day_amount"] == 3.0
    assert row["prior_same_day_reference_price"] == 95.0
    assert row["top_up_guard_result"] == TOP_UP_INCREMENTAL
    # 10% drop is beyond the top 8% sizing tier -> capped at 5.0x * $3 base = $15
    # desired; incremental = $15 - the prior $3 fill = $12.
    assert row["incremental_order_amount"] == pytest.approx(12.0)


# --- Isolation from other streams -------------------------------------------
def test_midday_root_is_distinct_from_morning_and_afternoon():
    assert DEFAULT_MIDDAY_LIVE_STATE_ROOT != DEFAULT_MORNING_LIVE_STATE_ROOT
    assert "ce16ba6b" not in DEFAULT_MIDDAY_LIVE_STATE_ROOT
    assert "midday_dca" in DEFAULT_MIDDAY_LIVE_STATE_ROOT


def test_midday_does_not_write_afternoon_or_morning_artifacts(tmp_path):
    afternoon_root = tmp_path / "ce16ba6b"
    (afternoon_root / "scheduled_runs" / "20260708_1520ET").mkdir(parents=True)
    latest = afternoon_root / "latest_scheduled_run.json"
    latest.write_text(json.dumps({"run_id": "scheduled_20260708_1520ET"}), encoding="utf-8")
    before = latest.read_text(encoding="utf-8")

    midday_root = tmp_path / "midday_dca"
    cfg = MiddayConfig(tickers=("SMALL",), morning_run_state_root=str(midday_root),
                       prior_allocation_roots=(str(tmp_path / "nonexistent_morning"),))
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())

    assert latest.read_text(encoding="utf-8") == before
    assert list(afternoon_root.rglob("run_state*.json")) == []
    for path in res["paths"].values():
        assert str(midday_root) in path


def test_midday_scheduled_run_counts_as_afternoon_prior_context():
    from near_close_dca import _prior_run_is_scheduled

    cfg = MiddayConfig(tickers=("SMALL",))
    midday_rc = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=False)
    assert midday_rc["run_origin"] == "scheduled"
    assert _prior_run_is_scheduled({"run_classification": midday_rc}) is False


# --- Config loading ---------------------------------------------------------
def test_load_midday_config_reads_config_file(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text(
        json.dumps({
            "sizing_ladder": {
                "base_amount_usd": 4.0,
                "no_buy_below_pct": 0.2,
                "tiers": [
                    {"min_drop_pct": 0.2, "max_drop_pct": 3.0, "multiplier": 1.0},
                    {"min_drop_pct": 3.0, "max_drop_pct": None, "multiplier": 2.0},
                ],
            },
            "budget_ledger": {
                "midday_daily_cap_usd": 70.0,
                "midday_weekly_cap_usd": 350.0,
            },
            "artifact_paths": {
                "live_run_state_root": "/custom/midday/root",
                "prior_allocation_roots": ["/custom/morning/root"],
            },
        }),
        encoding="utf-8",
    )
    cfg = load_midday_config(str(cfg_dir))
    assert cfg.base_buy_amount_usd == 4.0
    assert abs(cfg.minimum_drop_vs_previous_close - 0.002) < 1e-9
    assert cfg.morning_daily_cap_usd == 70.0
    assert cfg.morning_weekly_cap_usd == 350.0
    assert cfg.morning_run_state_root == "/custom/midday/root"
    assert cfg.prior_allocation_roots == ("/custom/morning/root",)


# --- PR #22 bot-review regression coverage (batch: midday config loading) ----


def test_load_midday_config_reads_configured_ticker_whitelist(tmp_path):
    # Codex review #71: the midday live stream must load its ticker whitelist
    # from config/tickers.json, not only the hardcoded module-level
    # ACTIVE_TICKERS/EXCLUDED_TICKERS defaults.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "tickers.json").write_text(
        json.dumps({"active": ["ZZZZ"], "excluded": ["YYYY"]}),
        encoding="utf-8",
    )

    cfg = load_midday_config(str(cfg_dir))

    assert cfg.tickers == ("ZZZZ",)
    assert cfg.excluded_tickers == frozenset({"YYYY"})


def test_load_midday_config_raises_on_invalid_configured_order_cap(tmp_path):
    # Codex review #85: an explicitly configured but invalid
    # circuit_breakers.max_per_order_usd must fail closed -- raise -- rather
    # than silently keep the default cap for a live trading stream.
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "budgets.json").write_text(
        json.dumps({"circuit_breakers": {"max_per_order_usd": float("nan")}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_midday_config(str(cfg_dir))


def test_load_midday_config_reads_approved_dates_from_trading_window(tmp_path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "midday_dca.json").write_text("{}", encoding="utf-8")
    (cfg_dir / "trading_window.json").write_text(
        json.dumps({"start_date": "2026-07-02", "end_date": "2026-07-06", "weekdays_only": True}),
        encoding="utf-8",
    )

    cfg = load_midday_config(str(cfg_dir))

    assert cfg.approved_test_dates == frozenset({date(2026, 7, 2), date(2026, 7, 3), date(2026, 7, 6)})
    assert cfg.decision_time_et is None


def test_midday_executor_default_init_loads_shipped_config():
    ex = MiddayDCAExecutor()
    assert ex.config.base_buy_amount_usd == 2.0
    assert ex.config.morning_daily_cap_usd == 90.0
    assert date(2026, 7, 2) in ex.config.approved_test_dates
    assert date(2026, 8, 28) in ex.config.approved_test_dates
    assert ex.state_root == DEFAULT_MIDDAY_LIVE_STATE_ROOT
    assert ex.config.sizing_tiers == StrategyConfig().sizing_tiers


def test_midday_ledger_namespace_isolated_from_morning_and_afternoon(tmp_path):
    from near_close_dca import StrategyState

    afternoon = StrategyState()
    day = _now_midday().astimezone(EASTERN).date()
    cfg = MiddayConfig(tickers=("BIG",), base_buy_amount_usd=2.0,
                       morning_run_state_root=str(tmp_path),
                       prior_allocation_roots=(str(tmp_path / "none"),))
    ex = MiddayDCAExecutor(cfg)
    assert ex.ledger.namespace == "midday"
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    ex.run(_now_midday(), data, broker=FakeLiveBroker())
    assert afternoon.day_notional(day) == 0.0
    assert ex.ledger.day_planned(day) > 0.0


def test_midday_seeding_ignores_manual_reruns(tmp_path):
    # Codex review (follow-up): manual_reruns/ under the midday root are
    # documented (near_close_dca.py) as post-12:30 backfill artifacts that
    # feed ONLY the afternoon top-up guard, never the midday stream's own
    # scheduled cap ledger. MiddayDCAExecutor inherits
    # _seed_ledger_from_prior_runs from MorningDCAExecutor unchanged, so it
    # was also folding manual_reruns/*/run_state.json into the midday
    # ledger -- meaning a scheduled midday retry could see its own daily cap
    # already partly consumed by an unrelated manual override, wrongly
    # blocking buys the original 12:30 run would have allowed.
    cfg = MiddayConfig(
        tickers=("AAA",),
        morning_daily_cap_usd=20.0,
        morning_weekly_cap_usd=200.0,
        morning_run_state_root=str(tmp_path),
    )
    ex = MiddayDCAExecutor(cfg)
    trading_day = _now_midday().astimezone(EASTERN).date()

    manual_dir = tmp_path / "manual_reruns" / "manual_run_1"
    manual_dir.mkdir(parents=True)
    (manual_dir / "run_state.json").write_text(
        json.dumps({
            "run_id": "manual_run_1",
            "trading_day": trading_day.isoformat(),
            "buy_candidates": [
                {"ticker": "ZZZ", "status": "submitted", "planned_amount_usd": 15.0}
            ],
        }),
        encoding="utf-8",
    )

    ex._seed_ledger_from_prior_runs(_now_midday())

    assert ex.ledger.day_planned(trading_day) == 0.0
    assert ex.ledger.remaining_daily(trading_day) == 20.0


def test_manual_midday_run_with_wrong_broker_account_raises_before_ordering(tmp_path):
    import pytest

    class _WrongAccountBroker:
        def __init__(self):
            self.buying_power_checked = False
            self.placed = False

        def account_number(self):
            return "not-the-approved-account"

        def available_buying_power(self):
            self.buying_power_checked = True
            raise AssertionError("buying power should not be checked for wrong account")

        def place_market_buy(self, ticker, notional_usd, ref_id):
            self.placed = True
            raise AssertionError("orders must not be placed for wrong account")

    cfg = MiddayConfig(
        tickers=("SMALL",),
        morning_run_state_root=str(tmp_path / "midday"),
        target_account_number="562057042",
        approved_test_dates=frozenset({date(2026, 7, 8)}),
    )
    broker = _WrongAccountBroker()
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"SMALL": 0.03})

    with pytest.raises(RuntimeError, match="broker account mismatch: expected 562057042, got not-the-approved-account"):
        ex.run(_now_midday(), data, manual=True, broker=broker)

    assert broker.buying_power_checked is False
    assert broker.placed is False


# --- Schema validation ------------------------------------------------------
def test_midday_classification_validates_against_json_schema():
    try:
        import jsonschema
    except ImportError:
        return
    schema_path = Path(__file__).resolve().parent / "schemas" / "midday_run_classification.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    cfg = MiddayConfig(tickers=("SMALL",))
    scheduled = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=False)
    manual = MiddayDCAExecutor(cfg).run_classification(_now_midday(), manual=True)
    for rc in (scheduled, manual):
        jsonschema.validate(rc, schema)


# --- Raw-input persistence (live_schedule_hardening_2026_07_20, inherited) --
def test_midday_scheduled_live_run_persists_raw_inputs(tmp_path):
    # MiddayDCAExecutor inherits MorningDCAExecutor.run() unchanged, so the
    # raw-input persistence / failure-state guarantee applies to the 12:30
    # scheduled live task too, under its OWN state root.
    cfg = MiddayConfig(tickers=("BIG",), base_buy_amount_usd=2.0, morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data, broker=FakeLiveBroker())
    artifacts = res["artifacts"]
    assert artifacts.get("status") != "blocked"
    raw_dir = Path(artifacts["raw_input_dir"])
    assert raw_dir.is_dir()
    assert raw_dir.parent.name == "20260708_1230ET"
    assert artifacts["raw_inputs_saved"]
    assert "raw_persistence_error" not in artifacts


if __name__ == "__main__":
    import sys
    import tempfile

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                if "tmp_path" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                    with tempfile.TemporaryDirectory() as d:
                        fn(Path(d))
                else:
                    fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
