#!/usr/bin/env python3
"""Tests for the Gate 5 (0935ET market-open) candidate generator.

Scope note: ``scripts/generate_market_open_candidates.py`` covers the 9:35 AM
market-open slot only. It is a sibling of, not a replacement for,
``scripts/generate_buy_candidates.py`` (1530ET): the two slots run on
genuinely different signal engines -- ``MarketOpenDCAExecutor.evaluate()`` here
versus ``near_close_dca.evaluate_source_signal()`` there -- so these tests
assert against the market-open executor's own behavior, including its
single-provider contract and its trend-validation exemption.

The most important test in this file is
``test_document_matches_the_executor_called_directly``: it is what proves this
entry point REUSES the real executor rather than reimplementing it, which is
the whole reason it exists (each prompt's Gate 5: "Use the repo entry point,
never a reimplementation").
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

import pytest

gmoc = pytest.importorskip(
    "scripts.generate_market_open_candidates",
    reason="scripts/generate_market_open_candidates.py not present",
)

import market_open_dca  # noqa: E402
from market_open_dca import MarketOpenConfig, MarketOpenDCAExecutor  # noqa: E402
from near_close_dca import Action  # noqa: E402
from scripts import stream_candidates  # noqa: E402
from scripts.refresh_market_open_candidates import refresh_market_open_candidates  # noqa: E402

ET = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_CONFIG_DIR = REPO_ROOT / "config"
# A Wednesday; 2026-08-04 is the immediately preceding market session, which is
# what _StaticMarketData requires previous_close_bar to be dated (it resolves
# that date from config/market_calendar.json, not from a fixed day offset).
DAY = date(2026, 8, 5)
PREVIOUS_SESSION = date(2026, 8, 4)
DECISION_ET = time(9, 35)
# completed_bar_anchor(09:35, 5-minute interval) is 09:30 -- the 09:35 bar has
# only just opened and is deliberately excluded as unfinished, so the 09:30 bar
# is the decision bar. Every fixture below therefore carries a 09:30 bar.
DECISION_BAR_ET = time(9, 30)


def _bar(moment: datetime, price: float, *, low: float | None = None, volume: float = 1000.0) -> dict:
    low = price if low is None else low
    return {
        "timestamp": moment.isoformat(),
        "open": price,
        "high": price + 0.01,
        "low": low,
        "close": price,
        "volume": volume,
    }


def _previous_close_bar(close: float, low: float) -> dict:
    """The prior completed regular-session daily bar: its close feeds
    previous_close and its low feeds previous_low, the two inputs
    near_close_dca.select_first_allocation_base_usd uses to pick the $1 vs $2
    first-allocation base."""
    return _bar(datetime.combine(PREVIOUS_SESSION, time(16, 0), tzinfo=ET), close, low=low)


def _intraday_bars(
    price: float, *, start: time = time(9, 30), end: time = DECISION_ET, step_minutes: int = 5
) -> list:
    """Bars spaced ``step_minutes`` apart, start-stamped.

    Defaults to 5 minutes -- ``config.interval_minutes``'s native cadence --
    since ``MARKET_OPEN_SPEC.source_interval_minutes`` is unset and every
    fixture in this file feeds the Robinhood-labeled 'sources.robinhood' map at
    that cadence. Bars spaced any tighter than the cadence the scoring config
    resolves to are rejected by
    ``stream_candidates._require_bars_match_configured_cadence``.
    """
    bars = []
    cur = datetime.combine(DAY, start, tzinfo=ET)
    stop = datetime.combine(DAY, end, tzinfo=ET)
    while cur <= stop:
        bars.append(_bar(cur, price))
        cur += timedelta(minutes=step_minutes)
    return bars


def _ticker_payload(
    *,
    price: float,
    previous_close: float,
    previous_low: float,
    quote: dict | None = None,
    start: time = time(9, 30),
    step_minutes: int = 5,
) -> dict:
    payload: dict[str, Any] = {
        "previous_close_bar": _previous_close_bar(previous_close, previous_low),
        "intraday_bars": _intraday_bars(price, start=start, step_minutes=step_minutes),
    }
    if quote is None:
        quote = {
            "price": price,
            "as_of": datetime.combine(DAY, time(9, 49), tzinfo=ET).isoformat(),
        }
    payload["quote"] = quote
    return payload


def _market_data(**per_ticker: dict) -> dict:
    return {
        "sources": {"robinhood": dict(per_ticker)},
        "source_errors": [],
        "ticker_errors": {"robinhood": []},
    }


def _config(**overrides: Any) -> MarketOpenConfig:
    """A hermetic market-open config with a one-ticker universe.

    Explicit sizing_tiers rather than the checked-in ladder so a config change
    cannot silently rewrite what these tests assert; explicit empty
    per_ticker_sizing so the global tiers below are the only ladder in play.
    """
    base: dict[str, Any] = dict(
        tickers=("TEST",),
        excluded_tickers=frozenset(),
        per_ticker_sizing={},
        sizing_tiers=((0.001, 1.0), (0.02, 1.5), (0.04, 2.0), (0.05, 2.5), (0.08, 5.0)),
        minimum_drop_vs_previous_close=0.001,
        base_buy_amount_usd=2.0,
        base_buy_amount_below_close_usd=1.0,
        interval_minutes=5,
        live_execution_enabled=False,
    )
    base.update(overrides)
    return MarketOpenConfig(**base)


def _run(
    market_data: dict,
    config: MarketOpenConfig | None = None,
    *,
    source_errors: Sequence[str] = (),
    ticker_errors: Sequence[str] = (),
    already_covered_tickers: Sequence[str] = (),
    check_planner_compatibility: bool = False,
) -> dict:
    """Call the public helper directly.

    ``check_planner_compatibility`` defaults to False here because these
    fixtures use a hermetic test ladder that has nothing to do with the
    checked-in config/sizing_ladder.json dca_runner validates planner rows
    against -- every row would be reported incompatible for reasons unrelated
    to what each test is asserting. The planner path has its own tests below.
    """
    return gmoc.generate_market_open_candidates(
        trading_day=DAY,
        config=_config() if config is None else config,
        market_data=market_data["sources"]["robinhood"],
        source_errors=source_errors,
        ticker_errors=ticker_errors,
        already_covered_tickers=already_covered_tickers,
        check_planner_compatibility=check_planner_compatibility,
    )


def _only(document: dict, ticker: str = "TEST") -> dict:
    matches = [row for row in document["candidates"] if row["ticker"] == ticker]
    assert len(matches) == 1, f"expected exactly one row for {ticker}, got {matches}"
    return matches[0]


# --- the signal itself ------------------------------------------------------


def test_qualifying_drop_below_close_and_low_buys_at_the_two_dollar_base():
    """5% below the prior close AND below the prior low -> the $2 base at the
    2.5x tier. This is the case that produced GATE5_DISABLED (place nothing)
    on every fire before this entry point existed."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    row = _only(_run(data))
    assert row["action"] == Action.BUY.value
    assert row["buy_multiplier"] == pytest.approx(2.5)
    assert row["selected_base_amount_usd"] == pytest.approx(2.0)
    assert row["suggested_amount_usd"] == pytest.approx(5.0)
    assert row["drop_vs_previous_close_pct"] == pytest.approx(5.0)
    assert row["target_price"] == pytest.approx(95.0)


