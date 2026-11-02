"""Daily option-chain capture for the cash-secured put shadow log (ADR docs/adr-csp-shadow-log.md).

The capture is the missing half of PR #2105: ``csp_shadow.select_candidates`` takes put quotes
in a clean row shape, and something has to turn the READ-ONLY Robinhood responses into those
rows and keep a point-in-time copy of what was seen. The scheduled task (4:20 PM ET, after the
4:15 PM option close) fetches with the MCP tools, saves the responses as one bundle file, and
hands that file to this module. This module never fetches and never trades.

Every expected value below comes from the REAL responses in
fixtures/rh_option_chains_2026-11-02.json (captured after the 2026-11-02 close; see its
``_about``). Nothing here is invented market data; the only synthetic inputs are the account
context (an alias built at runtime) and the deliberately broken copies made to prove each
fail-closed path.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from market_data_ingest.ledger.accounts import account_alias
from market_data_ingest.ledger.csp_capture import (
    CaptureBundleError,
    CaptureConfig,
    capture_from_bundle,
    captured_rows,
    ensure_capture_schema,
    expirations_in_window,
    load_bundle,
    normalize_chain_rows,
    quote_plan,
    record_capture,
    underlying_quote,
)
from market_data_ingest.ledger.csp_shadow import AccountContext, select_candidates

from .conftest import TEST_SALT
from .test_csp_shadow_no_orders import _BROKER_TOOL, _MCP

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "rh_option_chains_2026-11-02.json"
MODULE = Path(__file__).resolve().parents[2] / "market_data_ingest" / "ledger" / "csp_capture.py"
SESSION = date(2026, 11, 2)
CAPTURED_AT = datetime(2026, 11, 2, 23, 10, tzinfo=timezone.utc)  # 6:10 PM ET, the fixture's capture
ALIAS = account_alias("csp-capture-test-account", salt=TEST_SALT)


@pytest.fixture()
def bundle() -> dict:
    return load_bundle(FIXTURE)


def _by_id(rows):
    return {r["option_id"]: r for r in rows}


# --- loading the bundle ---------------------------------------------------------------------


def test_bundle_loads_the_real_fixture(bundle):
    assert set(bundle["chains"]) == {"GLW", "KR", "QQQ"}
    assert len(bundle["option_quotes"]) == 24
    assert len(bundle["equity_quotes"]) == 3


@pytest.mark.parametrize("text", ["[]", "null", '"x"', '{"chains": {}, "chains": {}}'])
def test_bundle_rejects_non_objects_and_duplicate_keys(tmp_path, text):
    path = tmp_path / "bundle.json"
    path.write_text(text)
    with pytest.raises(CaptureBundleError):
        load_bundle(path)


@pytest.mark.parametrize("missing", ["captured_at", "chains", "instruments", "option_quotes", "equity_quotes"])
def test_bundle_rejects_a_missing_section(tmp_path, missing):
    raw = json.loads(FIXTURE.read_text())
    del raw[missing]
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(CaptureBundleError):
        load_bundle(path)


def test_bundle_rejects_a_naive_capture_time(tmp_path):
    raw = json.loads(FIXTURE.read_text())
    raw["captured_at"] = "2026-11-02T23:10:00"  # no offset: the session cannot be known
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(CaptureBundleError):
        load_bundle(path)


# --- which expirations to fetch -------------------------------------------------------------


def test_expirations_in_the_25_to_45_day_window(bundle):
    # Session 2026-11-02: 11-27 is 25 days out, 12-04 is 32, 12-11 is 39, 12-18 is 46 (out).
    glw = expirations_in_window(bundle["chains"]["GLW"], SESSION)
    assert glw == (date(2026, 11, 27), date(2026, 12, 4), date(2026, 12, 11))
    assert expirations_in_window(bundle["chains"]["KR"], SESSION) == glw
    # QQQ also lists 11-30 (28 days).
    assert expirations_in_window(bundle["chains"]["QQQ"], SESSION) == (
        date(2026, 11, 27), date(2026, 11, 30), date(2026, 12, 4), date(2026, 12, 11))


def test_window_misses_both_monthly_expirations_today(bundle):
    # A real finding pinned as a test: the 2026-11-20 (18 days) and 2026-12-18 (46 days)
    # monthlies both fall outside 25..45, so on this session only weeklies qualify.
    for ticker in ("GLW", "KR", "QQQ"):
        window = expirations_in_window(bundle["chains"][ticker], SESSION)
        assert date(2026, 11, 20) not in window and date(2026, 12, 18) not in window


def test_window_follows_the_config(bundle):
    wide = CaptureConfig(min_dte=18, max_dte=46)
    window = expirations_in_window(bundle["chains"]["GLW"], SESSION, wide)
    assert window[0] == date(2026, 11, 20) and window[-1] == date(2026, 12, 18)


def test_a_chain_that_cannot_open_positions_has_no_window(bundle):
    chain = dict(bundle["chains"]["GLW"], can_open_position=False)
    assert expirations_in_window(chain, SESSION) == ()


# --- the underlying price -------------------------------------------------------------------


def test_underlying_uses_the_regular_session_last_trade(bundle):
    glw = next(r for r in bundle["equity_quotes"] if r["quote"]["symbol"] == "GLW")
    quote = underlying_quote(glw)
    # 123.27 at 20:59:58Z (3:59 PM ET), NOT the 124.1777 after-hours print at 22:54Z: the
    # option quotes are regular-session closes, so the stock must be too.
    assert quote["price"] == pytest.approx(123.27)
    assert quote["quote_at"] == datetime(2026, 11, 2, 20, 59, 58, 177914, tzinfo=timezone.utc)


@pytest.mark.parametrize("patch", [{"state": "halted"}, {"has_traded": False}, {"last_trade_price": "0.000000"},
                                   {"last_trade_price": "abc"}, {"venue_last_trade_time": None},
                                   {"venue_last_trade_time": "2026-11-02T20:59:58"}])
def test_underlying_fails_closed(bundle, patch):
    glw = copy.deepcopy(next(r for r in bundle["equity_quotes"] if r["quote"]["symbol"] == "GLW"))
    glw["quote"].update(patch)
    assert underlying_quote(glw) is None


# --- which strikes to quote -----------------------------------------------------------------


def test_quote_plan_keeps_strikes_from_70_to_100_percent_of_spot(bundle):
    spots = {"GLW": 123.27, "KR": 68.37, "QQQ": 681.13}
    batches = quote_plan(bundle["instruments"], spots)
    ids = [i for b in batches for i in b]
    strikes = {i["id"]: (t, float(i["strike_price"])) for t, rows in bundle["instruments"].items() for i in rows}
    kept = sorted(strikes[i] for i in ids)
    # GLW: 86.29..123.27 keeps 95..120 and drops the in-the-money 140..175 strikes.
    assert [s for t, s in kept if t == "GLW"] == [95.0, 100.0, 105.0, 110.0, 115.0, 120.0]
    # KR: 47.86..68.37 keeps all seven quoted strikes (61..67); QQQ: all three.
    assert [s for t, s in kept if t == "KR"] == [61.0, 62.0, 63.0, 64.0, 65.0, 66.0, 67.0]
    assert [s for t, s in kept if t == "QQQ"] == [640.0, 650.0, 660.0]
    assert len(ids) == len(set(ids)) == 16


def test_quote_plan_batches_at_most_20_ids(bundle):
    # get_option_quotes drops the official closes above 20 ids; batches stay at or below it.
    spots = {"GLW": 1000.0, "KR": 1000.0, "QQQ": 1000.0}  # every strike in band
    batches = quote_plan(bundle["instruments"], spots, CaptureConfig(strike_band=(0.0, 1.0)))
    assert sum(len(b) for b in batches) == 24
    assert all(1 <= len(b) <= 20 for b in batches)


def test_quote_plan_skips_a_ticker_without_a_spot(bundle):
    batches = quote_plan(bundle["instruments"], {"QQQ": 681.13})
    ids = {i for b in batches for i in b}
    assert ids == {i["id"] for i in bundle["instruments"]["QQQ"]}


# --- normalising into select_candidates rows ------------------------------------------------


def test_normalize_produces_the_select_candidates_row_shape(bundle):
    rows, dropped = normalize_chain_rows(bundle["instruments"], bundle["option_quotes"])
    assert dropped == ()
    assert len(rows) == 24
    row = _by_id(rows)["ef4c34e4-5be9-4054-8f45-3cd26a34031b"]  # GLW 2026-12-04 105 put
    assert row == {
        "ticker": "GLW",
        "option_type": "put",
        "option_id": "ef4c34e4-5be9-4054-8f45-3cd26a34031b",
        "expiration_date": date(2026, 12, 4),
        "strike": 105.0,
        "bid": 2.46,
        "ask": 3.25,
        "delta": -0.185454,
        "implied_volatility": 0.70028,
        # Robinhood sends nanoseconds; microseconds are kept, the rest truncated.
        "quote_at": datetime(2026, 11, 2, 20, 59, 59, 973228, tzinfo=timezone.utc),
        "contract_multiplier": 100,
    }


def _mutate(bundle, option_id, *, quote=None, instrument=None, drop_quote=False):
    b = copy.deepcopy(bundle)
    if drop_quote:
        b["option_quotes"] = [q for q in b["option_quotes"] if q["quote"]["instrument_id"] != option_id]
    for q in b["option_quotes"]:
        if q["quote"]["instrument_id"] == option_id and quote:
            q["quote"].update(quote)
    for rows in b["instruments"].values():
        for i in rows:
            if i["id"] == option_id and instrument:
                i.update(instrument)
    return b


GLW_105 = "ef4c34e4-5be9-4054-8f45-3cd26a34031b"


@pytest.mark.parametrize(("kwargs", "reason"), [
    ({"drop_quote": True}, "missing_quote"),
    ({"quote": {"bid_price": "0.000000"}}, "no_bid"),
    ({"quote": {"bid_price": "3.300000"}}, "crossed_market"),  # bid above the 3.25 ask
    ({"quote": {"delta": None}}, "invalid_quote"),
    ({"quote": {"delta": "abc"}}, "invalid_quote"),
    ({"quote": {"updated_at": "2026-11-02T20:59:59"}}, "invalid_quote"),  # naive
    ({"instrument": {"state": "expired"}}, "not_active"),
    ({"instrument": {"tradability": "untradable"}}, "not_tradable"),
    ({"instrument": {"type": "call"}}, "not_a_put"),
    ({"instrument": {"trade_value_multiplier": "150.0000"}}, "nonstandard_multiplier"),
    ({"instrument": {"chain_symbol": "KR"}}, "symbol_mismatch"),
    ({"instrument": {"strike_price": "-5"}}, "invalid_instrument"),
])
def test_normalize_drops_with_a_reason_never_estimates(bundle, kwargs, reason):
    broken = _mutate(bundle, GLW_105, **kwargs)
    rows, dropped = normalize_chain_rows(broken["instruments"], broken["option_quotes"])
    assert GLW_105 not in _by_id(rows)
    assert ("GLW", GLW_105, reason) in dropped
    assert len(rows) == 23  # every other contract still captured


def test_two_disagreeing_quotes_for_one_contract_drop_it(bundle):
    b = copy.deepcopy(bundle)
    twin = copy.deepcopy(next(q for q in b["option_quotes"] if q["quote"]["instrument_id"] == GLW_105))
    twin["quote"]["bid_price"] = "2.500000"
    b["option_quotes"].append(twin)
    rows, dropped = normalize_chain_rows(b["instruments"], b["option_quotes"])
    assert GLW_105 not in _by_id(rows)
    assert ("GLW", GLW_105, "ambiguous_quote") in dropped


def test_a_quote_with_no_instrument_is_ignored(bundle):
    b = copy.deepcopy(bundle)
    b["instruments"]["GLW"] = [i for i in b["instruments"]["GLW"] if i["id"] != GLW_105]
    rows, dropped = normalize_chain_rows(b["instruments"], b["option_quotes"])
    assert GLW_105 not in _by_id(rows) and len(rows) == 23
    assert ("?", GLW_105, "unknown_instrument") in dropped


# --- the whole capture, then the existing selector ------------------------------------------


def test_capture_from_bundle(bundle):
    result = capture_from_bundle(bundle)
    assert result.session == SESSION
    assert result.as_of == CAPTURED_AT
    assert len(result.chain_rows) == 24
    assert set(result.underlying_quotes) == {"GLW", "KR", "QQQ"}
    assert result.underlying_quotes["KR"]["price"] == pytest.approx(68.37)


def test_capture_refuses_quotes_from_after_the_capture_time(bundle):
    b = _mutate(bundle, GLW_105, quote={"updated_at": "2026-11-02T23:30:00Z"})  # 20 min after capture
    result = capture_from_bundle(b)
    assert GLW_105 not in _by_id(result.chain_rows)
    assert ("GLW", GLW_105, "future_quote") in result.dropped


def _account(value: float) -> AccountContext:
    return AccountContext(account_alias=ALIAS, account_value=value,
                          sector_by_ticker={"GLW": "tech", "KR": "staples", "QQQ": "etf"},
                          sector_value_by_sector={"tech": 0.0, "staples": 0.0, "etf": 0.0})


def _select(result, value):
    ladder = {t: q["price"] for t, q in result.underlying_quotes.items()}
    return select_candidates(result.chain_rows, result.underlying_quotes, ladder, ["GLW", "KR", "QQQ"], [],
                             result.as_of, account=_account(value))


def test_real_chains_through_the_selector_large_account(bundle):
    # ADR defaults: delta -0.30..-0.15, spread <= 10% of mid, collateral <= 10% of the account.
    # GLW: the in-band 105 and 110 puts have spreads of 0.79/2.855 = 27.7% and 0.85/3.875 = 21.9%.
    # KR: 61..65 in band, spreads 47% to 181% of mid. Neither ticker has an eligible contract.
    # QQQ 640 (spread 0.24/8.47 = 2.8%) and 650 (0.26/10.41 = 2.5%) pass; 660 (delta -0.317)
    # does not. Annualised bid yield: 640 -> 8.35/640*365/32 = 14.9%, 650 -> 10.28/650*365/32
    # = 18.0%, so 650 is the pick. Collateral $65,000 <= 10% of $1,000,000.
    picks = _select(capture_from_bundle(bundle), 1_000_000.0)
    assert [(s.ticker, s.strike) for s in picks.suggestions] == [("QQQ", 650.0)]
    assert picks.suggestions[0].bid == pytest.approx(10.28)
    assert picks.suggestions[0].annualised_bid_yield == pytest.approx(10.28 / 650 * 365 / 32)
    assert ("GLW", "no_eligible_contract") in picks.dropped
    assert ("KR", "no_eligible_contract") in picks.dropped


def test_real_chains_through_the_selector_small_account(bundle):
    # With a $50,000 account the 10% cap is $5,000 of collateral: QQQ ($64,000+) is out too,
    # so on this session nothing qualifies. Pinned so the operator sees it before 6 months pass.
    picks = _select(capture_from_bundle(bundle), 50_000.0)
    assert picks.suggestions == ()
    assert {t for t, r in picks.dropped if r == "no_eligible_contract"} == {"GLW", "KR", "QQQ"}


# --- the append-only snapshot store ---------------------------------------------------------


def _clock(moment):
    return lambda: moment


def test_record_and_read_back(bundle):
    con = duckdb.connect()
    ensure_capture_schema(con)
    result = capture_from_bundle(bundle)
    stored = CAPTURED_AT + timedelta(minutes=1)
    assert record_capture(con, result, run_id="run-1", clock=_clock(stored)) == 24
    rows = captured_rows(con, SESSION)
    assert len(rows) == 24
    row = next(r for r in rows if r["option_id"] == GLW_105)
    assert (row["ticker"], row["bid"], row["ask"]) == ("GLW", 2.46, 3.25)
    assert row["underlying_price"] == pytest.approx(123.27)
    assert row["source"] == "csp_chain_capture" and row["run_id"] == "run-1"


def test_rerun_of_the_same_capture_adds_nothing(bundle):
    con = duckdb.connect()
    result = capture_from_bundle(bundle)
    record_capture(con, result, run_id="run-1", clock=_clock(CAPTURED_AT))
    assert record_capture(con, result, run_id="run-2", clock=_clock(CAPTURED_AT + timedelta(hours=1))) == 0
    assert len(captured_rows(con, SESSION)) == 24


def test_reader_is_point_in_time(bundle):
    con = duckdb.connect()
    record_capture(con, capture_from_bundle(bundle), run_id="run-1", clock=_clock(CAPTURED_AT))
    assert captured_rows(con, SESSION, known_at=CAPTURED_AT - timedelta(seconds=1)) == []
    assert len(captured_rows(con, SESSION, known_at=CAPTURED_AT)) == 24
    assert captured_rows(con, SESSION + timedelta(days=1)) == []


def test_a_later_requote_is_a_new_version_and_the_reader_sees_the_newest(bundle):
    con = duckdb.connect()
    record_capture(con, capture_from_bundle(bundle), run_id="run-1", clock=_clock(CAPTURED_AT))
    later = _mutate(bundle, GLW_105, quote={"bid_price": "2.500000"})
    later["captured_at"] = "2026-11-02T23:20:00+00:00"
    record_capture(con, capture_from_bundle(later), run_id="run-2",
                   clock=_clock(CAPTURED_AT + timedelta(minutes=10)))
    rows = captured_rows(con, SESSION)
    assert len(rows) == 24
    assert next(r for r in rows if r["option_id"] == GLW_105)["bid"] == 2.5
    early = captured_rows(con, SESSION, known_at=CAPTURED_AT + timedelta(minutes=5))
    assert next(r for r in early if r["option_id"] == GLW_105)["bid"] == 2.46


# --- it can never trade ---------------------------------------------------------------------


def test_module_names_no_broker_order_tool_or_mcp_call():
    offenders = [f"{n}: {line.strip()}" for n, line in enumerate(MODULE.read_text().splitlines(), 1)
                 if _BROKER_TOOL.search(line) or _MCP.search(line)]
    assert not offenders, "the chain capture must never place, preview, review or cancel an order:\n" + "\n".join(
        offenders)


def test_module_imports_nothing_that_reaches_a_network():
    source = MODULE.read_text()
    banned = re.compile(r"^\s*(import|from)\s+(urllib|http|requests|socket|ssl|subprocess|httpx|aiohttp)\b", re.M)
    assert not banned.search(source)
