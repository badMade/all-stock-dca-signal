"""Synthetic rows for the cash-secured put shadow-log tests (ADR docs/adr-csp-shadow-log.md).

Everything here is SYNTHETIC: round, hand-picked numbers chosen so each expected value can be
checked with pencil and paper next to the assertion that uses it. None of it is a real option
chain, a real quote or a real account. Tickers are deliberately fake (``ZZZA``, ``ZZZB`` ...)
so nobody mistakes a fixture for market data, and the account is an alias built at runtime
from an obviously fake label, never an account-shaped literal.

The shapes match what a caller would assemble from the read-only Robinhood chain, instrument
and quote tools before handing plain dicts to ``csp_shadow`` (the module never fetches):

* chain row: ticker, option_type, expiration_date, strike, bid, ask, delta,
  implied_volatility, quote_at, option_id -- numerics may be strings;
* underlying quote: ticker -> {price, quote_at};
* later price row (settlement): ticker, quote_date, close.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from market_data_ingest.ledger.accounts import account_alias

from .conftest import TEST_SALT

ET = ZoneInfo("America/New_York")

# 2026-03-02 is a Monday. The selection runs at 15:00 ET that day, so its session is 03-02.
SESSION = date(2026, 3, 2)
AS_OF = datetime(2026, 3, 2, 15, 0, tzinfo=ET)
# The quotes were taken five minutes before the selection: same session, not after as_of.
QUOTE_AT = datetime(2026, 3, 2, 14, 55, tzinfo=ET)
# 2026-04-02 is 31 calendar days after 2026-03-02 -> inside the 25..45 day window.
EXPIRY = date(2026, 4, 2)

ALIAS = account_alias("shadow-test-account", salt=TEST_SALT)

# A synthetic $100,000 account: 10% per-contract cap = $10,000, 25% total cap = $25,000,
# 30% sector cap = $30,000 of holdings.
ACCOUNT_VALUE = 100_000.0


def chain_row(
    ticker: str = "ZZZA",
    *,
    strike: object = 45.0,
    bid: object = 0.95,
    ask: object = 1.03,
    delta: object = -0.22,
    expiration_date: object = EXPIRY,
    quote_at: object = QUOTE_AT,
    implied_volatility: object = 0.35,
    option_type: str = "put",
    option_id: str | None = None,
) -> dict:
    """One synthetic put quote. Default contract, worked once here and reused everywhere:

    mid = (0.95 + 1.03) / 2 = 0.99; spread = 0.08 / 0.99 = 8.08% of mid (<= 10%, passes);
    delta -0.22 is inside [-0.30, -0.15]; days to expiry 31 is inside [25, 45];
    collateral = 45 * 100 = $4,500 = 4.5% of the $100,000 account (<= 10%, passes).
    """
    return {
        "ticker": ticker,
        "option_type": option_type,
        "expiration_date": expiration_date,
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "delta": delta,
        "implied_volatility": implied_volatility,
        "quote_at": quote_at,
        "option_id": option_id or f"opt-{ticker}-{expiration_date}-{strike}",
    }


def underlying(price: object = 50.0, quote_at: object = QUOTE_AT) -> dict:
    return {"price": price, "quote_at": quote_at}


def account(*, value: float = ACCOUNT_VALUE, sectors: dict | None = None, holdings: dict | None = None):
    from market_data_ingest.ledger.csp_shadow import AccountContext

    return AccountContext(
        account_alias=ALIAS,
        account_value=value,
        sector_by_ticker=sectors if sectors is not None else {"ZZZA": "synthetic-a", "ZZZB": "synthetic-b",
                                                              "ZZZC": "synthetic-c"},
        sector_value_by_sector=holdings if holdings is not None else {"synthetic-a": 10_000.0,
                                                                      "synthetic-b": 10_000.0,
                                                                      # Held nothing, said explicitly: a sector
                                                                      # MISSING from this map fails closed.
                                                                      "synthetic-c": 0.0},
    )


def select(rows, *, quotes=None, ladder=None, active=("ZZZA", "ZZZB", "ZZZC"), paused=(), as_of=AS_OF,
           acct="default", open_positions=(), config=None):
    """Call ``select_candidates`` with the defaults above; returns the SelectionResult."""
    from market_data_ingest.ledger.csp_shadow import DEFAULT_CONFIG, select_candidates

    return select_candidates(
        rows,
        quotes if quotes is not None else {"ZZZA": underlying(), "ZZZB": underlying(), "ZZZC": underlying()},
        # The graded ladder would buy each name at $48.00 today, so strikes up to 48 qualify.
        ladder if ladder is not None else {"ZZZA": 48.0, "ZZZB": 48.0, "ZZZC": 48.0},
        frozenset(active),
        None if paused is None else frozenset(paused),
        as_of,
        config or DEFAULT_CONFIG,
        account=account() if acct == "default" else acct,
        open_positions=open_positions,
    )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)