def test_below_close_but_above_prior_low_buys_at_the_one_dollar_base():
    """Below the prior close but NOT below the prior low -> the reduced $1
    first-allocation base (near_close_dca.select_first_allocation_base_usd),
    same rule every live stream uses."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=94.0))
    row = _only(_run(data))
    assert row["action"] == Action.BUY.value
    assert row["selected_base_amount_usd"] == pytest.approx(1.0)
    assert row["suggested_amount_usd"] == pytest.approx(2.5)


def test_flat_tape_is_a_quiet_tape_not_an_error():
    """At/above the prior close -> zero candidates and no exception. Gate 5's
    "zero candidates: end silently" branch depends on this being a normal,
    successful document rather than a failure."""
    data = _market_data(TEST=_ticker_payload(price=100.0, previous_close=100.0, previous_low=99.0))
    document = _run(data)
    assert document["buy_count"] == 0
    assert document["same_day_top_up_candidates"] == []
    assert _only(document)["action"] == Action.SKIP.value


def test_trend_exemption_marks_the_buy_auto_buy_eligible():
    """The whole reason this slot needs its own entry point: at 09:35 only five
    minutes of regular-session data exist, so the 60-minute downtrend filter can
    never validate. config/scheduled_tasks.json:2ba19526.trend_validation_exemption
    exempts this slot, and market_open_dca.MARKET_OPEN_PROFILES enforces that in
    code -- so a qualifying drop is auto-buy eligible with no trend evidence at
    all, rather than being flagged manual_review_candidate and placing nothing.
    """
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data)
    row = _only(document)
    assert document["trend_validation_exempt"] is True
    assert row["execution_status"] == "auto_buy_eligible"
    assert row["auto_buy_signal"] is True
    # Not False -- the trend was never evaluated, which is a different thing
    # from evaluated-and-failed, and the evidence must not conflate them.
    assert row["trend_validated"] is None


def test_a_fresh_quote_is_used_at_the_live_window_anchor():
    """At the 09:50 evaluation boundary, a fresh quote -- not the completed
    decision bar's close -- is the decision price."""
    quote_time = datetime.combine(DAY, time(9, 49), tzinfo=ET)
    data = _market_data(
        TEST=_ticker_payload(
            price=99.0,
            previous_close=100.0,
            previous_low=97.0,
            quote={"price": 94.0, "as_of": quote_time.isoformat()},
        )
    )
    row = _only(_run(data))
    assert row["target_price"] == pytest.approx(94.0)
    assert row["drop_vs_previous_close_pct"] == pytest.approx(6.0)


def test_quote_obtained_after_nominal_time_is_accepted_inside_live_window():
    quote_time = datetime.combine(DAY, time(9, 38), tzinfo=ET)
    data = _market_data(
        TEST=_ticker_payload(
            price=99.0,
            previous_close=100.0,
            previous_low=97.0,
            quote={"price": 94.0, "as_of": quote_time.isoformat()},
        )
    )
    row = _only(_run(data))
    assert row["target_price"] == pytest.approx(94.0)


def test_a_stale_quote_falls_back_to_the_now_completed_0945_bar():
    """A quote too old to be verified as the decision moment's price is treated
    exactly like no quote at all, falling back to a decision bar already known
    to be fresh -- never trusted as-is (Codex review, 1530ET module: "Reject
    stale quote timestamps"). Robinhood-primary now, so this runs through the
    base ``_StaticMarketData.get_quote()`` staleness path rather than
    ``StreamMarketData.get_quote()``'s Finance-specific branch, which no
    longer applies to this slot. The fresh fallback bar this slot now falls
    back to sits at 09:45 -- one 5-minute Robinhood interval before the
    09:50 evaluation-delayed anchor -- rather than 09:30, since this slot no
    longer resolves its cadence from ``MARKET_OPEN_SPEC.source_interval_
    minutes`` (15, Finance) but from ``config.interval_minutes`` (5,
    Robinhood's native cadence)."""
    stale = datetime.combine(DAY, time(9, 10), tzinfo=ET)
    payload = _ticker_payload(
        price=99.0,
        previous_close=100.0,
        previous_low=97.0,
        quote={"price": 94.0, "as_of": stale.isoformat()},
    )
    # Extend the bars to 09:45 so the fallback decision bar stays fresh at the
    # effective anchor; the default (through DECISION_ET, 09:35) would be one
    # interval too stale under the 5-minute cadence.
    payload["intraday_bars"] = _intraday_bars(99.0, end=time(9, 45))
    row = _only(_run(_market_data(TEST=payload)))
    assert row["action"] == Action.BUY.value
    assert row["target_price"] == pytest.approx(99.0)


# --- reuse, not reimplementation -------------------------------------------


def test_document_matches_the_executor_called_directly():
    """Parity check: every published row must equal what
    MarketOpenDCAExecutor.evaluate() produces for the same inputs.

    This is the assertion that keeps this module an ENTRY POINT rather than a
    fourth implementation of the signal logic. If someone ever inlines the
    grading math here "for speed" or "to add a field", this test fails.
    """
    config = _config()
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data, config)

    provider = stream_candidates.StreamMarketData(
        gmoc.MARKET_OPEN_SOURCE,
        data["sources"]["robinhood"],
        quote_staleness_minutes=config.interval_minutes,
        trading_day=DAY,
    )
    executor = MarketOpenDCAExecutor(config=config, mode=market_open_dca.MARKET_OPEN_LIVE_MODE)
    now = datetime.combine(DAY, executor.decision_time, tzinfo=ET)
    signals = executor.evaluate(now, provider)

    assert [row["ticker"] for row in document["candidates"]] == [s.ticker for s in signals]
    for row, signal in zip(document["candidates"], signals, strict=True):
        assert row["action"] == signal.action.value
        assert row["buy_multiplier"] == pytest.approx(signal.multiplier)
        assert row["suggested_amount_usd"] == pytest.approx(round(signal.hypothetical_amount_usd, 2))
        assert row["target_price"] == signal.current_price
        assert row["execution_status"] == signal.execution_status


