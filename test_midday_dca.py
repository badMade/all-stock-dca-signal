"""Self-tests for the midday graded-DCA executor (12:30 PM ET live execution).

Covers the midday stream identity (the shared $3-base 0x-to-5x ladder used by
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
    # share the SAME $3-base 0x-to-5x ladder.
    assert MIDDAY_SIZING_TIERS == StrategyConfig().sizing_tiers
    assert ex.multiplier_for_drop(0.08) == 5.0
    assert graded_buy_multiplier(0.08, StrategyConfig()) == 5.0
    assert MIDDAY_SIZING_TIERS == MORNING_SIZING_TIERS


def test_midday_ladder_brackets():
    ex = MiddayDCAExecutor(MiddayConfig(base_buy_amount_usd=3.0))
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
    res = ex.run(_now_midday(), data)

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
    ex = MiddayDCAExecutor(MiddayConfig(tickers=("BIG",), base_buy_amount_usd=3.0))
    # Use declining bars so the strict downward-trend check (last close < first close) passes.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now_midday(), data)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is True
    assert sig.execution_status == EXEC_AUTO_BUY_ELIGIBLE
    # 0x-to-5x ladder: 10% drop -> 5x -> $15.
    assert sig.hypothetical_amount_usd == 15.0


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
    cfg = MiddayConfig(tickers=("BIG",), base_buy_amount_usd=3.0,
                       morning_run_state_root=str(tmp_path))
    ex = MiddayDCAExecutor(cfg)
    data = FewBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    sig = ex.evaluate(_now_midday(), data)[0]
    assert sig.action == Action.BUY
    assert sig.trend_validated is False
    assert sig.execution_status == EXEC_MANUAL_REVIEW_CANDIDATE

    res = ex.run(_now_midday(), data)
    art = res["artifacts"]
    assert art["counts"]["manual_review_candidates"] == 1
    assert art["manual_review_tickers"] == ["BIG"]


# --- Same-day top-up guard --------------------------------------------------
def test_top_up_no_prior_buys_full_amount(tmp_path):
    prior_root = tmp_path / "morning"
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data)
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_NO_PRIOR_RUN
    assert row["incremental_order_amount"] == 15.0
    # Ledger recorded the full (un-offset) amount.
    day = _now_midday().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(day) == 15.0


def test_top_up_incremental_when_price_below_prior(tmp_path):
    prior_root = tmp_path / "morning"
    # Morning bought ~$3 at a fill price of 95.0.
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
    # Midday price 90.0 is strictly below the 95.0 morning reference.
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data)
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_INCREMENTAL
    assert row["prior_same_day_amount"] == 3.0
    assert row["prior_same_day_reference_price"] == 95.0
    assert row["midday_decision_price"] == 90.0
    # Desired $15 minus prior $3 = $12 incremental.
    assert row["incremental_order_amount"] == 12.0
    day = _now_midday().astimezone(EASTERN).date()
    assert ex.ledger.day_planned(day) == 12.0


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
        return self.duplicate

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
        prior_root, ticker="BIG", planned_amount=3.0,
        fill_price=95.0, quantity=round(3.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0,
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
    assert res["artifacts"]["broker_outcomes"]["BIG"]["status"] == "submitted"
    assert res["artifacts"]["broker_outcomes"]["BIG"]["skip_reason"] == "same-stream broker order exists"
    assert res["artifacts"]["buy_candidates"][0]["status"] == "submitted"
    assert res["artifacts"]["buy_candidates"][0]["planned_amount_usd"] == 12.0


class _TopUpBrokerWithoutSameStreamCheck(_TopUpDuplicateBroker):
    same_day_strategy_order_exists_with_ref_id_prefix = None


def test_guarded_midday_top_up_blocks_when_same_stream_checker_unavailable(tmp_path):
    prior_root = tmp_path / "morning"
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=3.0,
        fill_price=95.0, quantity=round(3.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0,
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
        prior_root, ticker="BIG", planned_amount=3.0,
        fill_price=95.0, quantity=round(3.0 / 95.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0,
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
    assert broker.placed == [("BIG", 12.0, "midday-dca-topup-2026-07-08-BIG")]
    assert res["artifacts"]["broker_outcomes"]["BIG"]["status"] == "submitted"


def test_top_up_zero_when_price_not_below_prior(tmp_path):
    prior_root = tmp_path / "morning"
    # Morning fill price 50.0 (below the midday price of 90.0).
    _write_prior_morning_run(
        prior_root, ticker="BIG", planned_amount=3.0,
        fill_price=50.0, quantity=round(3.0 / 50.0, 6),
    )
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data)
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
    _write_prior_morning_run(prior_root, ticker="BIG", planned_amount=3.0)
    cfg = MiddayConfig(
        tickers=("BIG",), base_buy_amount_usd=3.0,
        morning_run_state_root=str(tmp_path / "midday"),
        prior_allocation_roots=(str(prior_root),),
    )
    ex = MiddayDCAExecutor(cfg)
    data = FixedDropProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    res = ex.run(_now_midday(), data)
    row = res["artifacts"]["buy_candidates"][0]
    assert row["top_up_guard_result"] == TOP_UP_MISSING_PRIOR_PRICE
    assert row["incremental_order_amount"] == 0.0
    assert row["top_up_manual_review"] is True
    assert res["artifacts"]["top_up_manual_review_tickers"] == ["BIG"]


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
    res = ex.run(_now_midday(), data)

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


def test_midday_executor_default_init_loads_shipped_config():
    ex = MiddayDCAExecutor()
    assert ex.config.base_buy_amount_usd == 3.0
    assert ex.config.morning_daily_cap_usd == 90.0
    assert ex.state_root == DEFAULT_MIDDAY_LIVE_STATE_ROOT
    assert ex.config.sizing_tiers == StrategyConfig().sizing_tiers


def test_midday_ledger_namespace_isolated_from_morning_and_afternoon(tmp_path):
    from near_close_dca import StrategyState

    afternoon = StrategyState()
    day = _now_midday().astimezone(EASTERN).date()
    cfg = MiddayConfig(tickers=("BIG",), base_buy_amount_usd=3.0,
                       morning_run_state_root=str(tmp_path),
                       prior_allocation_roots=(str(tmp_path / "none"),))
    ex = MiddayDCAExecutor(cfg)
    assert ex.ledger.namespace == "midday"
    data = DecliningBarsProvider(SourceName.ROBINHOOD, 100.0, {"BIG": 0.10})
    ex.run(_now_midday(), data)
    assert afternoon.day_notional(day) == 0.0
    assert ex.ledger.day_planned(day) > 0.0


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
