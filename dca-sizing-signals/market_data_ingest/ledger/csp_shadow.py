"""Cash-secured put SHADOW LOG: research only, suggest only, never trades.

Implements the PROPOSED ADR docs/adr-csp-shadow-log.md (PR #2103) beside the Phase 0 ledger
(docs/adr-outcome-ledger-pit-harness.md, PR #2101). The question it answers is narrow: on this
account's real bid prices, would selling a cash-secured put on a name the graded DCA ladder
already wants to own have beaten leaving the same cash in the 3.60% APY sweep, or in plain DCA?

What this module is NOT allowed to do (ADR decision 1 and "Verification"):

* It has no code path that sends, previews, reviews or withdraws any brokerage order, and it
  names no broker order tool at all; tests/ledger/test_csp_shadow_no_orders.py greps this file
  for those names and fails the build if one appears.
* It makes no network call and fetches nothing. A caller reads option chains, instruments and
  quotes from the READ-ONLY Robinhood tools and hands the rows in as plain dicts.
* It never changes DCA sizing and nothing in the live paths imports it (ADR decision 7).

Shape (pure functions over plain rows, plus an append-only store):

* ``select_candidates`` -> at most one shadow suggestion per active ticker per session, with
  every filter and cap from ADR decision 2 and open question 4. A missing, stale, future-dated
  or non-finite input drops that contract or ticker with a reason; it is never estimated and
  never raises into the caller (fail closed, project rule).
* ``entry_from_suggestion`` / ``record_shadow_entries`` -> shadow trades entered at the BID.
* ``settle`` -> expiry (OTM keeps the premium, ITM is assigned at the strike), or an early close
  costed at the ASK; an unknown later price leaves the trade unscored, never assumed.
* ``scorecard`` -> net return on locked collateral vs the cash sweep and vs plain DCA, the worst
  single-trade loss vs trailing 12 months of premium, and the ADR pass-bar booleans.
* ``open_short_puts`` / ``tax_loss_blocked_tickers`` -> the open shadow puts, so the tax-loss
  eligibility check can treat them as blocking (ADR decision 6).

Store rules are the ledger's (store.py), restated because this module keeps its OWN tables via
``ensure_csp_schema(con)`` and does not touch ``LedgerStore``'s schema: append-only (no
row-mutating SQL anywhere -- test_ledger_schema.py scans this file too), a ``content_hash`` in
every primary key with ``ON CONFLICT DO NOTHING``, provenance columns on every row, an
``inputs_known_at`` column so readers can ask "what was known then", one transaction per batch.

Every default below is PROPOSED, NOT OPERATOR-APPROVED (ADR PR #2103 open questions).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import duckdb

from ..prices.store import FAR_FUTURE, to_utc_naive
from .accounts import require_alias
from .errors import LedgerValidationError

ET = ZoneInfo("America/New_York")
SOURCE = "csp_shadow"
CSP_TABLES: tuple[str, ...] = ("csp_shadow_entries", "csp_shadow_settlements")
PROPOSED_DEFAULTS_STATUS = "proposed, not operator-approved (ADR PR #2103 open questions 3, 4, 6)"

# Cash sweep APY the ADR benchmarks against (High-Yield Cash, Sept 2026, per the operator).
SWEEP_APY = 0.036

# Float noise allowance for the inclusive caps: (1.05 - 0.95) / 1.00 is 0.10000000000000009 in
# binary floating point, and a spread of exactly 10% of mid must pass a "<= 10%" cap.
_EPS = 1e-9

SCORED_STATUSES = frozenset({"expired_otm", "assigned", "closed_early"})
SETTLEMENT_STATUSES = SCORED_STATUSES | {"unscored"}
# Any of these on a settled trade fails the ADR's "zero shadow assignments would have breached
# concentration or wash-sale rules" bar. ``concentration_unchecked`` is in the set on purpose:
# an assignment nobody could check is not evidence of no breach (fail closed).
EARLY_ASSIGNMENT_FLAGS = frozenset({"ex_dividend_in_window", "deep_itm"})
BREACH_FLAGS = frozenset({"assignment_breaches_sector_cap", "assigned_in_tax_loss_pause", "concentration_unchecked"})


@dataclass(frozen=True)
class CspShadowConfig:
    """Proposed defaults (ADR decision 2 and open questions 3/4). One-line diffs to change."""

    min_dte: int = 25
    max_dte: int = 45
    # Put deltas are negative: -0.30 is further in the money than -0.15.
    min_delta: float = -0.30
    max_delta: float = -0.15
    max_spread_pct_of_mid: float = 0.10
    max_collateral_pct_per_contract: float = 0.10
    max_total_collateral_pct: float = 0.25
    # "No candidates in a sector already ABOVE 30% of the account": exactly 30% still passes.
    max_sector_pct: float = 0.30
    contract_multiplier: int = 100
    # Early-assignment risk flag: underlying more than this fraction below the strike.
    deep_itm_pct: float = 0.10
    # Expiry is settled only once the expiry session's close has happened.
    market_close_et: time = time(16, 0)
    # One open shadow put per ticker, like a human writing one contract per name.
    one_open_per_ticker: bool = True
    # ADR decision 5 minimums: no pass before BOTH are met.
    pass_min_months: int = 6
    pass_min_settled_trades: int = 20
    trailing_premium_months: int = 12


DEFAULT_CONFIG = CspShadowConfig()


@dataclass(frozen=True)
class AccountContext:
    """What the caps need about the account, by ALIAS only (ledger/accounts.py).

    ``sector_value_by_sector`` is dollars currently held per sector; a sector absent from it
    holds nothing. A raw account identifier is refused at construction, without echoing it.
    """

    account_alias: str
    account_value: float
    sector_by_ticker: Mapping[str, str]
    sector_value_by_sector: Mapping[str, float]

    def __post_init__(self) -> None:
        require_alias(self.account_alias, where="AccountContext")


@dataclass(frozen=True)
class ShadowSuggestion:
    """One "would sell this put at the bid" record. Never an order."""

    suggestion_id: str
    account_alias: str
    session: date
    as_of: datetime
    ticker: str
    option_id: str
    expiration_date: date
    dte: int
    strike: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    delta: float
    implied_volatility: float | None
    quote_at: datetime
    underlying_price: float
    underlying_quote_at: datetime
    ladder_buy_price: float
    contract_multiplier: int
    collateral: float
    sector: str
    annualised_bid_yield: float


@dataclass(frozen=True)
class ShadowEntry:
    """A shadow trade: the suggestion entered at the BID (``entry_price == bid``, always)."""

    entry_id: str
    account_alias: str
    session: date
    ticker: str
    option_id: str
    expiration_date: date
    dte: int
    strike: float
    bid: float
    ask: float
    mid: float
    spread_pct: float
    delta: float
    implied_volatility: float | None
    quote_at: datetime
    underlying_price: float
    underlying_quote_at: datetime
    ladder_buy_price: float
    contract_multiplier: int
    collateral: float
    sector: str
    entry_price: float
    premium_dollars: float
    inputs_known_at: datetime


@dataclass(frozen=True)
class SelectionResult:
    suggestions: tuple[ShadowSuggestion, ...]
    # (ticker, reason) for every drop, so "every data failure dropped the suggestion" is auditable.
    dropped: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Settlement:
    entry_id: str
    ticker: str
    entry_session: date | None
    expiration_date: date | None
    status: str  # open | expired_otm | assigned | closed_early | unscored
    settled_on: date | None
    underlying_close: float | None
    exit_ask: float | None
    assigned: bool
    effective_cost_basis: float | None
    collateral: float | None
    premium_dollars: float | None
    pnl: float | None
    return_on_collateral: float | None
    days_held: int | None
    flags: tuple[str, ...]
    unscored_reason: str | None
    inputs_known_at: datetime


@dataclass(frozen=True)
class Scorecard:
    as_of_session: date
    sweep_apy: float
    n_settled: int
    n_unscored: int
    n_assigned: int
    net_pnl: float
    locked_collateral_years: float
    annualised_return_on_collateral: float | None
    sweep_pnl: float
    excess_vs_sweep: float
    dca_pnl: float | None
    excess_vs_dca: float | None
    worst_trade_loss: float
    trailing_12m_premium: float
    overlay_max_drawdown: float
    benchmark_max_drawdown: float | None
    beats_sweep: bool
    beats_dca: bool | None
    worst_loss_below_premium: bool
    drawdown_ok: bool
    no_breaches: bool
    has_min_months: bool
    has_min_trades: bool
    # Review of #2105: any expired-but-unscored trade blocks a pass.
    no_unscored: bool
    # Risk notes (not pass gates): settled trades flagged for early-assignment risk.
    n_ex_dividend_risk: int
    n_deep_itm_risk: int
    n_early_assignment_risk: int
    passes: bool
    breach_flags: tuple[str, ...] = field(default=())


# --- parsing (numerics may arrive as strings; non-finite is refused) -------------------------


def _num(value: object) -> float | None:
    """A finite float from a number or a numeric string (DoltHub and broker payloads send
    strings), else None. Booleans are refused: ``True`` is not a price."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            out = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return out if math.isfinite(out) else None