def test_document_identifies_the_slot_and_stream_it_speaks_for():
    """A Gate 6/6.5/9 caller reads these fields to route the document; a wrong
    stream label here would file market-open evidence under another stream."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data)
    assert document["slot"] == "0935ET"
    assert document["runner_slot_id"] == "0935"
    assert document["stream"] == "market_open"
    assert document["cron_id"] == "2ba19526"
    assert document["decision_time_et"] == "09:50"
    assert document["trading_day"] == DAY.isoformat()
    assert document["source"] == "robinhood"


def test_evaluation_time_reports_the_full_live_window_anchor():
    """MARKET_OPEN_SPEC.evaluation_delay_minutes=15 mutates the executor's
    actual decision anchor from 09:35 to 09:50 -- quote acceptance,
    completed-bar selection and sizing all evaluate at 09:50 -- so both
    persisted timestamps should expose that effective anchor."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data)
    assert document["decision_time_et"] == "09:50"
    assert document["evaluation_time_et"] == "09:50"


def test_quote_from_the_end_of_the_live_window_is_accepted():
    quote = {"price": 95.0, "as_of": datetime.combine(DAY, time(9, 49), tzinfo=ET).isoformat()}
    data = _market_data(TEST=_ticker_payload(price=99.0, previous_close=100.0, previous_low=97.0, quote=quote))
    row = _only(_run(data))
    assert row["action"] == Action.BUY.value
    assert row["target_price"] == pytest.approx(95.0)


def test_quote_just_before_nominal_fire_is_accepted_during_delayed_evaluation():
    quote = {"price": 95.0, "as_of": datetime.combine(DAY, time(9, 34, 50), tzinfo=ET).isoformat()}
    payload = _ticker_payload(price=99.0, previous_close=100.0, previous_low=97.0, quote=quote)
    row = _only(_run(_market_data(TEST=payload)))
    assert row["action"] == Action.BUY.value
    assert row["target_price"] == pytest.approx(95.0)


def test_boolean_quote_price_is_rejected():
    """Under the old Finance-primary policy a boolean quote was silently
    ignored (Finance quotes were never parsed at all, only the executor's own
    connector quote was retained). Robinhood is the primary source now, so the
    quote is parsed like any other market-data field, and ``bool`` being an
    ``int`` subclass (``float(True) == 1.0``) makes a corrupted JSON ``true``
    fail closed with a ValueError rather than silently coercing to a $1.00
    price (Codex review: "Reject boolean market prices")."""
    payload = _ticker_payload(price=99.0, previous_close=100.0, previous_low=97.0)
    payload["quote"] = {
        "price": True,
        "as_of": datetime.combine(DAY, time(9, 49), tzinfo=ET).isoformat(),
    }
    with pytest.raises(ValueError, match="boolean market-data value for quote.price"):
        _run(_market_data(TEST=payload))


