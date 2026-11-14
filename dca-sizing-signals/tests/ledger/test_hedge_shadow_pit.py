"""Point-in-time pricing for the loss-hedge shadow log (docs/adr-goals-and-loss-hedge-shadow.md, tests 8 and 11).

Written FIRST: fails until ``market_data_ingest/ledger/hedge_shadow.py`` exists.

Both tests use REAL read-only Robinhood captures, never invented quotes:

* ``fixtures/rh_option_chains_2026-11-02.json`` (already in the repo for the CSP shadow log). Its
  QQQ put quotes were stamped 2026-11-02T21:14:59Z, i.e. 4:14:59 PM ET, because QQQ options
  trade until 4:15 PM. A 4:00 PM ET decision therefore must NOT see them.
* ``fixtures/rh_crypto_quotes_2026-11-13.json``: two captures of BTC-USD and ETH-USD,
  at 10:13:28 and 10:17:52 ET on 2026-11-13.

The rule under test: a quote is usable only if its own ``updated_at`` is at or before the
decision time. A later quote produces no price (None), never an estimate.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

hedge_shadow = pytest.importorskip(
    "market_data_ingest.ledger.hedge_shadow",
    reason="loss-hedge shadow implementation is deferred pending ADR approval",
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ET = ZoneInfo("America/New_York")

OPTIONS = json.loads((FIXTURES / "rh_option_chains_2026-11-02.json").read_text())
CRYPTO = json.loads((FIXTURES / "rh_crypto_quotes_2026-11-13.json").read_text())

# QQQ 2026-12-04 640 put, from the fixture's instruments.QQQ list.
QQQ_640_PUT = "03aac4c2-e2b8-4a48-b2c9-b12e67eb099b"


def crypto_quotes() -> list[dict]:
    return [quote for capture in CRYPTO["captures"] for quote in capture["results"]]


def test_fixtures_hold_what_these_tests_assume():
    # Guard the guards: if a fixture is ever re-captured, these facts must be re-checked.
    qqq = next(q["quote"] for q in OPTIONS["option_quotes"] if q["quote"]["instrument_id"] == QQQ_640_PUT)
    assert qqq["updated_at"] == "2026-11-02T21:14:59.920988052Z"
    assert qqq["ask_price"] == "8.590000"
    btc = [q for q in crypto_quotes() if q["symbol"] == "BTCUSD"]
    assert [q["updated_at"] for q in btc] == ["2026-11-13T10:13:28.804-05:00", "2026-11-13T10:17:52.2-05:00"]


# --------------------------------------------------------------------------------------------
# Decision time: 4:00 PM ET on the trading day, timezone-aware.
# --------------------------------------------------------------------------------------------

def test_decision_time_is_4pm_eastern_and_aware():
    assert hedge_shadow.decision_time(date(2026, 11, 2)) == datetime(2026, 11, 2, 16, tzinfo=ET)
    # Across the DST change: Nov 2, 2026 is EST (UTC-5), so 4 PM ET is 21:00 UTC.
    assert hedge_shadow.decision_time(date(2026, 11, 2)).astimezone(timezone.utc).hour == 21
    assert hedge_shadow.decision_time(date(2026, 7, 1)).astimezone(timezone.utc).hour == 20


# --------------------------------------------------------------------------------------------
# Test 8: options are priced only from quotes stamped at or before the decision time.
# --------------------------------------------------------------------------------------------

def test_8_a_quote_stamped_after_4pm_is_invisible_to_the_4pm_decision():
    decision = hedge_shadow.decision_time(date(2026, 11, 2))
    assert hedge_shadow.option_quote_at(OPTIONS["option_quotes"], QQQ_640_PUT, decision) is None


def test_8_the_same_quote_is_usable_once_its_timestamp_has_passed():
    decision = datetime(2026, 11, 2, 16, 20, tzinfo=ET)
    quote = hedge_shadow.option_quote_at(OPTIONS["option_quotes"], QQQ_640_PUT, decision)
    assert quote is not None and quote["instrument_id"] == QQQ_640_PUT


def test_8_a_nanosecond_stamp_rounds_up_never_down():
    # The stamp is 21:14:59.920988052Z. Python datetimes stop at microseconds, so the parser must
    # round UP to .920989: truncating to .920988 would let a .920988 decision see a quote that
    # was published 52 ns later -- a tiny lookahead, but a lookahead. Inclusive at .920989.
    truncated = datetime(2026, 11, 2, 21, 14, 59, 920_988, tzinfo=timezone.utc)
    rounded_up = datetime(2026, 11, 2, 21, 14, 59, 920_989, tzinfo=timezone.utc)
    assert hedge_shadow.option_quote_at(OPTIONS["option_quotes"], QQQ_640_PUT, truncated) is None
    assert hedge_shadow.option_quote_at(OPTIONS["option_quotes"], QQQ_640_PUT, rounded_up) is not None


def test_8_an_unknown_instrument_has_no_quote():
    decision = datetime(2026, 11, 3, 16, tzinfo=ET)
    assert hedge_shadow.option_quote_at(OPTIONS["option_quotes"], "not-an-instrument", decision) is None


def test_8_buying_protection_is_costed_at_the_ask_times_the_multiplier():
    decision = datetime(2026, 11, 2, 16, 20, tzinfo=ET)
    quote = hedge_shadow.option_quote_at(OPTIONS["option_quotes"], QQQ_640_PUT, decision)
    # ask 8.59 x 100 shares per contract = 859.00 (entry at the ask: we are the buyer).
    assert hedge_shadow.put_entry_cost(quote, contracts=1) == Decimal("859.00")


def test_8_a_naive_decision_time_is_refused():
    with pytest.raises(ValueError):
        hedge_shadow.option_quote_at(OPTIONS["option_quotes"], QQQ_640_PUT, datetime(2026, 11, 3, 16))


# --------------------------------------------------------------------------------------------
# Test 11: crypto trades 24/7; the shadow price is the LAST quote at or before the decision.
# --------------------------------------------------------------------------------------------

def test_11_the_4pm_decision_uses_the_last_quote_of_the_day_so_far():
    decision = hedge_shadow.decision_time(date(2026, 11, 13))
    # Two BTC quotes precede 4 PM; the later one (10:17:52, mark 68845.28683803) wins.
    assert hedge_shadow.crypto_price_at(crypto_quotes(), "BTC", decision) == Decimal("68845.28683803")
    assert hedge_shadow.crypto_price_at(crypto_quotes(), "ETH", decision) == Decimal("1939.5064956")


def test_11_a_decision_between_the_captures_uses_the_earlier_one():
    decision = datetime(2026, 11, 13, 10, 15, tzinfo=ET)
    assert hedge_shadow.crypto_price_at(crypto_quotes(), "BTC", decision) == Decimal("68724.64601649")


def test_11_a_decision_before_every_capture_has_no_price():
    assert hedge_shadow.crypto_price_at(crypto_quotes(), "BTC", datetime(2026, 11, 13, 10, 0, tzinfo=ET)) is None
    assert hedge_shadow.crypto_price_at(crypto_quotes(), "BTC", hedge_shadow.decision_time(date(2026, 11, 12))) \
        is None


def test_11_input_order_does_not_change_the_answer():
    decision = hedge_shadow.decision_time(date(2026, 11, 13))
    reversed_quotes = list(reversed(crypto_quotes()))
    assert hedge_shadow.crypto_price_at(reversed_quotes, "BTC", decision) == Decimal("68845.28683803")


def test_11_an_asset_outside_the_allowlist_has_no_price():
    decision = hedge_shadow.decision_time(date(2026, 11, 13))
    assert hedge_shadow.crypto_price_at(crypto_quotes(), "DOGE", decision) is None


def test_11_a_naive_decision_time_is_refused():
    with pytest.raises(ValueError):
        hedge_shadow.crypto_price_at(crypto_quotes(), "BTC", datetime(2026, 11, 13, 16))
