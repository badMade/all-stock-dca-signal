"""ADR docs/adr-csp-shadow-log.md, Amendment 1 (operator approved 2026-11-02).

1. The selector's window is 18 to 46 days (was 25 to 45), so monthlies fall inside it.
2. A separate CAP-WAIVED track: the same filters with no collateral or sector caps, kept in
   its own tables and read separately, so it can never mix into the real track or block a
   tax-loss sale.

The real-data cases use the read-only Robinhood fixture captured after the 2026-11-02 close
(fixtures/rh_option_chains_2026-11-02.json, via csp_capture). Everything else reuses the
synthetic rows in csp_rows.py, with the arithmetic beside each assertion.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from market_data_ingest.ledger.accounts import account_alias
from market_data_ingest.ledger.csp_capture import capture_from_bundle, load_bundle
from market_data_ingest.ledger.csp_shadow import (
    CAP_WAIVED_CONFIG,
    CAP_WAIVED_TRACK,
    DEFAULT_CONFIG,
    REAL_TRACK,
    AccountContext,
    entry_from_suggestion,
    open_short_puts,
    record_settlements,
    record_shadow_entries,
    select_candidates,
    settle,
    shadow_entries,
    shadow_settlements,
    tax_loss_blocked_tickers,
)
from market_data_ingest.ledger.errors import LedgerValidationError

from .conftest import TEST_SALT
from .csp_rows import AS_OF, chain_row, select

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rh_option_chains_2026-11-02.json"
ALIAS = account_alias("csp-amendment-test-account", salt=TEST_SALT)


# --- 1. the window ----------------------------------------------------------------------------


def test_default_window_is_18_to_46_days():
    assert (DEFAULT_CONFIG.min_dte, DEFAULT_CONFIG.max_dte) == (18, 46)


def test_every_other_default_is_unchanged():
    # The amendment touches the window only; the ADR's other proposed defaults stand.
    assert (DEFAULT_CONFIG.min_delta, DEFAULT_CONFIG.max_delta) == (-0.30, -0.15)
    assert DEFAULT_CONFIG.max_spread_pct_of_mid == 0.10
    assert (DEFAULT_CONFIG.max_collateral_pct_per_contract, DEFAULT_CONFIG.max_total_collateral_pct,
            DEFAULT_CONFIG.max_sector_pct) == (0.10, 0.25, 0.30)


# --- 2. the cap-waived config -----------------------------------------------------------------


def test_cap_waived_config_differs_only_in_the_three_caps():
    assert math.isinf(CAP_WAIVED_CONFIG.max_collateral_pct_per_contract)
    assert math.isinf(CAP_WAIVED_CONFIG.max_total_collateral_pct)
    assert math.isinf(CAP_WAIVED_CONFIG.max_sector_pct)
    same = {f for f in DEFAULT_CONFIG.__dataclass_fields__
            if f not in {"max_collateral_pct_per_contract", "max_total_collateral_pct", "max_sector_pct"}}
    assert all(getattr(CAP_WAIVED_CONFIG, f) == getattr(DEFAULT_CONFIG, f) for f in same)
    # A1 (proposed default): still one open shadow put per ticker.
    assert CAP_WAIVED_CONFIG.one_open_per_ticker is True


def _real(value: float):
    result = capture_from_bundle(load_bundle(FIXTURE))
    acct = AccountContext(account_alias=ALIAS, account_value=value,
                          sector_by_ticker={"GLW": "tech", "KR": "staples", "QQQ": "etf"},
                          sector_value_by_sector={"tech": 0.0, "staples": 0.0, "etf": 0.0})
    ladder = {t: q["price"] for t, q in result.underlying_quotes.items()}

    def run(config, paused=()):
        return select_candidates(result.chain_rows, result.underlying_quotes, ladder, ["GLW", "KR", "QQQ"],
                                 list(paused), result.as_of, config, account=acct)
    return run


def test_real_fixture_small_account_real_track_selects_nothing():
    assert _real(50_000.0)(DEFAULT_CONFIG).suggestions == ()


def test_real_fixture_small_account_cap_waived_track_selects_qqq_650():
    # Same arithmetic as test_csp_capture: QQQ 650 at the 10.28 bid, 18.0% annualised, beats
    # the 640. GLW and KR still fail the 10% spread cap, which the waiver does not touch.
    picks = _real(50_000.0)(CAP_WAIVED_CONFIG)
    assert [(s.ticker, s.strike, s.bid) for s in picks.suggestions] == [("QQQ", 650.0, 10.28)]
    assert picks.suggestions[0].collateral == 65_000.0  # more than the whole account: not tradable
    assert {t for t, r in picks.dropped if r == "no_eligible_contract"} == {"GLW", "KR"}


def test_cap_waived_track_still_honours_the_tax_loss_pause():
    picks = _real(50_000.0)(CAP_WAIVED_CONFIG, paused=["QQQ"])
    assert picks.suggestions == ()
    assert ("QQQ", "tax_loss_pause") in picks.dropped


def test_cap_waived_track_still_fails_closed_without_a_pause_list():
    result = select([chain_row()], config=CAP_WAIVED_CONFIG, paused=None)
    assert result.suggestions == () and result.dropped == (("*", "no_pause_list"),)


# --- 2. its own tables ------------------------------------------------------------------------


def _clock(moment):
    return lambda: moment


def _one_synthetic_suggestion():
    (s,) = select([chain_row()], config=CAP_WAIVED_CONFIG).suggestions
    return s


def test_tracks_are_named():
    assert (REAL_TRACK, CAP_WAIVED_TRACK) == ("real", "cap_waived")


def test_cap_waived_entries_are_stored_and_read_separately():
    con = duckdb.connect()
    s = _one_synthetic_suggestion()
    assert record_shadow_entries(con, [s], run_id="r1", as_of=AS_OF, track=CAP_WAIVED_TRACK,
                                 clock=_clock(AS_OF)) == 1
    assert shadow_entries(con) == []  # the real track is untouched
    (row,) = shadow_entries(con, track=CAP_WAIVED_TRACK)
    assert row["ticker"] == s.ticker and row["entry_price"] == s.bid


def test_the_same_contract_can_sit_in_both_tracks():
    # Identical entry ids in both tracks must not collide: separate tables, not one table with
    # a flag that ON CONFLICT DO NOTHING could silently swallow.
    con = duckdb.connect()
    s = _one_synthetic_suggestion()
    record_shadow_entries(con, [s], run_id="r1", as_of=AS_OF, clock=_clock(AS_OF))
    record_shadow_entries(con, [s], run_id="r1", as_of=AS_OF, track=CAP_WAIVED_TRACK, clock=_clock(AS_OF))
    assert len(shadow_entries(con)) == 1
    assert len(shadow_entries(con, track=CAP_WAIVED_TRACK)) == 1


def test_cap_waived_open_puts_do_not_block_tax_loss_eligibility():
    con = duckdb.connect()
    s = _one_synthetic_suggestion()
    record_shadow_entries(con, [s], run_id="r1", as_of=AS_OF, track=CAP_WAIVED_TRACK, clock=_clock(AS_OF))
    assert open_short_puts(con, AS_OF) == []
    assert tax_loss_blocked_tickers(con, AS_OF) == set()


def test_cap_waived_settlements_are_stored_and_read_separately():
    con = duckdb.connect()
    s = _one_synthetic_suggestion()
    record_shadow_entries(con, [s], run_id="r1", as_of=AS_OF, track=CAP_WAIVED_TRACK, clock=_clock(AS_OF))
    entry = entry_from_suggestion(s)
    # Expiry passed with the stock above the strike: expired out of the money, premium kept.
    after = datetime.combine(entry.expiration_date + timedelta(days=1), datetime.min.time(), timezone.utc)
    prices = [{"ticker": entry.ticker, "quote_date": entry.expiration_date, "close": entry.strike + 5.0}]
    settled = settle([entry], prices, as_of=after)
    assert settled[0].status == "expired_otm"
    record_settlements(con, settled, run_id="r2", as_of=after, track=CAP_WAIVED_TRACK, clock=_clock(after))
    assert shadow_settlements(con) == []
    assert [r["status"] for r in shadow_settlements(con, track=CAP_WAIVED_TRACK)] == ["expired_otm"]


@pytest.mark.parametrize("bad", ["", "Real", "capwaived", None, 1])
def test_an_unknown_track_is_refused(bad):
    con = duckdb.connect()
    with pytest.raises(LedgerValidationError):
        shadow_entries(con, track=bad)
    with pytest.raises(LedgerValidationError):
        record_shadow_entries(con, [], run_id="r1", as_of=AS_OF, track=bad)
