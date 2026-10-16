from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

import dca_runner
from dca_rec_models import (
    SLOT_IDS,
    BrokerState,
    ExpectedPrice,
    ExpectedPriceSet,
    MarketSnapshot,
    Recommendation,
    RecommendationSet,
)

DAY = date(2026, 10, 13)
STAMP = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
UUID = "abcdef12-1234-5678-1234-abcdef123456"
PAYLOAD = {"sources": {}, "source_errors": [], "ticker_errors": {}}


def rec(ticker="SPY", usd=12.34):
    return Recommendation(ticker, usd, UUID)


def rec_set(items=()):
    return RecommendationSet(DAY, STAMP, "1530", "robinhood_agent_fetch", "v1", "abc", tuple(items))


def price():
    return ExpectedPrice("SPY", 100.0, 101.0, None, 1.2, 0.99, 15.0)


def test_slot_ids_match_live_runner_contract():
    assert set(SLOT_IDS) == set(dca_runner.SLOTS)


def test_market_snapshot_round_trip_and_constructor_validation():
    value = MarketSnapshot(DAY, STAMP, "robinhood_agent_fetch", PAYLOAD)
    assert MarketSnapshot.from_json(value.to_json()) == value
    for args in [
        (datetime(2026, 10, 13), STAMP, "robinhood_agent_fetch", PAYLOAD),
        (DAY, datetime(2026, 10, 13), "robinhood_agent_fetch", PAYLOAD),
        (DAY, STAMP, "bad", PAYLOAD),
        (DAY, STAMP, "robinhood_agent_fetch", {"wrong": {}}),
        (DAY, STAMP, "robinhood_agent_fetch", {"sources": [], "source_errors": [], "ticker_errors": {}}),
        (DAY, STAMP, "robinhood_agent_fetch", {"sources": {}, "source_errors": [], "ticker_errors": {}, "extra": 1}),
        (DAY, STAMP, "robinhood_agent_fetch", {"sources": {"x": float("nan")}, "source_errors": [], "ticker_errors": {}}),
    ]:
        with pytest.raises(ValueError):
            MarketSnapshot(*args)
    for data in [None, [], {"trading_day": "2026-10-13"},
                 {"trading_day": "2026-10-13", "as_of": STAMP.isoformat(), "source": "x", "payload": PAYLOAD},
                 {"trading_day": 20261013, "as_of": STAMP.isoformat(), "source": "robinhood_agent_fetch", "payload": PAYLOAD},
                 {"trading_day": "not-a-day", "as_of": STAMP.isoformat(), "source": "robinhood_agent_fetch", "payload": PAYLOAD},
                 {"trading_day": "20261013", "as_of": STAMP.isoformat(), "source": "robinhood_agent_fetch", "payload": PAYLOAD},
                 {"trading_day": "2026-10-13", "as_of": 1, "source": "robinhood_agent_fetch", "payload": PAYLOAD},
                 {"trading_day": "2026-10-13", "as_of": "not-a-time", "source": "robinhood_agent_fetch", "payload": PAYLOAD}]:
        with pytest.raises(ValueError):
            MarketSnapshot.from_json(data)


def test_broker_state_redacts_account_number_and_validates_direct_construction():
    state = BrokerState(DAY, STAMP, "sensitive-account", {"orders": []}, 12.34)
    payload = state.to_json()
    assert payload["account_number"] == "<redacted>"
    assert "sensitive-account" not in str(payload)
    with pytest.raises(ValueError):
        BrokerState.from_json(payload)
    assert BrokerState.from_json({**payload, "account_number": "restored-account"}).buying_power_usd == 12.34
    for args in [
        (DAY, datetime(2026, 10, 13), "acct", {}, None),
        (DAY, STAMP, "", {}, None),
        (DAY, STAMP, "acct", [], None),
        (DAY, STAMP, "acct", {}, True),
        (DAY, STAMP, "acct", {"x": float("inf")}, None),
    ]:
        with pytest.raises(ValueError):
            BrokerState(*args)
    for number in (float("nan"), "12", True):
        with pytest.raises(ValueError):
            BrokerState.from_json({"trading_day": DAY.isoformat(), "as_of": STAMP.isoformat(),
                                   "account_number": "acct", "orders_envelope": {}, "buying_power_usd": number})