def test_live_execution_enabled_is_reported_but_does_not_suppress_candidates():
    """config/market_open_dca.json's live_execution_enabled gates PLACEMENT
    inside MarketOpenDCAExecutor.run(), which this module never calls. A false
    value must be visible in the evidence without emptying the buy list -- an
    entry point that returned nothing while the flag is false would just be
    GATE5_DISABLED with extra steps."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data, _config(live_execution_enabled=False))
    assert document["live_execution_enabled"] is False
    assert document["buy_count"] == 1
    assert document["same_day_top_up_candidates"][0]["ticker"] == "TEST"


# --- the Gate 6.5 handoff ---------------------------------------------------


def test_unknown_prior_low_is_published_with_its_conservative_base_rule():
    """The live Robinhood adapter stamps low=0.0 on a quote-derived
    previous-close bar as an explicit "unknown" sentinel, which the executor
    turns into previous_low=None. Such a row is still a real, correctly sized buy
    -- with an unknown low the base can only be the conservative one, and no
    refreshed price can upgrade it -- so it is published rather than dropped, and
    marked so a consumer applies that rule deliberately instead of inferring it
    from a null (Codex review: "Handle unknown prior lows before publishing
    cands")."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    # low=0.0 is the sentinel: accepted by the parser for previous_close_bar only.
    data["sources"]["robinhood"]["TEST"]["previous_close_bar"]["low"] = 0.0
    document = _run(data)
    row = _only(document)
    assert row["action"] == Action.BUY.value
    assert row["previous_low"] is None
    assert row["selected_base_amount_usd"] == pytest.approx(1.0)
    (cand,) = document["cands"]
    assert cand["prior_low"] is None
    assert cand["unknown_prior_low"] is True
    assert document["withheld_contradictory_base_tickers"] == []
    assert [r["ticker"] for r in document["same_day_top_up_candidates"]] == ["TEST"]


def test_a_known_prior_low_is_not_marked_unknown():
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    (cand,) = _run(data)["cands"]
    assert cand["prior_low"] == pytest.approx(97.0)
    assert cand["unknown_prior_low"] is False


def test_top_up_rows_carry_the_shape_the_top_up_gate_requires():
    """scripts/same_day_top_up_gate.py scores a missing or non-positive
    current_price as MISSING_CURRENT_PRICE and zeroes the row, so publishing
    the ticker/amount alone would fail every candidate closed at Gate 6.5."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    rows = _run(data)["same_day_top_up_candidates"]
    assert rows == [{"ticker": "TEST", "desired_usd": pytest.approx(5.0), "current_price": pytest.approx(95.0)}]


def test_post_anchor_refresh_regrades_recovered_candidate():
    """A quote after 09:35 must be sized directly, not rejected by the signal anchor."""
    candidate = _run(
        _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=99.0))
    )["cands"][0]
    result = refresh_market_open_candidates(
        [candidate],
        {"TEST": {"price": 98.0, "as_of": "2026-08-05T09:41:00-04:00"}},
        _config(),
        trading_day=date(2026, 8, 5),
        placement_time=datetime.fromisoformat("2026-08-05T09:42:00-04:00"),
    )
    assert result["dropped"] == []
    assert result["cands"][0]["current_price"] == pytest.approx(98.0)
    assert result["cands"][0]["desired_usd"] < candidate["desired_usd"]


def test_post_anchor_refresh_drops_full_base_candidate_that_recovers_above_prior_low():
    candidate = _run(
        _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    )["cands"][0]
    assert candidate["selected_base_amount_usd"] == pytest.approx(2.0)

    result = refresh_market_open_candidates(
        [candidate],
        {"TEST": {"price": 98.0, "as_of": "2026-08-05T09:41:00-04:00"}},
        _config(),
        trading_day=date(2026, 8, 5),
        placement_time=datetime.fromisoformat("2026-08-05T09:42:00-04:00"),
    )

    assert result["cands"] == []
    assert result["dropped"] == [{"ticker": "TEST", "reason": "base_no_longer_supported"}]


@pytest.mark.parametrize("price", [True, False])
def test_post_anchor_refresh_rejects_boolean_prices(price):
    candidate = _run(
        _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=99.0))
    )["cands"][0]

    result = refresh_market_open_candidates(
        [candidate],
        {"TEST": {"price": price, "as_of": "2026-08-05T09:41:00-04:00"}},
        _config(),
        trading_day=date(2026, 8, 5),
        placement_time=datetime.fromisoformat("2026-08-05T09:42:00-04:00"),
    )

    assert result["cands"] == []
    assert result["dropped"] == [{"ticker": "TEST", "reason": "invalid_refresh_quote"}]


def test_post_anchor_refresh_never_increases_authorized_size():
    candidate = _run(
        _market_data(TEST=_ticker_payload(price=98.0, previous_close=100.0, previous_low=99.0))
    )["cands"][0]
    result = refresh_market_open_candidates(
        [candidate],
        {"TEST": {"price": 95.0, "as_of": "2026-08-05T09:41:00-04:00"}},
        _config(),
        trading_day=date(2026, 8, 5),
        placement_time=datetime.fromisoformat("2026-08-05T09:42:00-04:00"),
    )
    assert result["cands"] == []
    assert result["dropped"] == [{"ticker": "TEST", "reason": "refresh_would_increase_size"}]


@pytest.mark.parametrize(
    "as_of",
    ["2026-08-04T15:59:00-04:00", "2026-08-05T09:34:59-04:00", "2026-08-05T09:41:31-04:00"],
)
def test_post_anchor_refresh_rejects_quotes_outside_placement_freshness_window(as_of):
    candidate = _run(
        _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=99.0))
    )["cands"][0]
    result = refresh_market_open_candidates(
        [candidate],
        {"TEST": {"price": 98.0, "as_of": as_of}},
        _config(),
        trading_day=date(2026, 8, 5),
        placement_time=datetime.fromisoformat("2026-08-05T09:41:00-04:00"),
    )
    assert result["cands"] == []
    assert result["dropped"] == [{"ticker": "TEST", "reason": "stale_refresh_quote"}]


def test_refresh_cli_does_not_require_broker_account_environment(tmp_path):
    candidates = tmp_path / "candidates.json"
    quotes = tmp_path / "quotes.json"
    candidates.write_text("[]", encoding="utf-8")
    quotes.write_text("{}", encoding="utf-8")
    env = os.environ.copy()
    env.pop("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", None)

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.refresh_market_open_candidates",
            "--trading-day",
            "2026-08-05",
            "--placement-time",
            "2026-08-05T09:41:00-04:00",
            "--candidates",
            str(candidates),
            "--quotes",
            str(quotes),
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"cands": [], "dropped": []}


def test_top_up_rows_survive_a_planner_incompatible_calibrated_ladder():
    """Regression test for the divergence from the 1530ET module.

    dca_runner.plan_orders() validates a candidate's multiplier/drop against
    ONLY the global config/sizing_ladder.json, with no awareness of the
    per-ticker calibration the executor's own sizing does respect -- so a
    calibrated ticker's real, correct BUY is routinely "planner incompatible".
    The 1530ET module withholds such a row from its top-up list because its
    Gate 6 IS plan_orders(); the market-open prompt's Gate 6 is not. Coupling
    the two here would have emptied the one file Gate 6.5 actually consumes on
    ordinary days, handing the executor a zero-candidate list for a tape that
    genuinely qualified.
    """
    # Already-normalized shape (fraction thresholds, not min_drop_pct
    # percentages) -- this is what dca_config.load_per_ticker_sizing hands the
    # config object at runtime, so building it by hand here keeps the fixture
    # honest about what the executor really reads. A 4x multiplier at a 5%
    # drop is a real calibrated pairing the GLOBAL ladder does not allow,
    # which is exactly the planner-incompatible case under test.
    config = _config(
        per_ticker_sizing={
            "TEST": {
                "tiers": ((0.01, 4.0),),
                "base_amount_usd": 2.0,
                "base_amount_below_close_usd": 1.0,
            }
        },
    )
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data, config, check_planner_compatibility=True)
    assert document["buy_count"] == 1
    assert document["planner_incompatible_tickers"] == ["TEST"]
    assert document["planner_candidates"] == []
    # The point of the test: the real buy still reaches Gate 6.5.
    assert [row["ticker"] for row in document["same_day_top_up_candidates"]] == ["TEST"]


def test_manual_review_candidates_never_reach_the_auto_placement_outputs():
    """The buy filter keys on the executor's own execution_status, not on the
    assumption that this slot's trend exemption makes manual review impossible
    -- so a future profile change that re-enables trend validation cannot
    quietly promote a manual-review row into an auto-placed order."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(market_open_dca.MARKET_OPEN_PROFILES["live"], "validate_trend", True)
        document = _run(data)
    row = _only(document)
    assert row["execution_status"] == "manual_review_candidate"
    assert document["buy_count"] == 0
    assert document["same_day_top_up_candidates"] == []
    assert document["manual_review_tickers"] == ["TEST"]


# --- fail-closed input handling --------------------------------------------


def test_source_error_aborts_instead_of_emitting_a_quiet_tape():
    """This slot has ONE market-data source. A failed fetch leaves nothing to
    score, and a zero-candidate document would be indistinguishable from a
    genuinely quiet tape -- which Gate 5 ends silently. Aborting routes it to
    the prompt's "error or no output file: abort and alert" branch instead."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    with pytest.raises(ValueError, match="source_errors"):
        _run(data, source_errors=("robinhood",))


def test_an_unrecognized_source_error_marker_is_rejected():
    """An error marker that matches no known source marks nothing as failed, so
    a caller who correctly reported their failed fetch would still receive a
    zero-candidate document that reads as a quiet tape."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    with pytest.raises(ValueError, match="unsupported source"):
        _run(data, source_errors=("finance ",))


def test_unverified_planner_rows_are_withheld_and_named():
    """When the planner check cannot run, its rows are withheld rather than
    published as verified -- and every affected ticker is named, so an empty
    planner list is never mistaken for "nothing qualified"."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data, check_planner_compatibility=False)
    assert document["planner_config_mismatch"] is True
    assert document["planner_candidates"] == []
    assert document["planner_incompatible_tickers"] == ["TEST"]
    # ...and the buy still reaches Gate 6.5 regardless.
    assert [row["ticker"] for row in document["same_day_top_up_candidates"]] == ["TEST"]


def test_ticker_error_scores_no_data_with_the_provider_error_recorded():
    """A per-symbol fetch EXCEPTION must not read like a symbol this source
    simply does not cover: the provider raises for it exactly as a live
    provider would, so the executor records the provider-error warnings and
    returns NO_DATA."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data, ticker_errors=("TEST",))
    row = _only(document)
    assert row["action"] == Action.NO_DATA.value
    assert row["provider_error"] is True
    assert document["provider_error_tickers"] == ["TEST"]
    assert document["buy_count"] == 0
    assert any("provider error" in warning for warning in row["warnings"])


def test_already_covered_ticker_is_never_scored():
    """Gate 4's compatible-stand-down case: config/budgets.json's
    one_order_per_ticker_per_stream safeguard blocks a second buy for this
    ticker in this window at ANY size, so it must not appear as a row Gate 6.5
    could be asked to resize."""
    config = _config(tickers=("TEST", "OTHER"))
    data = _market_data(
        TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0),
        OTHER=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0),
    )
    document = _run(data, config, already_covered_tickers=("TEST",))
    assert [row["ticker"] for row in document["candidates"]] == ["OTHER"]
    assert document["already_covered_tickers"] == ["TEST"]
    assert [row["ticker"] for row in document["same_day_top_up_candidates"]] == ["OTHER"]