def _day(value: object) -> date | None:
    if isinstance(value, datetime):
        return None  # a timestamp is not a session date; refuse rather than guess the zone
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value.strip()) == 10:
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _moment(value: object) -> datetime | None:
    """An AWARE datetime, or an ISO 8601 string carrying an offset. Naive values are refused:
    without a zone the quote's session cannot be known."""
    if isinstance(value, str) and value.strip():
        try:
            value = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value
    return None


def _ticker(value: object) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _session_of(moment: datetime) -> date:
    return moment.astimezone(ET).date()


def _require_aware(as_of: object, what: str) -> datetime:
    # A caller bug, not a data failure: refuse loudly instead of silently picking a zone.
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise LedgerValidationError(f"{what} must be a timezone-aware datetime")
    return as_of


def _get(obj: object, key: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(key)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj).get(key)
    return None


def _hash(values: Sequence[Any]) -> str:
    return hashlib.sha256(json.dumps(list(values), default=str, separators=(",", ":")).encode()).hexdigest()


def _add_months(day: date, months: int) -> date:
    """Same day-of-month ``months`` later (or earlier), clamped to the month's last day."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, min(day.day, d))
        except ValueError:
            continue
    raise AssertionError("unreachable")  # pragma: no cover


# --- selection -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Contract:
    ticker: str
    option_id: str
    expiration_date: date
    strike: float
    bid: float
    ask: float
    delta: float
    implied_volatility: float | None
    quote_at: datetime


def _parse_contract(raw: Mapping[str, Any]) -> _Contract | None:
    ticker = _ticker(raw.get("ticker"))
    expiry = _day(raw.get("expiration_date"))
    strike, bid, ask, delta = (_num(raw.get(k)) for k in ("strike", "bid", "ask", "delta"))
    quote_at = _moment(raw.get("quote_at"))
    iv_raw = raw.get("implied_volatility")
    iv = None if iv_raw is None else _num(iv_raw)
    if None in (ticker, expiry, strike, bid, ask, delta, quote_at) or (iv_raw is not None and iv is None):
        return None
    option_id = raw.get("option_id")
    if not isinstance(option_id, str) or not option_id.strip():
        option_id = f"{ticker}:{expiry}:{strike}"
    return _Contract(ticker, option_id.strip(), expiry, strike, bid, ask, delta, iv, quote_at)  # type: ignore[arg-type]


@dataclass
class _OpenBook:
    tickers: set[str]
    locked: float
    # Collateral of open shadow puts per sector: an assignment would land in that sector, so it
    # counts toward the sector's exposure (review of #2105).
    sector_load: dict[str, float]
    unknown_sector: bool


def _open_book(open_positions: Iterable[object], session: date, account: AccountContext) -> _OpenBook:
    """Tickers with an open shadow put on ``session``, the collateral those puts lock, and that
    collateral by sector. A position whose expiry or collateral cannot be read blocks its ticker
    and fills the whole book (fail closed); one whose sector cannot be known is reported so the
    caller can refuse to judge sector exposure at all."""
    book = _OpenBook(set(), 0.0, {}, False)
    for pos in open_positions:
        expiry = _day(_get(pos, "expiration_date"))
        ticker = _ticker(_get(pos, "ticker"))
        if expiry is not None and expiry < session:
            continue  # expired before today's session: neither open nor locking cash
        if ticker:
            book.tickers.add(ticker)
        collateral = _num(_get(pos, "collateral"))
        amount = math.inf if collateral is None or expiry is None else collateral
        book.locked += amount
        sector = _get(pos, "sector")
        if not isinstance(sector, str) or not sector.strip():
            sector = account.sector_by_ticker.get(ticker) if ticker else None
        if not isinstance(sector, str) or not sector.strip():
            book.unknown_sector = True
            continue
        book.sector_load[sector] = book.sector_load.get(sector, 0.0) + amount
    return book


def select_candidates(
    chain_rows: Iterable[object],
    underlying_quotes: Mapping[str, Mapping[str, Any]],
    ladder_buy_price_by_ticker: Mapping[str, object],
    active_tickers: Iterable[str],
    paused_tickers: Iterable[str] | None,
    as_of: datetime,
    config: CspShadowConfig = DEFAULT_CONFIG,
    *,
    account: AccountContext | None = None,
    open_positions: Iterable[object] = (),
) -> SelectionResult:
    """At most one shadow put suggestion per active ticker for ``as_of``'s session.

    ``chain_rows``: put quotes as fetched by the caller (ticker, option_type, expiration_date,
    strike, bid, ask, delta, implied_volatility, quote_at, option_id). ``underlying_quotes``:
    ticker -> {price, quote_at}. ``ladder_buy_price_by_ticker``: the price the graded ladder
    would buy at today. ``open_positions``: open shadow entries (``open_short_puts`` rows or
    ``ShadowEntry``) -- they lock collateral and, by default, block a second put on the name.

    Fail closed: no account context, a quote from an earlier session or after ``as_of``, a
    non-finite number, a missing ladder price, sector or sector holding -> no record, with a
    reason in ``dropped``. ``paused_tickers=None`` means the tax-loss pause list is UNKNOWN,
    so nothing is suggested (a put on a paused name would rehearse a wash sale). Sector
    exposure is holdings + open shadow collateral + earlier picks in this run. Never an
    estimate, and data problems never raise.
    """
    as_of = _require_aware(as_of, "as_of")
    session = _session_of(as_of)
    dropped: set[tuple[str, str]] = set()

    def drop(ticker: str | None, reason: str) -> None:
        dropped.add((ticker or "?", reason))

    value = _num(account.account_value) if account is not None else None
    if account is None or value is None or value <= 0:
        return SelectionResult((), (("*", "no_account_context"),))

    if paused_tickers is None:
        # Review of #2105: an unknown pause list is not an empty one (fail closed).
        return SelectionResult((), (("*", "no_pause_list"),))
    active = {t for t in (_ticker(x) for x in active_tickers) if t}
    paused = {t for t in (_ticker(x) for x in paused_tickers) if t}
    book = _open_book(open_positions, session, account)
    if book.unknown_sector:
        return SelectionResult((), (("*", "open_position_without_sector"),))
    open_tickers, open_collateral = book.tickers, book.locked
    held_by_sector: dict[str, float] = {}

    # Parse and de-duplicate contracts. Two rows for one contract that DISAGREE are ambiguous
    # (which one did the broker show?), so that contract is dropped rather than guessed.
    by_ticker: dict[str, dict[str, _Contract]] = {}
    ambiguous: set[tuple[str, str]] = set()
    for raw in chain_rows:
        if not isinstance(raw, Mapping):
            drop(None, "invalid_row")
            continue
        if str(raw.get("option_type", "")).strip().lower() != "put":
            continue  # calls and untyped rows are simply not candidates
        contract = _parse_contract(raw)
        if contract is None:
            drop(_ticker(raw.get("ticker")), "invalid_row")
            continue
        quote_session = _session_of(contract.quote_at)
        if quote_session < session:
            drop(contract.ticker, "stale_option_quote")
            continue
        if contract.quote_at > as_of:
            drop(contract.ticker, "future_option_quote")  # lookahead
            continue
        seen = by_ticker.setdefault(contract.ticker, {})
        prior = seen.get(contract.option_id)
        if prior is not None and prior != contract:
            ambiguous.add((contract.ticker, contract.option_id))
        seen[contract.option_id] = contract

    picks: list[ShadowSuggestion] = []
    for ticker in sorted(by_ticker):
        if ticker not in active:
            drop(ticker, "not_active")
            continue
        if ticker in paused:
            drop(ticker, "tax_loss_pause")
            continue
        if config.one_open_per_ticker and ticker in open_tickers:
            drop(ticker, "open_shadow_put")
            continue
        ladder = _num(ladder_buy_price_by_ticker.get(ticker))
        if ladder is None or ladder <= 0:
            drop(ticker, "no_ladder_price")
            continue
        quote = underlying_quotes.get(ticker)
        if not isinstance(quote, Mapping):
            drop(ticker, "no_underlying_quote")
            continue
        u_price, u_at = _num(quote.get("price")), _moment(quote.get("quote_at"))
        if u_price is None or u_price <= 0 or u_at is None:
            drop(ticker, "invalid_underlying_quote")
            continue
        if _session_of(u_at) < session or u_at > as_of:
            drop(ticker, "stale_underlying_quote")
            continue
        sector = account.sector_by_ticker.get(ticker)
        if not isinstance(sector, str) or not sector.strip():
            drop(ticker, "no_sector")
            continue
        if sector not in account.sector_value_by_sector:
            # Review of #2105: a sector absent from the holdings map is unknown, not $0.
            drop(ticker, "no_sector_value")
            continue
        held = _num(account.sector_value_by_sector[sector])
        if held is None or held < 0:
            drop(ticker, "invalid_account_context")
            continue
        held_by_sector[sector] = held
        if (held + book.sector_load.get(sector, 0.0)) / value > config.max_sector_pct + _EPS:
            drop(ticker, "sector_cap")
            continue

        eligible: list[ShadowSuggestion] = []
        for contract in by_ticker[ticker].values():
            if (ticker, contract.option_id) in ambiguous:
                drop(ticker, "ambiguous_contract")
                continue
            suggestion = _evaluate(contract, session, as_of, ladder, u_price, u_at, sector, value, account, config)
            if suggestion is not None:
                eligible.append(suggestion)
        if not eligible:
            drop(ticker, "no_eligible_contract")
            continue
        # Best annualised bid yield on collateral; ties broken deterministically.
        eligible.sort(key=lambda s: (-s.annualised_bid_yield, s.expiration_date, s.strike, s.option_id))
        picks.append(eligible[0])

    # Total collateral cap and the running sector check, highest yield first. Open shadow
    # puts count against both; each accepted pick then adds its collateral to its sector, so a
    # second name in a sector the first pick pushed above 30% is refused (review of #2105).
    picks.sort(key=lambda s: (-s.annualised_bid_yield, s.ticker))
    cap = config.max_total_collateral_pct * value
    locked = open_collateral
    sector_load = dict(book.sector_load)
    kept: list[ShadowSuggestion] = []
    for s in picks:
        if (held_by_sector[s.sector] + sector_load.get(s.sector, 0.0)) / value > config.max_sector_pct + _EPS:
            drop(s.ticker, "sector_cap")
            continue
        if locked + s.collateral > cap + _EPS:
            drop(s.ticker, "total_collateral_cap")
            continue
        locked += s.collateral
        sector_load[s.sector] = sector_load.get(s.sector, 0.0) + s.collateral
        kept.append(s)
    kept.sort(key=lambda s: s.ticker)
    return SelectionResult(tuple(kept), tuple(sorted(dropped)))


def _evaluate(
    c: _Contract,
    session: date,
    as_of: datetime,
    ladder: float,
    u_price: float,
    u_at: datetime,
    sector: str,
    account_value: float,
    account: AccountContext,
    config: CspShadowConfig,
) -> ShadowSuggestion | None:
    """The per-contract filters of ADR decision 2 and the per-contract cap; None if any fails."""
    dte = (c.expiration_date - session).days
    if not config.min_dte <= dte <= config.max_dte:
        return None
    if not config.min_delta - _EPS <= c.delta <= config.max_delta + _EPS:
        return None
    if c.strike <= 0 or c.bid <= 0 or c.ask < c.bid:
        return None  # no premium at the bid, or a crossed market: not a usable quote
    mid = (c.bid + c.ask) / 2
    spread_pct = (c.ask - c.bid) / mid
    if spread_pct > config.max_spread_pct_of_mid + _EPS:
        return None
    if c.strike > ladder + _EPS:
        return None  # would commit to buying above the ladder's own buy price
    collateral = c.strike * config.contract_multiplier
    if collateral > config.max_collateral_pct_per_contract * account_value + _EPS:
        return None
    annualised = c.bid / c.strike * 365.0 / dte
    # Identity: one shadow record per (account, session, ticker, contract). A later run that
    # session with different quotes is a new VERSION of the same id (new content_hash).
    suggestion_id = _hash([account.account_alias, session.isoformat(), c.ticker, c.option_id])
    return ShadowSuggestion(
        suggestion_id=suggestion_id,
        account_alias=account.account_alias,
        session=session,
        as_of=as_of,
        ticker=c.ticker,
        option_id=c.option_id,
        expiration_date=c.expiration_date,
        dte=dte,
        strike=c.strike,
        bid=c.bid,
        ask=c.ask,
        mid=mid,
        spread_pct=spread_pct,
        delta=c.delta,
        implied_volatility=c.implied_volatility,
        quote_at=c.quote_at,
        underlying_price=u_price,
        underlying_quote_at=u_at,
        ladder_buy_price=ladder,
        contract_multiplier=config.contract_multiplier,
        collateral=collateral,
        sector=sector,
        annualised_bid_yield=annualised,
    )


def entry_from_suggestion(s: ShadowSuggestion) -> ShadowEntry:
    """Enter the shadow trade at the BID. Never the mid, never the ask (ADR decision 3)."""
    base = dataclasses.asdict(s)
    shared = {f.name: base[f.name] for f in dataclasses.fields(ShadowEntry) if f.name in base}
    return ShadowEntry(
        **shared,
        entry_id=s.suggestion_id,
        entry_price=s.bid,
        premium_dollars=s.bid * s.contract_multiplier,
        inputs_known_at=s.as_of,
    )


# --- settlement ------------------------------------------------------------------------------

_INVALID = object()  # a later price row that exists but cannot be used


def _last_closed_session(as_of: datetime, config: CspShadowConfig) -> date:
    """The latest date whose daily close can exist at ``as_of``: the as_of session itself only
    once its 16:00 ET close has happened (review of #2105), otherwise the day before."""
    local = as_of.astimezone(ET)
    return local.date() if local.time() >= config.market_close_et else local.date() - timedelta(days=1)


def _price_index(later_prices: Iterable[object], last_closed: date) -> dict[tuple[str, date], object]:
    """(ticker, quote_date) -> close, for rows dated on or before ``last_closed`` only: a bar
    whose close had not happened by as_of is invisible (lookahead). An unusable or conflicting
    row maps to ``_INVALID`` so it is unscored rather than skipped in favour of a guess."""
    out: dict[tuple[str, date], object] = {}
    for raw in later_prices:
        if not isinstance(raw, Mapping):
            continue
        ticker, quote_date = _ticker(raw.get("ticker")), _day(raw.get("quote_date"))
        if ticker is None or quote_date is None or quote_date > last_closed:
            continue
        close = _num(raw.get("close"))
        value: object = close if close is not None and close > 0 else _INVALID
        key = (ticker, quote_date)
        if key in out and out[key] != value:
            value = _INVALID
        out[key] = value
    return out


def _early_close_index(early_closes: Iterable[object], as_of: datetime) -> dict[str, list[tuple[datetime, object]]]:
    """entry_id -> [(quote_at, ask or _INVALID)], only buy-back quotes known by ``as_of``. All
    are kept: ``_pick_early_close`` chooses among them per entry, so row order never matters."""
    out: dict[str, list[tuple[datetime, object]]] = {}
    for raw in early_closes:
        if not isinstance(raw, Mapping):
            continue
        entry_id, quote_at = raw.get("entry_id"), _moment(raw.get("quote_at"))
        if not isinstance(entry_id, str) or quote_at is None or quote_at > as_of:
            continue
        ask = _num(raw.get("ask"))
        out.setdefault(entry_id, []).append((quote_at, ask if ask is not None and ask >= 0 else _INVALID))
    return out


def _pick_early_close(
    quotes: Sequence[tuple[datetime, object]], start: date, expiry: date, entered_at: datetime | None
) -> tuple[datetime, object] | None:
    """The EARLIEST buy-back quote inside the holding window (review of #2105: with several
    rows the last one in iteration order used to win, so a 3.00 or a 0.10 ask depended on row
    order). Two quotes at that same moment that disagree are ambiguous -> ``_INVALID``."""
    usable = [(at, ask) for at, ask in quotes
              if start <= _session_of(at) <= expiry and (entered_at is None or at >= entered_at)]
    if not usable:
        return None
    first = min(at for at, _ in usable)
    asks = {ask for at, ask in usable if at == first}  # floats and the _INVALID sentinel hash fine
    return (first, asks.pop()) if len(asks) == 1 else (first, _INVALID)


def _settled_after_close(as_of: datetime, expiry: date, config: CspShadowConfig) -> bool:
    local = as_of.astimezone(ET)
    return local.date() > expiry or (local.date() == expiry and local.time() >= config.market_close_et)


def settle(
    entries: Iterable[object],
    later_prices: Iterable[object],
    as_of: datetime,
    *,
    early_closes: Iterable[object] = (),
    ex_dividend_dates: Mapping[str, Iterable[object]] | None = None,
    paused_tickers: Iterable[str] = (),
    account: AccountContext | None = None,
    config: CspShadowConfig = DEFAULT_CONFIG,
) -> tuple[Settlement, ...]:
    """Settle shadow entries as known at ``as_of``, one ``Settlement`` per entry.

    * An early close (``early_closes`` row: entry_id, ask, quote_at) is costed at the ASK; with
      several quotes for one entry the earliest inside the holding window is used.
    * After the expiry session's close: underlying close on the expiry date >= strike ->
      ``expired_otm`` (premium kept); below the strike -> ``assigned`` at the strike, effective
      cost basis strike - premium, marked at that close.
    * No usable expiry price -> ``unscored``, never assumed. Before expiry -> ``open``.

    Early-assignment RISK is flagged, not simulated: ``ex_dividend_in_window`` when an
    ex-dividend date falls inside the holding window, ``deep_itm`` when a later close inside it
    sat more than ``deep_itm_pct`` below the strike. Assignments are also checked against the
    tax-loss pause and the sector cap (``account``); unchecked counts as a breach later.
    """
    as_of = _require_aware(as_of, "as_of")
    session = _session_of(as_of)
    prices = _price_index(later_prices, _last_closed_session(as_of, config))
    closes = _early_close_index(early_closes, as_of)
    paused = {t for t in (_ticker(x) for x in paused_tickers) if t}
    ex_divs = {
        t: {d for d in (_day(x) for x in days) if d}
        for t, days in ((_ticker(k), v) for k, v in (ex_dividend_dates or {}).items()) if t
    }
    return tuple(_settle_one(e, session, as_of, prices, closes, paused, ex_divs, account, config) for e in entries)


def _settle_one(
    entry: object,
    session: date,
    as_of: datetime,
    prices: Mapping[tuple[str, date], object],
    closes: Mapping[str, list[tuple[datetime, object]]],
    paused: set[str],
    ex_divs: Mapping[str, set[date]],
    account: AccountContext | None,
    config: CspShadowConfig,
) -> Settlement:
    entry_id = _get(entry, "entry_id")
    ticker = _ticker(_get(entry, "ticker"))
    start = _day(_get(entry, "session"))
    expiry = _day(_get(entry, "expiration_date"))
    strike, price_in = _num(_get(entry, "strike")), _num(_get(entry, "entry_price"))
    collateral, premium = _num(_get(entry, "collateral")), _num(_get(entry, "premium_dollars"))
    mult = _num(_get(entry, "contract_multiplier")) or float(config.contract_multiplier)
    base = dict(
        entry_id=entry_id if isinstance(entry_id, str) else "",
        ticker=ticker or "?", entry_session=start, expiration_date=expiry, settled_on=None,
        underlying_close=None, exit_ask=None, assigned=False, effective_cost_basis=None,
        collateral=collateral, premium_dollars=premium, pnl=None, return_on_collateral=None,
        days_held=None, unscored_reason=None, inputs_known_at=as_of,
    )
    if not base["entry_id"] or None in (ticker, start, expiry, strike, price_in, collateral, premium):
        return Settlement(**base, status="unscored", flags=(), unscored_reason="invalid_entry")
    assert ticker and start and expiry and strike is not None and price_in is not None  # narrowed above
    assert collateral is not None and premium is not None

    flags: list[str] = []
    if any(start < d <= expiry for d in ex_divs.get(ticker, ())):
        flags.append("ex_dividend_in_window")
    deep_line = strike * (1 - config.deep_itm_pct)
    if any(t == ticker and start < d <= expiry and isinstance(v, float) and v < deep_line
           for (t, d), v in prices.items()):
        flags.append("deep_itm")

    entered_at = _moment(_get(entry, "inputs_known_at"))
    early = _pick_early_close(closes.get(base["entry_id"], ()), start, expiry, entered_at)
    if early is not None:
        quote_at, ask = early
        closed_on = _session_of(quote_at)
        if ask is _INVALID:
            return Settlement(**{**base, "unscored_reason": "invalid_early_close_quote"}, status="unscored",
                              flags=tuple(flags))
        assert isinstance(ask, float)
        pnl = (price_in - ask) * mult  # buy back at the ASK, never the mid
        return Settlement(**{**base, "settled_on": closed_on, "exit_ask": ask, "pnl": pnl,
                             "return_on_collateral": pnl / collateral,
                             "days_held": max(1, (closed_on - start).days)},
                          status="closed_early", flags=tuple(flags))

    if not _settled_after_close(as_of, expiry, config):
        return Settlement(**base, status="open", flags=tuple(flags))

    close = prices.get((ticker, expiry))
    if close is None or close is _INVALID:
        reason = "no_expiry_price" if close is None else "invalid_expiry_price"
        return Settlement(**{**base, "unscored_reason": reason}, status="unscored", flags=tuple(flags))
    assert isinstance(close, float)
    days = (expiry - start).days
    if close < strike - _EPS:
        # At least $0.01 in the money at expiry: auto-exercised against us, shares bought at
        # the strike. Marked at the expiry close so the loss is the real one, not a paper zero.
        basis = strike - price_in
        pnl = (close - basis) * mult
        if ticker in paused:
            flags.append("assigned_in_tax_loss_pause")
        flags.extend(_sector_breach(ticker, strike * mult, account, config))
        return Settlement(**{**base, "settled_on": expiry, "underlying_close": close, "assigned": True,
                             "effective_cost_basis": basis, "pnl": pnl, "return_on_collateral": pnl / collateral,
                             "days_held": days}, status="assigned", flags=tuple(flags))
    return Settlement(**{**base, "settled_on": expiry, "underlying_close": close, "pnl": premium,
                         "return_on_collateral": premium / collateral, "days_held": days},
                      status="expired_otm", flags=tuple(flags))


def _sector_breach(ticker: str, added: float, account: AccountContext | None, config: CspShadowConfig) -> list[str]:
    value = _num(account.account_value) if account is not None else None
    if account is None or value is None or value <= 0:
        return ["concentration_unchecked"]
    sector = account.sector_by_ticker.get(ticker)
    held = _num(account.sector_value_by_sector.get(sector, 0.0)) if isinstance(sector, str) else None
    if held is None:
        return ["concentration_unchecked"]
    return ["assignment_breaches_sector_cap"] if (held + added) / value > config.max_sector_pct + _EPS else []


# --- scorecard -------------------------------------------------------------------------------


def _max_drawdown(pnls: Sequence[float]) -> float:
    """Largest peak-to-trough fall of cumulative dollar P&L, starting from zero."""
    peak = cum = worst = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def scorecard(
    settled: Iterable[object],
    sweep_apy: float = SWEEP_APY,
    dca_benchmark_rows: Iterable[object] = (),
    *,
    as_of: datetime,
    config: CspShadowConfig = DEFAULT_CONFIG,
) -> Scorecard:
    """ADR decisions 4 and 5 over settled shadow trades (``Settlement`` or equivalent dicts).

    ``dca_benchmark_rows``: entry_id -> ``dca_return_pct``, the return the SAME collateral
    dollars would have made in plain DCA of that ticker over the same holding period (computed
    by the caller from the Phase 0 outcome ledger). If any settled trade lacks one, the DCA
    comparison is None and the drawdown bar fails closed. ``passes`` is never True before
    ``pass_min_months`` AND ``pass_min_settled_trades`` are both met, and never while any trade
    that has expired is still unscored. Only trades settled on or before the as_of session count.
    Early-assignment flags are reported as counts (risk notes), not as a pass gate.

    The ADR's "every data failure dropped the suggestion" bar is structural (selection never
    raises and records its drops), so it is covered by tests rather than computed here.
    """
    as_of = _require_aware(as_of, "as_of")
    session = _session_of(as_of)
    rows = list(settled)
    scored = []
    for r in rows:
        status = _get(r, "status")
        pnl, collateral, days = _num(_get(r, "pnl")), _num(_get(r, "collateral")), _num(_get(r, "days_held"))
        start, done = _day(_get(r, "entry_session")), _day(_get(r, "settled_on"))
        # Review of #2105: a trade settled after the as_of session is not known yet (lookahead).
        if status in SCORED_STATUSES and None not in (pnl, collateral, days, start, done) and done <= session:
            scored.append({"entry_id": _get(r, "entry_id"), "status": status, "pnl": pnl, "collateral": collateral,
                           "days": days, "start": start, "done": done,
                           "premium": _num(_get(r, "premium_dollars")) or 0.0,
                           "flags": tuple(_get(r, "flags") or ())})
    scored.sort(key=lambda r: (r["done"], str(r["entry_id"])))
    # Unscored trades whose expiry has passed are counted and block a pass (review of #2105):
    # missing-price trades are often the losers, so letting them drop out biases toward a pass.
    # An unscored row with an unreadable expiry counts too (fail closed).
    n_unscored = 0
    for r in rows:
        if _get(r, "status") == "unscored":
            expiry = _day(_get(r, "expiration_date"))
            if expiry is None or expiry <= session:
                n_unscored += 1
    # Early-assignment risk is flagged, not simulated; the counts are reported as risk notes.
    n_ex_div = sum(1 for r in scored if "ex_dividend_in_window" in r["flags"])
    n_deep = sum(1 for r in scored if "deep_itm" in r["flags"])
    n_risk = sum(1 for r in scored if EARLY_ASSIGNMENT_FLAGS & set(r["flags"]))

    net = sum(r["pnl"] for r in scored)
    years = sum(r["collateral"] * r["days"] / 365.0 for r in scored)
    sweep = sum(r["collateral"] * ((1 + sweep_apy) ** (r["days"] / 365.0) - 1) for r in scored)

    bench = {}
    for raw in dca_benchmark_rows:
        if isinstance(raw, Mapping) and isinstance(raw.get("entry_id"), str):
            ret = _num(raw.get("dca_return_pct"))
            if ret is not None:
                bench[raw["entry_id"]] = ret
    matched = bool(scored) and all(r["entry_id"] in bench for r in scored)
    dca_series = [r["collateral"] * bench[r["entry_id"]] for r in scored] if matched else None
    dca_pnl = sum(dca_series) if dca_series is not None else None

    worst = max([0.0] + [-r["pnl"] for r in scored])
    cutoff = _add_months(session, -config.trailing_premium_months)
    trailing = sum(r["premium"] for r in scored if cutoff < r["start"] <= session)
    overlay_dd = _max_drawdown([r["pnl"] for r in scored])
    bench_dd = _max_drawdown(dca_series) if dca_series is not None else None
    breaches = tuple(sorted({f for r in scored for f in r["flags"] if f in BREACH_FLAGS}))

    has_trades = len(scored) >= config.pass_min_settled_trades
    has_months = bool(scored) and max(r["done"] for r in scored) >= _add_months(
        min(r["start"] for r in scored), config.pass_min_months)
    beats_sweep = bool(scored) and net > sweep
    worst_ok = bool(scored) and worst < trailing
    drawdown_ok = bench_dd is not None and overlay_dd <= bench_dd + _EPS
    no_breaches = bool(scored) and not breaches
    return Scorecard(
        as_of_session=session,
        sweep_apy=sweep_apy,
        n_settled=len(scored),
        n_unscored=n_unscored,
        n_assigned=sum(1 for r in scored if r["status"] == "assigned"),
        net_pnl=net,
        locked_collateral_years=years,
        annualised_return_on_collateral=net / years if years > 0 else None,
        sweep_pnl=sweep,
        excess_vs_sweep=net - sweep,
        dca_pnl=dca_pnl,
        excess_vs_dca=net - dca_pnl if dca_pnl is not None else None,
        worst_trade_loss=worst,
        trailing_12m_premium=trailing,
        overlay_max_drawdown=overlay_dd,
        benchmark_max_drawdown=bench_dd,
        beats_sweep=beats_sweep,
        beats_dca=net > dca_pnl if dca_pnl is not None else None,
        worst_loss_below_premium=worst_ok,
        drawdown_ok=drawdown_ok,
        no_breaches=no_breaches,
        has_min_months=has_months,
        has_min_trades=has_trades,
        no_unscored=n_unscored == 0,
        n_ex_dividend_risk=n_ex_div,
        n_deep_itm_risk=n_deep,
        n_early_assignment_risk=n_risk,
        passes=(has_trades and has_months and beats_sweep and worst_ok and drawdown_ok and no_breaches
                and n_unscored == 0),
        breach_flags=breaches,
    )


# --- append-only store -----------------------------------------------------------------------

_PROV = ("inputs_known_at TIMESTAMP NOT NULL, content_hash VARCHAR NOT NULL, source VARCHAR NOT NULL, "
         "as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS csp_shadow_entries (
    entry_id VARCHAR NOT NULL, account_alias VARCHAR NOT NULL, session DATE NOT NULL, ticker VARCHAR NOT NULL,
    option_id VARCHAR NOT NULL, expiration_date DATE NOT NULL, dte INTEGER NOT NULL,
    strike DOUBLE NOT NULL, bid DOUBLE NOT NULL, ask DOUBLE NOT NULL, mid DOUBLE NOT NULL, spread_pct DOUBLE NOT NULL,
    delta DOUBLE NOT NULL, implied_volatility DOUBLE, quote_at TIMESTAMP NOT NULL,
    underlying_price DOUBLE NOT NULL, underlying_quote_at TIMESTAMP NOT NULL, ladder_buy_price DOUBLE NOT NULL,
    contract_multiplier INTEGER NOT NULL, collateral DOUBLE NOT NULL, sector VARCHAR NOT NULL,
    -- Entered at the bid, enforced by the schema as well as by entry_from_suggestion.
    entry_price DOUBLE NOT NULL CHECK (entry_price = bid), premium_dollars DOUBLE NOT NULL,
    {_PROV},
    PRIMARY KEY (entry_id, content_hash)
);
CREATE TABLE IF NOT EXISTS csp_shadow_settlements (
    entry_id VARCHAR NOT NULL, ticker VARCHAR NOT NULL, entry_session DATE, expiration_date DATE,
    status VARCHAR NOT NULL CHECK (status IN ('expired_otm', 'assigned', 'closed_early', 'unscored')),
    settled_on DATE, underlying_close DOUBLE, exit_ask DOUBLE, assigned BOOLEAN NOT NULL,
    effective_cost_basis DOUBLE, collateral DOUBLE, premium_dollars DOUBLE, pnl DOUBLE, return_on_collateral DOUBLE,
    days_held INTEGER, flags VARCHAR[] NOT NULL, unscored_reason VARCHAR,
    {_PROV},
    PRIMARY KEY (entry_id, content_hash)
);
"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_csp_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the shadow-log tables if absent. Idempotent; never touches existing rows or the
    ledger's own tables (store.py owns those)."""
    con.execute(_SCHEMA)


def _to_db(value: Any) -> Any:
    if isinstance(value, datetime):
        return to_utc_naive(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def _insert(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    cols = list(rows[0])
    quoted = ", ".join(f'"{c}"' for c in cols)
    marks = ", ".join("?" for _ in cols)
    con.execute("BEGIN TRANSACTION")
    try:
        before = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # type: ignore[index]
        con.executemany(f"INSERT INTO {table} ({quoted}) VALUES ({marks}) ON CONFLICT DO NOTHING",
                        [[_to_db(r[c]) for c in cols] for r in rows])
        after = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # type: ignore[index]
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise
    return int(after - before)


def _provenance(*, source: str, run_id: str, as_of: datetime, clock: Callable[[], datetime]) -> dict[str, Any]:
    if not isinstance(source, str) or not source.strip() or not isinstance(run_id, str) or not run_id.strip():
        raise LedgerValidationError("source and run_id must be non-empty strings")
    return {"source": source, "as_of": as_of, "ingested_at": clock(), "run_id": run_id}


def record_shadow_entries(
    con: duckdb.DuckDBPyConnection,
    suggestions: Iterable[ShadowSuggestion],
    *,
    run_id: str,
    as_of: datetime,
    source: str = SOURCE,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Append one shadow entry (at the BID) per suggestion; returns rows actually inserted.
    Identical re-records are dropped by content hash. The whole batch is validated first."""
    as_of = _require_aware(as_of, "as_of")
    items = list(suggestions)
    if not all(isinstance(s, ShadowSuggestion) for s in items):
        raise LedgerValidationError("record_shadow_entries takes ShadowSuggestion values from select_candidates")
    if any(as_of < s.as_of for s in items):
        # A row stamped before the quotes it was built from would be visible to a reader asking
        # about a moment before those quotes existed (same rule as the ledger's outcomes).
        raise LedgerValidationError("as_of must not be earlier than the suggestion's as_of")
    ensure_csp_schema(con)
    prov = _provenance(source=source, run_id=run_id, as_of=as_of, clock=clock)
    rows = []
    for s in items:
        entry = dataclasses.asdict(entry_from_suggestion(s))
        entry["content_hash"] = _hash([entry[k] for k in sorted(entry)])
        rows.append({**entry, **prov})
    return _insert(con, "csp_shadow_entries", rows)


def record_settlements(
    con: duckdb.DuckDBPyConnection,
    settlements: Iterable[Settlement],
    *,
    run_id: str,
    as_of: datetime,
    source: str = SOURCE,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Append settled and unscored results (``open`` rows are not results and are skipped).
    A later, better-informed settlement of the same entry is a new row; readers take it."""
    as_of = _require_aware(as_of, "as_of")
    items = [s for s in settlements if isinstance(s, Settlement) and s.status in SETTLEMENT_STATUSES]
    if any(as_of < s.inputs_known_at for s in items):
        raise LedgerValidationError("as_of must not be earlier than the settlement's inputs_known_at")
    if any(not s.entry_id for s in items):
        raise LedgerValidationError("settlement without an entry_id")
    ensure_csp_schema(con)
    prov = _provenance(source=source, run_id=run_id, as_of=as_of, clock=clock)
    rows = []
    for s in items:
        row = dataclasses.asdict(s)
        row["flags"] = list(s.flags)
        row["content_hash"] = _hash([row[k] for k in sorted(row) if k != "inputs_known_at"])
        rows.append({**row, **prov})
    return _insert(con, "csp_shadow_settlements", rows)


def _from_db(value: Any) -> Any:
    return value.replace(tzinfo=timezone.utc) if isinstance(value, datetime) else value


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    cursor = con.execute(sql, list(params))
    names = [d[0] for d in cursor.description]
    return [{k: _from_db(v) for k, v in zip(names, row, strict=True)} for row in cursor.fetchall()]


def _known(known_at: datetime | None) -> datetime:
    return FAR_FUTURE if known_at is None else to_utc_naive(known_at)


_LATEST_ENTRIES = (
    "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY entry_id "
    "ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn FROM csp_shadow_entries WHERE as_of <= ?) "
    "WHERE rn = 1"
)
# Like the ledger's outcomes: visible only if both written and built from data known by the
# reader's moment. A SCORED row always beats an unscored one (review of #2105: a re-run during
# a price-feed outage wrote a newer "unscored" row that hid an assigned loser from the
# scorecard). Among scored rows the one built from the newest information wins, so a real
# correction (better data, new hash) still lands; an unscored row only ever replaces another
# unscored row.
_LATEST_SETTLEMENTS = (
    "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY entry_id "
    "ORDER BY (status <> 'unscored') DESC, inputs_known_at DESC, as_of DESC, ingested_at DESC, content_hash) AS rn "
    "FROM csp_shadow_settlements WHERE as_of <= ? AND inputs_known_at <= ?) WHERE rn = 1"
)


def shadow_entries(con: duckdb.DuckDBPyConnection, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
    ensure_csp_schema(con)
    return _rows(con, _LATEST_ENTRIES + " ORDER BY session, ticker, entry_id", [_known(known_at)])


def shadow_settlements(con: duckdb.DuckDBPyConnection, *, known_at: datetime | None = None) -> list[dict[str, Any]]:
    ensure_csp_schema(con)
    k = _known(known_at)
    rows = _rows(con, _LATEST_SETTLEMENTS + " ORDER BY settled_on NULLS LAST, entry_id", [k, k])
    for r in rows:
        r["flags"] = tuple(r["flags"] or ())
    return rows


def open_short_puts(
    con: duckdb.DuckDBPyConnection,
    as_of: datetime,
    *,
    known_at: datetime | None = None,
    account_alias: str | None = None,
) -> list[dict[str, Any]]:
    """Shadow puts open on ``as_of``'s session, as known at ``known_at`` (default ``as_of``).

    Open = entered on or before the session, expiring on or after it (expiry day counts: an
    assignment happens after that close), and not closed early BEFORE the session. The
    tax-loss eligibility check treats each of these as blocking its ticker (ADR decision 6).
    """
    as_of = _require_aware(as_of, "as_of")
    session = _session_of(as_of)
    k = _known(as_of if known_at is None else known_at)
    ensure_csp_schema(con)
    sql = (
        f"SELECT e.* FROM ({_LATEST_ENTRIES}) AS e LEFT JOIN ({_LATEST_SETTLEMENTS}) AS s USING (entry_id) "
        "WHERE e.session <= ? AND e.expiration_date >= ? "
        "AND NOT coalesce(s.status = 'closed_early' AND s.settled_on < ?, false) "
        "AND (? IS NULL OR e.account_alias = ?) ORDER BY e.ticker, e.entry_id"
    )
    return _rows(con, sql, [k, k, k, session, session, session, account_alias, account_alias])


def tax_loss_blocked_tickers(
    con: duckdb.DuckDBPyConnection, as_of: datetime, *, known_at: datetime | None = None
) -> set[str]:
    """Tickers whose tax-loss eligibility an open shadow put blocks on ``as_of``'s session."""
    return {r["ticker"] for r in open_short_puts(con, as_of, known_at=known_at)}
