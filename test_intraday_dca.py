"""Self-tests for the intraday live-execution stream config loader.

Focused on the fail-closed config-loading fix (Codex review: "Keep disabled
streams disabled when config loading fails") rather than full coverage of
intraday_dca.py, which reuses morning_dca.MorningDCAExecutor's already
heavily-tested pipeline.
"""

import os

os.environ.setdefault("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", "999000111")

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from intraday_dca import IntradayConfig, IntradayDCAExecutor, load_intraday_config
from morning_dca import EASTERN, Bar
from near_close_dca import ROBINHOOD_AGENTIC_ACCOUNT_NUMBER


def test_missing_config_file_fails_closed_to_shadow_mode(tmp_path):
    # config/intraday_dca.json ships live_execution_enabled=false ("SHIPS
    # DISABLED") -- a missing config file must not silently fall back to
    # MorningConfig's inherited live_execution_enabled=True default, which
    # would turn a shadow-only stream live precisely when its config cannot
    # be trusted.
    cfg = load_intraday_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert isinstance(cfg, IntradayConfig)
    assert cfg.live_execution_enabled is False


def test_unparseable_config_file_fails_closed_to_shadow_mode(tmp_path):
    (tmp_path / "intraday_dca.json").write_text("{not valid json", encoding="utf-8")
    cfg = load_intraday_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is False


def test_explicit_true_in_a_readable_config_still_overrides_the_fail_closed_default(tmp_path):
    # The fail-closed default must not prevent a real, readable config from
    # explicitly re-enabling live placement once it has been vetted.
    (tmp_path / "intraday_dca.json").write_text(
        json.dumps({"live_execution_enabled": True}), encoding="utf-8"
    )
    cfg = load_intraday_config(config_dir=str(tmp_path), allow_unpinned_account=True)
    assert cfg.live_execution_enabled is True


class _DecliningBarsProvider:
    """Provider generating strictly declining intraday bars for one ticker."""

    def __init__(self, previous_close, drop):
        self.source = "robinhood"
        self._prev = previous_close
        self._drop = drop

    def get_previous_close_bar(self, ticker, as_of):
        ts = datetime.combine(as_of.astimezone(EASTERN).date() - timedelta(days=1), time(16, 0), tzinfo=EASTERN)
        p = self._prev
        return Bar(timestamp=ts, open=p, high=p, low=p, close=p, volume=0.0)

    def get_intraday_bars(self, ticker, start, end, interval_minutes):
        final_price = self._prev * (1.0 - self._drop)
        start_price = self._prev * (1.0 - self._drop * 0.5)
        timestamps = []
        cursor = start
        while cursor <= end:
            timestamps.append(cursor)
            cursor += timedelta(minutes=interval_minutes)
        n = len(timestamps)
        anchor_idx = n - 2 if n >= 2 else 0
        bars = []
        for i, ts in enumerate(timestamps):
            if i <= anchor_idx and anchor_idx > 0:
                price = start_price + (final_price - start_price) * i / anchor_idx
            else:
                price = final_price
            bars.append(Bar(timestamp=ts, open=price, high=price, low=price, close=price, volume=100.0))
        return bars

    def get_bid_ask(self, ticker, as_of):
        price = self._prev * (1.0 - self._drop)
        return (price * 0.9995, price * 1.0005)


class _RefIdTrackingBroker:
    """Broker double that remembers placed ref_ids for real duplicate checks."""

    def __init__(self):
        self.placed = {}
        self._ref_ids = []

    def account_number(self):
        return ROBINHOOD_AGENTIC_ACCOUNT_NUMBER

    def available_buying_power(self):
        return 1000.0

    def same_day_strategy_order_exists(self, ticker, trading_day):
        return any(t == ticker for t, _ in self._ref_ids)

    def same_day_strategy_order_exists_with_ref_id_prefix(self, ticker, trading_day, ref_id_prefixes):
        return any(
            t == ticker and any(rid.startswith(p) for p in ref_id_prefixes)
            for t, rid in self._ref_ids
        )

    def is_tradable_for_fractional_buy(self, ticker):
        return True

    def review_market_buy(self, ticker, notional_usd):
        return True, "ok"

    def place_market_buy(self, ticker, notional_usd, ref_id):
        self.placed[ticker] = notional_usd
        self._ref_ids.append((ticker, ref_id))
        return f"ORDER-{ticker}-{len(self._ref_ids)}"


def test_idempotency_stream_differs_per_slot_but_matches_a_same_slot_retry():
    # Codex review, P1 ("Scope intraday idempotency keys to each slot"): all
    # three slots share self.stream == "intraday", so the idempotency
    # namespace used for the ref_id / same-stream duplicate check must
    # include the fired slot too, or a later slot's legitimate top-up
    # collides with an earlier slot's ref id.
    ex_1130 = IntradayDCAExecutor(slot="1130")
    ex_1330 = IntradayDCAExecutor(slot="1330")
    ex_1130_retry = IntradayDCAExecutor(slot="1130")

    assert ex_1130._idempotency_stream() != ex_1330._idempotency_stream()
    assert ex_1130._idempotency_stream() == ex_1130_retry._idempotency_stream()


def test_later_slot_top_up_is_not_treated_as_earlier_slots_duplicate(tmp_path):
    # Integration-level regression: an 11:30 fill must not block a genuine,
    # lower-price 13:30 incremental top-up for the same ticker just because
    # both slots share stream="intraday" and would otherwise collide on the
    # same ref_id/duplicate-check namespace.
    live_root = tmp_path / "live"
    trading_day = date(2026, 7, 8)
    cfg = IntradayConfig(
        tickers=("NVDA",),
        base_buy_amount_usd=3.0,
        morning_run_state_root=str(live_root),
        prior_allocation_roots=(str(live_root),),
        approved_test_dates=frozenset({trading_day}),
    )
    broker = _RefIdTrackingBroker()

    ex_1130 = IntradayDCAExecutor(cfg, slot="1130")
    now_1130 = datetime.combine(trading_day, time(11, 30), tzinfo=EASTERN)
    data_1130 = _DecliningBarsProvider(100.0, 0.03)
    res_1130 = ex_1130.run(now_1130, data_1130, broker=broker)
    assert broker.placed.get("NVDA") == pytest.approx(4.5)  # 1.5x * $3
    first_ref_ids = list(broker._ref_ids)

    # Confirm the fill so the next slot's top-up guard has a known reference
    # price to top up against (mirrors production reconciliation).
    state_path = Path(res_1130["paths"]["run_state"])
    state = json.loads(state_path.read_bytes())
    for row in state["buy_candidates"]:
        if row["ticker"] == "NVDA":
            row["status"] = "filled"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    ex_1330 = IntradayDCAExecutor(cfg, slot="1330")
    now_1330 = datetime.combine(trading_day, time(13, 30), tzinfo=EASTERN)
    data_1330 = _DecliningBarsProvider(100.0, 0.05)  # deeper drop -> lower price
    ex_1330.run(now_1330, data_1330, broker=broker)

    assert len(broker._ref_ids) == 2, (
        "the 13:30 slot must place its own incremental order, not be skipped "
        "as a duplicate of the 11:30 slot's ref id"
    )
    assert broker._ref_ids[1][1] != first_ref_ids[0][1]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