def test_unknown_ticker_in_the_payload_is_rejected():
    """A symbol this config has never heard of means a stale or mistyped
    payload; scoring the rest of it silently would hide that."""
    data = _market_data(
        TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0),
        NOPE=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0),
    )
    with pytest.raises(ValueError, match="active universe"):
        _run(data)


def test_unknown_ticker_error_entry_is_rejected():
    """Same reasoning for the error markers: an unknown ticker_errors entry
    marks nothing as errored, so the caller's intended fail-closed effect
    silently does not happen."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    with pytest.raises(ValueError, match="active universe"):
        _run(data, ticker_errors=("NOPE",))


def test_stale_previous_close_bar_is_rejected():
    """Inherited from _StaticMarketData: a previous_close_bar dated anything
    other than the immediately previous market session would be used as the
    BUY-grading and $1/$2 base-selection baseline as if it were fresh."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    data["sources"]["robinhood"]["TEST"]["previous_close_bar"] = _bar(
        datetime.combine(date(2026, 7, 20), time(16, 0), tzinfo=ET), 100.0, low=97.0
    )
    with pytest.raises(ValueError, match="immediately previous market session"):
        _run(data)


@pytest.mark.parametrize("bad_price", ["NaN", "Infinity", 0, -1])
def test_non_finite_or_non_positive_prices_are_rejected(bad_price):
    """Inherited from _StaticMarketData: a NaN close makes every comparison in
    the grading path evaluate False (silently downgrading to no-action), while
    a 0/negative placeholder from a failed fetch grades as a 100%+ drop and
    would size a maximum-multiplier buy."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    data["sources"]["robinhood"]["TEST"]["intraday_bars"][0]["close"] = (
        float(bad_price) if isinstance(bad_price, str) else bad_price
    )
    with pytest.raises(ValueError):
        _run(data)


def test_excluded_ticker_payload_is_tolerated():
    """One shared fetch across streams may legitimately include a ticker this
    slot excludes (BYDDY is not tradable for fractional buys); the executor
    never scores it, so rejecting the payload would force per-stream fetches
    for no safety gain."""
    config = _config(tickers=("TEST",), excluded_tickers=frozenset({"BYDDY"}))
    data = _market_data(
        TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0),
        BYDDY=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0),
    )
    document = _run(data, config)
    assert [row["ticker"] for row in document["candidates"]] == ["TEST"]


# --- the CLI ----------------------------------------------------------------


def _patch_config(monkeypatch, config: MarketOpenConfig | None = None) -> None:
    """Point the CLI at a hermetic one-ticker config.

    The config loader is a field on the module's frozen StreamSpec, not a module
    global the CLI looks up by name, so a hermetic test swaps the SPEC (which
    main() does resolve at call time) rather than patching a loader attribute
    the shared core never reads.
    """
    resolved = _config() if config is None else config
    monkeypatch.setattr(
        gmoc,
        "MARKET_OPEN_SPEC",
        dataclasses.replace(gmoc.MARKET_OPEN_SPEC, config_loader=lambda *_a, **_kw: resolved),
    )


def _cli(argv: list, monkeypatch, config: MarketOpenConfig | None = None) -> dict:
    _patch_config(monkeypatch, config)
    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        exit_code = gmoc.main(argv)
    assert exit_code == 0
    return json.loads(stdout.getvalue())


def test_cli_end_to_end(tmp_path, monkeypatch):
    """argument parsing -> generate_market_open_candidates -> one JSON document
    on stdout, which is exactly what the executor prompt's Gate 5 consumes."""
    path = tmp_path / "market_data_0935ET.json"
    payload = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    payload["sources"]["yahoo_finance_yfinance"] = {
        "TEST": _ticker_payload(price=90.0, previous_close=100.0, previous_low=97.0)
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    document = _cli(
        [
            "--trading-day", DAY.isoformat(),
            "--market-data", str(path),
            "--config-dir", str(REAL_CONFIG_DIR),
        ],
        monkeypatch,
    )
    assert document["slot"] == "0935ET"
    assert document["buy_count"] == 1
    assert document["same_day_top_up_candidates"][0]["ticker"] == "TEST"
    assert document["planner_config_mismatch"] is False
    assert document["source"] == "robinhood"
    assert document["validation_context_sources_present"] == ["yahoo_finance_yfinance"]
    # The materially different yfinance price was retained as context, not used
    # to replace the Robinhood-primary $5 candidate with a shallower signal.
    assert document["cands"][0]["current_price"] == pytest.approx(95.0)


def test_cli_ignores_primary_errors_for_already_covered_tickers(tmp_path, monkeypatch):
    payload = _market_data()
    payload["ticker_errors"]["robinhood"] = ["TEST"]
    path = tmp_path / "market_data_0935ET.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    document = _cli(
        [
            "--trading-day", DAY.isoformat(),
            "--market-data", str(path),
            "--config-dir", str(REAL_CONFIG_DIR),
            "--already-covered-tickers", "TEST",
        ],
        monkeypatch,
    )

    assert document["source"] == "robinhood"
    assert document["candidates"] == []
    assert document["already_covered_tickers"] == ["TEST"]