def test_recommendation_round_trip_price_free_and_direct_invariants():
    item = rec()
    row = rec_set((item,))
    data = row.to_json()
    assert RecommendationSet.from_json(data) == row

    def keys(value):
        if isinstance(value, dict):
            yield from value
            for child in value.values():
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    forbidden = ("price", "close", "low", "quote", "bar")
    assert all(not any(word in key.lower() for word in forbidden) for key in keys(data))
    for args in [("spy", 1.0, UUID), ("TOO-LONG-TICKER", 1.0, UUID), ("SPY", 1.234, UUID),
                 ("SPY", 0, UUID), ("SPY", float("inf"), UUID), ("SPY", True, UUID), ("SPY", 1.0, "bad")]:
        with pytest.raises(ValueError):
            Recommendation(*args)
    with pytest.raises(ValueError):
        RecommendationSet(DAY, STAMP, "1530", "src", "v1", "hash", [item])
    with pytest.raises(ValueError):
        rec_set((item, item))
    with pytest.raises(ValueError):
        RecommendationSet(DAY, datetime(2026, 10, 13), "1530", "src", "v1", "hash", ())
    with pytest.raises(ValueError):
        replace(rec_set(), slot="1530ET")
    with pytest.raises(ValueError):
        replace(rec_set(), schema_version=True)
    for changed in [
        {"usd": 1.001}, {"ticker": "x"}, {"ref_id": "bad"}, {"close": 1}, {"schema_version": 1.0},
        {"slot": "bad"},
    ]:
        payload = rec_set((item,)).to_json()
        if "close" in changed:
            payload["items"][0]["close"] = changed["close"]
        else:
            payload.update(changed)
        with pytest.raises(ValueError):
            RecommendationSet.from_json(payload)
    with pytest.raises(ValueError):
        RecommendationSet.from_json({**rec_set().to_json(), "items": {}})
    for malformed in (None, [], {"ticker": "SPY"}, {"ticker": "SPY", "usd": 1, "ref_id": UUID, "x": 0}):
        with pytest.raises(ValueError):
            Recommendation.from_json(malformed)


def test_no_price_key_guard_runs_during_direct_construction_and_serialization():
    class LeakingRecommendation(Recommendation):
        def to_json(self):
            return {"ticker": self.ticker, "usd": self.usd, "ref_id": self.ref_id, "nested": {"close": 1}}

    leaking_item = LeakingRecommendation("SPY", 1.0, UUID)
    with pytest.raises(ValueError, match="price field"):
        RecommendationSet(DAY, STAMP, "1530", "src", "v1", "hash", (leaking_item,))

    unsafe = object.__new__(RecommendationSet)
    for name, value in {"trading_day": DAY, "as_of": STAMP, "slot": "1530", "source": "src",
                        "ladder_version": "v1", "universe_hash": "hash", "items": (leaking_item,),
                        "schema_version": 1}.items():
        object.__setattr__(unsafe, name, value)
    with pytest.raises(ValueError, match="price field"):
        unsafe.to_json()


def test_expected_price_models_round_trip_and_validate():
    expected = price()
    assert ExpectedPrice.from_json(expected.to_json()) == expected
    bundle = ExpectedPriceSet(DAY, STAMP, "0935", "broker_history", (expected,),
                              {"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900})
    assert ExpectedPriceSet.from_json(bundle.to_json()) == bundle
    with pytest.raises(ValueError):
        ExpectedPrice("spy", 1, 1, None, 1, 1, 1)
    for field in ("decision_price", "prior_close", "multiplier", "drop_pct", "notional_usd"):
        with pytest.raises(ValueError):
            replace(expected, **{field: float("nan")})
    with pytest.raises(ValueError):
        replace(expected, prior_low=True)
    with pytest.raises(ValueError):
        ExpectedPriceSet(DAY, STAMP, "bad", "source", (),
                         {"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900})
    with pytest.raises(ValueError):
        ExpectedPriceSet(DAY, STAMP, "0935", "source", [expected],
                         {"budget_tier": "Normal", "daily_cap_usd": 180, "weekly_cap_usd": 900})
    with pytest.raises(ValueError):
        ExpectedPriceSet.from_json({**bundle.to_json(), "items": {}})
    for bad_budget in (
        {"budget_tier": "normal", "daily_cap_usd": 0, "weekly_cap_usd": 900},
        {"budget_tier": "normal", "daily_cap_usd": 180, "weekly_cap_usd": float("inf")},
        {"budget_tier": "", "daily_cap_usd": 180, "weekly_cap_usd": 900},
    ):
        with pytest.raises(ValueError):
            replace(bundle, budget=bad_budget)
    with pytest.raises(ValueError):
        ExpectedPrice.from_json({**expected.to_json(), "other": 1})


@pytest.mark.parametrize("field,value", [
    ("decision_price", 0.0), ("decision_price", -1.0), ("prior_close", 0.0), ("prior_low", 0.0),
    ("multiplier", 0.0), ("notional_usd", -3.0), ("drop_pct", -0.1),
])
def test_expected_price_rejects_non_positive_values(field, value):
    # PR #1667 review: a zero decision price would reach fill comparison's division.
    from dca_rec_models import ExpectedPrice
    values = {"ticker": "AAPL", "decision_price": 100.0, "prior_close": 102.0, "prior_low": 99.0,
              "multiplier": 1.5, "drop_pct": 1.96, "notional_usd": 3.0, field: value}
    with pytest.raises(ValueError, match=f"{field} must be positive"):
        ExpectedPrice(**values)