def test_market_open_spec_source_is_robinhood_primary():
    """0935ET's Gate 5 policy source. Confirmed and implemented 2026-09-09
    (docs/market_open_signal_entry.md, config/scheduled_tasks.json:
    market_data_source_policy.slot_overrides.0935ET): this slot is Robinhood-primary,
    the same deliberate exception 1030ET already carries, rather than the
    Finance-primary default every other slot uses. No Finance MCP connector is
    required to run this slot's Gate 5."""
    assert gmoc.MARKET_OPEN_SPEC.source is stream_candidates.SourceName.ROBINHOOD


def test_cli_accepts_a_robinhood_only_payload_with_no_finance_data_at_all(tmp_path, monkeypatch):
    """The exact failure two live 0935ET executor sessions hit on 2026-09-09: no Finance MCP
    connector was available in the session, only Robinhood. Per the Robinhood-primary policy
    above, a --market-data file carrying only 'sources.robinhood' (no 'sources.finance' key
    anywhere) must be accepted and scored, not rejected with "missing the required 'finance'
    object"."""
    payload = {
        "sources": {
            "robinhood": {
                "TEST": _ticker_payload(
                    price=95.0, previous_close=100.0, previous_low=97.0, step_minutes=5
                )
            }
        },
        "source_errors": [],
        "ticker_errors": {"robinhood": []},
    }
    path = tmp_path / "market_data_0935ET.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    document = _cli(
        ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR)],
        monkeypatch,
    )

    assert document["source"] == "robinhood"
    assert _only(document)["action"] == Action.BUY.value
    assert _only(document)["target_price"] == pytest.approx(95.0)


def test_cli_does_not_substitute_yfinance_for_a_robinhood_primary_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gmoc,
        "MARKET_OPEN_SPEC",
        dataclasses.replace(gmoc.MARKET_OPEN_SPEC, source=stream_candidates.SourceName.ROBINHOOD),
    )
    _patch_config(monkeypatch)
    payload = {
        "sources": {
            "robinhood": {},
            "yahoo_finance_yfinance": {
                "TEST": _ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0)
            },
        },
        "source_errors": ["robinhood"],
        "ticker_errors": {"robinhood": [], "yahoo_finance_yfinance": []},
    }
    path = tmp_path / "market_data_0935ET.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="robinhood.*source_errors"):
        gmoc.main(
            ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR)]
        )


def test_cli_refreshed_decision_time_recomputes_desired_size(tmp_path, monkeypatch):
    """A pre-placement rerun must not retain the amount sized at 09:35."""
    ticker = _ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0)
    ticker["intraday_bars"].append(_bar(datetime.combine(DAY, time(9, 45), tzinfo=ET), 95.0))
    ticker["quote"] = {
        "price": 98.0,
        "as_of": datetime.combine(DAY, time(9, 54), tzinfo=ET).isoformat(),
    }
    path = tmp_path / "market_data_0935ET.json"
    path.write_text(json.dumps(_market_data(TEST=ticker)), encoding="utf-8")

    initial = _cli(
        ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR)],
        monkeypatch,
    )
    refreshed = _cli(
        [
            "--trading-day", DAY.isoformat(),
            "--decision-time-et", "09:40:00",
            "--market-data", str(path),
            "--config-dir", str(REAL_CONFIG_DIR),
        ],
        monkeypatch,
    )

    assert initial["cands"][0]["current_price"] == 95.0
    assert refreshed["cands"][0]["current_price"] == 98.0
    assert initial["decision_time_et"] == "09:50"
    assert refreshed["decision_time_et"] == "09:55"
    assert refreshed["cands"][0]["desired_usd"] < initial["cands"][0]["desired_usd"]


def test_cli_rejects_a_second_market_data_source(tmp_path, monkeypatch):
    """The executor evaluates ONE provider. An unsupported extra source's
    payload would be silently discarded, leaving a caller believing a
    dual-source cross-check happened."""
    payload = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    payload["sources"]["alpaca"] = {}
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _patch_config(monkeypatch)
    with pytest.raises(ValueError, match="unsupported key"):
        gmoc.main(
            ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR)]
        )


def test_cli_rejects_a_missing_sources_key(tmp_path, monkeypatch):
    """A truncated/malformed file must not read as "the fetch returned
    nothing" -- that would be a schema-valid, zero-candidate quiet tape."""
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({"source_errors": []}), encoding="utf-8")
    _patch_config(monkeypatch)
    with pytest.raises(ValueError, match="'sources' object"):
        gmoc.main(
            ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR)]
        )


def test_cli_rejects_an_omitted_robinhood_source(tmp_path, monkeypatch):
    """An empty `sources` object is a failed fetch spelled the ambiguous way;
    a real one is spelled in source_errors, which aborts loudly."""
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps({"sources": {}}), encoding="utf-8")
    _patch_config(monkeypatch)
    with pytest.raises(ValueError, match="missing the required 'robinhood'"):
        gmoc.main(
            ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR)]
        )


def test_cli_rejects_duplicate_keys_in_the_market_data_file(tmp_path, monkeypatch):
    """Plain json.loads keeps only the LAST of two entries for the same ticker,
    discarding a conflicting snapshot before validation ever sees it."""
    path = tmp_path / "market_data.json"
    path.write_text(
        '{"sources": {"robinhood": {"TEST": {}, "TEST": {}}}}',
        encoding="utf-8",
    )
    _patch_config(monkeypatch)
    with pytest.raises(ValueError):
        gmoc.main(
            ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR)]
        )


def test_cli_rejects_null_ticker_errors(tmp_path, monkeypatch):
    """An explicit null (a failed serializer dropping the per-symbol error map)
    is malformed evidence, not "no per-ticker errors"."""
    payload = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    payload["ticker_errors"] = None
    path = tmp_path / "market_data.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    _patch_config(monkeypatch)
    with pytest.raises(ValueError, match="got null"):
        gmoc.main(
            ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(REAL_CONFIG_DIR)]
        )


def test_cli_aborts_on_a_missing_config_dir(tmp_path, monkeypatch):
    """load_market_open_config only WARNS and falls back to hardcoded defaults
    for a missing directory -- which would score against the built-in universe
    and caps while the caller believes the checked-in policy was used."""
    path = tmp_path / "market_data.json"
    path.write_text(
        json.dumps(_market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not exist"):
        gmoc.main(
            [
                "--trading-day", DAY.isoformat(),
                "--market-data", str(path),
                "--config-dir", str(tmp_path / "nope"),
            ]
        )


def test_cli_aborts_on_a_config_dir_missing_the_stream_config(tmp_path, monkeypatch):
    """config/market_open_dca.json specifically: without it,
    load_market_open_config silently returns MarketOpenConfig() defaults, so
    this slot's own caps and policy would be replaced by hardcoded ones."""
    incomplete = tmp_path / "config"
    incomplete.mkdir()
    for name in gmoc._REQUIRED_CONFIG_FILENAMES:
        if name != "market_open_dca.json":
            (incomplete / name).write_text(
                (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
            )
    path = tmp_path / "market_data.json"
    path.write_text(
        json.dumps(_market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="market_open_dca.json"):
        gmoc.main(
            ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(incomplete)]
        )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"intraday_bars": []},
        {"previous_close_bar": _previous_close_bar(100.0, 97.0), "intraday_bars": []},
        {"intraday_bars": _intraday_bars(95.0)},
    ],
    ids=["empty", "no-bars-no-close", "empty-bars", "no-previous-close"],
)
def test_incomplete_payload_for_a_present_ticker_is_rejected(payload):
    """A truncated fetch must not collapse into the quiet-tape branch.

    _StaticMarketData turns a missing previous_close_bar or bar list into
    None/[], and the executor then returns an ordinary NO_DATA with
    provider_error=false -- indistinguishable from a symbol this source simply
    does not cover. On a single-source slot that means a broken response produces
    exactly the zero-candidate document Gate 5 ends silently on (Codex review:
    "Reject incomplete payloads for attempted tickers").
    """
    data = {"sources": {"robinhood": {"TEST": payload}}}
    with pytest.raises(ValueError, match="incomplete payload"):
        _run(data)


def test_payload_bars_finer_than_the_configured_cadence_are_rejected():
    """MARKET_OPEN_SPEC leaves ``source_interval_minutes`` unset, so it resolves
    to ``config.interval_minutes`` -- 5 minutes, this slot's native Robinhood
    cadence. Neither ``StreamMarketData`` nor the executor's own bar-window
    checks validate the SPACING between bars, only that enough of them fall
    inside the window -- so a caller who instead fetches finer-grained bars
    could satisfy those checks on a slice of the window far narrower than they
    were calibrated for, with nothing downstream noticing (Codex review, PR
    #917 discussion on ``scripts/generate_midday_candidates.py``: "Reject
    five-minute bars under the Finance cadence"). This is the runtime guard
    that closes that gap.
    """
    data = _market_data(
        TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0, step_minutes=1)
    )
    with pytest.raises(
        ValueError, match="closer together than this stream's configured 5-minute cadence"
    ):
        _run(data)


def test_absent_ticker_is_still_the_supported_not_covered_shape():
    """The counterpart to the test above: omitting the ticker entirely stays the
    documented way to say "this source does not cover it" and must keep scoring
    the rest of the universe."""
    config = _config(tickers=("TEST", "GHOST"))
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data, config)
    assert document["buy_count"] == 1
    assert document["no_data_tickers"] == ["GHOST"]


def _stream_config_cli(tmp_path, stream_config_text: str):
    """Run the CLI in a FRESH process with $DCA_CONFIG_DIR pointed at a tmp
    config directory, so dca_runner imports from the same directory --config-dir
    names.

    In-process invocation cannot reach these checks: any tmp directory differs
    from the dca_runner.CONFIG_DIR this test session already resolved, so the
    config-dir mismatch guard (tested separately below) fires first. A subprocess
    is also how the executor prompt actually invokes this command.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in gmoc._REQUIRED_CONFIG_FILENAMES:
        (config_dir / name).write_text(
            (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    (config_dir / "market_open_dca.json").write_text(stream_config_text, encoding="utf-8")
    # Narrow the universe to the one ticker these fixtures carry. budgets.json,
    # sizing_ladder.json and market_calendar.json stay VERBATIM from the real
    # config: dca_runner's own stricter parsers cross-check them against each
    # other in whole cents, so hand-building them would test the fixture rather
    # than the code. tickers.json/per_ticker_sizing.json are safe to scope down --
    # dca_runner reads neither at import time -- and _require_populated_config
    # requires the calibration to cover every active ticker.
    (config_dir / "tickers.json").write_text(json.dumps({"active": ["TEST"]}), encoding="utf-8")
    (config_dir / "per_ticker_sizing.json").write_text(
        json.dumps(
            {
                "status": "active",
                "tickers": {"TEST": {"tiers": [{"min_drop_pct": 1.0, "multiplier": 1.0}]}},
            }
        ),
        encoding="utf-8",
    )
    path = tmp_path / "market_data.json"
    path.write_text(
        json.dumps(
            _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
        ),
        encoding="utf-8",
    )
    return subprocess.run(
        [
            sys.executable, "-m", "scripts.generate_market_open_candidates",
            "--trading-day", DAY.isoformat(),
            "--market-data", str(path),
            "--config-dir", str(config_dir),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "DCA_CONFIG_DIR": str(config_dir)},
        capture_output=True,
        text=True,
    )


def test_cli_rejects_a_malformed_stream_config(tmp_path):
    """load_market_open_config treats unparseable exactly like absent -- a logged
    warning and the dataclass DEFAULTS -- so without this check a damaged live
    policy silently produced a full, exit-0 candidate document sized by the
    hardcoded fallback (Codex review: "Reject malformed market-open
    configuration"). _require_populated_config cannot cover it: it validates the
    SHARED file list it was written for and never sees the stream-specific
    filename this module appends."""
    result = _stream_config_cli(tmp_path, "{not json")
    assert result.returncode != 0
    assert "malformed, or has a duplicate JSON key" in result.stderr
    assert result.stdout.strip() == ""


def test_cli_rejects_a_duplicate_key_in_the_stream_config(tmp_path):
    """Plain json.loads keeps only the LAST occurrence, so two
    live_execution_enabled entries would discard one with no trace."""
    result = _stream_config_cli(
        tmp_path, '{"live_execution_enabled": false, "live_execution_enabled": true}'
    )
    assert result.returncode != 0
    assert "duplicate JSON key" in result.stderr


@pytest.mark.parametrize(
    "stream_config_text",
    ['{}', '{"unrelated": 1}', '{"stream": "market_open"}'],
    ids=["empty", "unrecognized-keys", "identity-only"],
)
def test_cli_rejects_a_hollow_stream_config(tmp_path, stream_config_text):
    """Parsing is not applying. A syntactically valid but hollow config loads as
    this stream's hardcoded dataclass DEFAULTS without complaint, so Gate 5 would
    emit real candidates sized by fallback policy while appearing to have used
    the checked-in one (Codex review: "Reject empty stream configuration
    objects")."""
    result = _stream_config_cli(tmp_path, stream_config_text)
    assert result.returncode != 0
    assert "missing required policy key" in result.stderr
    assert result.stdout.strip() == ""


def test_cli_rejects_another_streams_config(tmp_path):
    """The same failure wearing a different hat: a sibling slot's config loads
    cleanly and would size this slot from the wrong ladder and caps."""
    midday = (REAL_CONFIG_DIR / "midday_dca.json").read_text(encoding="utf-8")
    result = _stream_config_cli(tmp_path, midday)
    assert result.returncode != 0
    assert "declares stream" in result.stderr


def test_cli_accepts_a_valid_stream_config_in_a_fresh_process(tmp_path):
    """The control for the two tests above: the same subprocess path with a
    VALID stream config must still produce a document, so those failures are
    attributable to the config content and not to the harness."""
    result = _stream_config_cli(
        tmp_path, (REAL_CONFIG_DIR / "market_open_dca.json").read_text(encoding="utf-8")
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["slot"] == "0935ET"


def test_cli_fails_closed_when_dca_runner_resolved_a_different_config_dir(tmp_path, monkeypatch):
    """dca_runner reads its config once, at import, with no per-call override.
    A --config-dir that differs would have this run score from one directory
    while validating the market calendar, ladder and calibration against
    another -- and unlike a planner mismatch, a calendar disagreement is neither
    withheld nor reported, so the resulting rows still look actionable (Codex
    review: "Honor a new config directory after dca_runner was imported").
    """
    other = tmp_path / "config"
    other.mkdir()
    for name in gmoc._REQUIRED_CONFIG_FILENAMES:
        (other / name).write_text(
            (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    path = tmp_path / "market_data.json"
    path.write_text(
        json.dumps(_market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))),
        encoding="utf-8",
    )
    _patch_config(monkeypatch)
    with pytest.raises(ValueError, match="does not match the directory dca_runner already resolved"):
        gmoc.main(
            ["--trading-day", DAY.isoformat(), "--market-data", str(path), "--config-dir", str(other)]
        )


def test_public_helper_rejects_a_mismatched_config_dir(tmp_path):
    """The CLI guard is not the only entry path. A direct caller scoring from one
    directory while dca_runner holds another can have a stale previous-close bar
    accepted (or a correct one rejected) against the wrong holiday calendar, and
    that verdict is neither withheld nor reported the way a planner mismatch is
    (Codex review: "Validate the config directory in direct helper calls")."""
    other = tmp_path / "config"
    other.mkdir()
    for name in gmoc._REQUIRED_CONFIG_FILENAMES:
        (other / name).write_text(
            (REAL_CONFIG_DIR / name).read_text(encoding="utf-8"), encoding="utf-8"
        )
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    with pytest.raises(ValueError, match="does not match the directory dca_runner already resolved"):
        gmoc.generate_market_open_candidates(
            trading_day=DAY,
            config=_config(),
            market_data=data["sources"]["robinhood"],
            config_dir=other,
            check_planner_compatibility=False,
        )


def test_required_config_filenames_include_the_stream_config():
    """The 1530ET module's shared list plus this slot's own stream config --
    asserted directly so a future edit cannot drop it and reintroduce the
    silent hardcoded-defaults fallback."""
    assert "market_open_dca.json" in gmoc._REQUIRED_CONFIG_FILENAMES
    assert "tickers.json" in gmoc._REQUIRED_CONFIG_FILENAMES
    assert "market_calendar.json" in gmoc._REQUIRED_CONFIG_FILENAMES


def test_trend_window_coverage_never_withholds_on_this_trend_exempt_slot():
    """The sibling slots withhold an auto-buy row whose bars do not reach back
    to the start of the 60-minute trend window
    (``withheld_incomplete_trend_window_tickers``). At 09:35 that window would
    start at 08:35, pre-market, so a "full" window is impossible by
    construction -- applying the rule here would withhold every candidate on
    every fire. It is keyed off the profile's own validate_trend flag, so this
    slot is exempt from it exactly as it is exempt from the trend rule itself."""
    data = _market_data(TEST=_ticker_payload(price=95.0, previous_close=100.0, previous_low=97.0))
    document = _run(data)
    assert document["trend_validation_exempt"] is True
    assert document["withheld_incomplete_trend_window_tickers"] == []
    assert [c["ticker"] for c in document["cands"]] == ["TEST"]


def test_build_document_rejects_a_deployment_figure_this_slot_does_not_accept():
    """build_document is exported, so a direct caller could hand this spec a
    deployment figure and silently get the kill switch applied: reproduced
    turning a 5x/$10 market-open candidate into 1x/$2 while the document reported
    kill_switch_evaluated: true. That contradicts the one guarantee this entry
    point makes -- that it sizes exactly like the executor it mirrors, which
    never sees the figure (Codex review: "Reject deployment figures for
    unsupported streams"). Raising beats ignoring: a caller passing the argument
    believes it matters."""
    assert gmoc.MARKET_OPEN_SPEC.requires_deployed_last_7d_usd is False
    data = _market_data(TEST=_ticker_payload(price=88.0, previous_close=100.0, previous_low=97.0))
    with pytest.raises(ValueError, match="does not accept deployed_last_7d_usd"):
        stream_candidates.build_document(
            gmoc.MARKET_OPEN_SPEC,
            trading_day=DAY,
            config=_config(),
            market_data=data["sources"]["robinhood"],
            deployed_last_7d_usd=100000.0,
            config_dir=REAL_CONFIG_DIR,
            check_planner_compatibility=False,
        )


def test_the_deepest_tier_is_unchanged_when_no_figure_is_supplied():
    """The control: the same candidate at the same drop must still size at the
    full ladder multiplier, so the rejection above is a guard and not a cap."""
    data = _market_data(TEST=_ticker_payload(price=88.0, previous_close=100.0, previous_low=97.0))
    document = _run(data)
    row = _only(document) if "_only" in globals() else document["candidates"][0]
    assert row["buy_multiplier"] == pytest.approx(5.0)
    assert row["suggested_amount_usd"] == pytest.approx(10.0)
    assert document["kill_switch_evaluated"] is False
