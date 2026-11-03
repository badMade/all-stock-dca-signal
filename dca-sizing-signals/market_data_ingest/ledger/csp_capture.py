"""Daily option-chain CAPTURE for the cash-secured put shadow log: research only, never trades.

The missing half of PR #2105 (ADR docs/adr-csp-shadow-log.md, PR #2103). ``csp_shadow``'s
``select_candidates`` takes put quotes in a clean row shape; this module turns the READ-ONLY
Robinhood responses into those rows and keeps an append-only, point-in-time copy of what the
broker showed, so every later shadow decision can be replayed from the same inputs.

How a day runs (the scheduled task, proposed 4:20 PM ET on the Mac Studio, after the 4:15 PM
option close; see PR #2111 for the timing evidence):

1. The agent session calls the read-only tools (get_option_chains, get_equity_quotes,
   get_option_instruments, get_option_quotes) and saves the responses as one BUNDLE file in
   the shape of tests/ledger/fixtures/rh_option_chains_2026-11-02.json.
   ``expirations_in_window`` and ``quote_plan`` say which expirations and strikes to fetch, in
   batches the quote tool can answer with official closes (20 ids or fewer).
2. ``load_bundle`` -> ``capture_from_bundle`` -> ``record_capture`` stores the snapshot.
3. ``select_candidates`` (csp_shadow) reads ``CaptureResult.chain_rows`` and
   ``underlying_quotes`` as they are.

What this module is NOT allowed to do: fetch anything (no network import, no tool name), send,
preview, review or withdraw any order (tests/ledger/test_csp_capture.py greps this file the
same way test_csp_shadow_no_orders.py greps csp_shadow.py), or change DCA sizing. Nothing in the
live paths imports it.

Fail closed, per contract: a missing, zero, crossed, malformed, future-dated or ambiguous
quote drops THAT contract with a reason in ``dropped``; it is never estimated, and data
problems never raise. Only a bundle that is not a bundle at all raises ``CaptureBundleError``.

Store rules are the ledger's: append-only (no row-mutating SQL anywhere in this package --
test_ledger_schema.py scans it), a ``content_hash`` in the primary key with ``ON CONFLICT DO
NOTHING``, provenance on every row, and a reader that answers "what was known then".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from ..prices.store import to_utc_naive
from .csp_shadow import (
    _EPS,
    DEFAULT_CONFIG,
    _day,
    _insert,
    _known,
    _moment,
    _num,
    _rows,
    _session_of,
    _ticker,
    _utc_now,
)
from .errors import LedgerValidationError

SOURCE = "csp_chain_capture"
REQUIRED_SECTIONS = ("captured_at", "chains", "instruments", "option_quotes", "equity_quotes")


class CaptureBundleError(LedgerValidationError):
    """The bundle file is not a usable bundle (not JSON, not an object, a duplicate key, a
    missing section, or a capture time without a zone). Nothing from it is used."""


@dataclass(frozen=True)
class CaptureConfig:
    """What to fetch. The DTE window defaults to csp_shadow's own, so the capture never asks
    for a different window than the selector accepts (test_capture_window_defaults_...)."""

    min_dte: int = DEFAULT_CONFIG.min_dte
    max_dte: int = DEFAULT_CONFIG.max_dte
    # Strikes to quote, as a fraction of the underlying's price. The ADR's -0.30..-0.15 delta
    # band sat between 86% and 93% of spot on the 2026-11-02 fixture (GLW 105/110, QQQ
    # 640/650); 70%..100% leaves room for high-volatility names without quoting the deep
    # in-the-money strikes nobody would sell. Proposed, not operator-approved.
    strike_band: tuple[float, float] = (0.70, 1.00)
    # get_option_quotes drops the official closes above 20 ids per call.
    max_quote_batch: int = 20


DEFAULT_CAPTURE = CaptureConfig()


@dataclass(frozen=True)
class CaptureResult:
    session: date
    as_of: datetime
    chain_rows: tuple[dict[str, Any], ...]
    # ticker -> {"price", "quote_at"}: the shape select_candidates reads.
    underlying_quotes: Mapping[str, Mapping[str, Any]]
    # (ticker, option_id or "*", reason) for every contract or underlying left out.
    dropped: tuple[tuple[str, str, str], ...] = field(default_factory=tuple)


# --- the bundle -----------------------------------------------------------------------------


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            # Same rule as dca_config: two values for one key is ambiguous, not "last wins".
            raise CaptureBundleError(f"duplicate key {key!r} in bundle")
        out[key] = value
    return out


def load_bundle(path: Path | str) -> dict[str, Any]:
    """Read and shape-check one bundle file. Raises CaptureBundleError; returns the dict."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_no_duplicate_keys)
    except CaptureBundleError:
        raise
    except (OSError, ValueError) as exc:
        raise CaptureBundleError(f"bundle is not readable JSON: {type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise CaptureBundleError("bundle must be a JSON object")
    missing = [s for s in REQUIRED_SECTIONS if s not in raw]
    if missing:
        raise CaptureBundleError(f"bundle is missing {missing}")
    _capture_time(raw)
    for name, kind in (("chains", dict), ("instruments", dict), ("option_quotes", list), ("equity_quotes", list)):
        if not isinstance(raw[name], kind):
            raise CaptureBundleError(f"bundle section {name!r} must be a JSON {kind.__name__}")
    return raw


def _capture_time(bundle: Mapping[str, Any]) -> datetime:
    moment = _moment(bundle.get("captured_at"))
    if moment is None:
        raise CaptureBundleError("captured_at must be an ISO 8601 time with an offset")
    return moment


# --- what to fetch --------------------------------------------------------------------------


def expirations_in_window(
    chain: Mapping[str, Any], session: date, config: CaptureConfig = DEFAULT_CAPTURE
) -> tuple[date, ...]:
    """The chain's expirations ``min_dte``..``max_dte`` calendar days after ``session``.
    A chain that cannot open positions has none (a put nobody can sell is not a candidate)."""
    if not isinstance(chain, Mapping) or chain.get("can_open_position") is not True:
        return ()
    dates = chain.get("expiration_dates")
    if not isinstance(dates, list):
        return ()
    days = (_day(x) for x in dates)
    out = {d for d in days if d is not None and config.min_dte <= (d - session).days <= config.max_dte}
    return tuple(sorted(out))


def quote_plan(
    instruments_by_ticker: Mapping[str, Iterable[Mapping[str, Any]]],
    spot_by_ticker: Mapping[str, object],
    config: CaptureConfig = DEFAULT_CAPTURE,
) -> list[list[str]]:
    """Instrument ids to quote, in batches of at most ``max_quote_batch``: put strikes between
    ``strike_band`` x spot. A ticker without a usable spot is skipped (nothing to anchor on)."""
    lo, hi = config.strike_band
    ids: list[str] = []
    seen: set[str] = set()
    for ticker in sorted(instruments_by_ticker):
        spot = _num(spot_by_ticker.get(ticker))
        if spot is None or spot <= 0:
            continue
        for inst in instruments_by_ticker[ticker] or ():
            if not isinstance(inst, Mapping) or str(inst.get("type", "")).lower() != "put":
                continue
            strike, iid = _num(inst.get("strike_price")), inst.get("id")
            if strike is None or not isinstance(iid, str) or not iid or iid in seen:
                continue
            if lo * spot - _EPS <= strike <= hi * spot + _EPS:
                seen.add(iid)
                ids.append(iid)
    size = max(1, int(config.max_quote_batch))
    return [ids[i:i + size] for i in range(0, len(ids), size)]


# --- the underlying -------------------------------------------------------------------------


def underlying_quote(result: Mapping[str, Any]) -> dict[str, Any] | None:
    """{"price", "quote_at"} from one get_equity_quotes result, or None.

    The REGULAR-session last trade only: option quotes are regular-session closes, so an
    after-hours print (``last_non_reg_trade_price``) would compare a 6 PM stock price with a
    4 PM put. A halted, untraded, zero or unparseable quote is None, never a guess.
    """
    quote = result.get("quote") if isinstance(result, Mapping) else None
    if not isinstance(quote, Mapping) or quote.get("state") != "active" or quote.get("has_traded") is not True:
        return None
    price, at = _num(quote.get("last_trade_price")), _moment(quote.get("venue_last_trade_time"))
    if price is None or price <= 0 or at is None:
        return None
    return {"price": price, "quote_at": at}


# --- normalising ----------------------------------------------------------------------------


def _instrument_problem(ticker: str, inst: Mapping[str, Any]) -> str | None:
    if _ticker(inst.get("chain_symbol")) != ticker:
        return "symbol_mismatch"
    if str(inst.get("type", "")).lower() != "put":
        return "not_a_put"
    if inst.get("state") != "active":
        return "not_active"
    if inst.get("tradability") != "tradable":
        return "not_tradable"
    if _num(inst.get("trade_value_multiplier")) != 100.0:
        # An adjusted contract (after a split or merger) does not deliver 100 plain shares,
        # so its collateral and assignment would be wrong if modelled as standard.
        return "nonstandard_multiplier"
    strike, expiry = _num(inst.get("strike_price")), _day(inst.get("expiration_date"))
    if strike is None or strike <= 0 or expiry is None:
        return "invalid_instrument"
    return None


def _quote_row(ticker: str, iid: str, inst: Mapping[str, Any], q: Mapping[str, Any]) -> tuple[dict | None, str]:
    bid, ask, delta = (_num(q.get(k)) for k in ("bid_price", "ask_price", "delta"))
    quote_at = _moment(q.get("updated_at"))
    iv_raw = q.get("implied_volatility")
    iv = None if iv_raw is None else _num(iv_raw)
    if None in (bid, ask, delta, quote_at) or (iv_raw is not None and iv is None):
        return None, "invalid_quote"
    if bid <= 0:  # type: ignore[operator]
        return None, "no_bid"  # the shadow log sells at the bid; no bid, no trade
    if bid > ask + _EPS:  # type: ignore[operator]
        return None, "crossed_market"
    return {
        "ticker": ticker,
        "option_type": "put",
        "option_id": iid,
        "expiration_date": _day(inst.get("expiration_date")),
        "strike": _num(inst.get("strike_price")),
        "bid": bid,
        "ask": ask,
        "delta": delta,
        "implied_volatility": iv,
        "quote_at": quote_at,
        "contract_multiplier": 100,
    }, ""


def normalize_chain_rows(
    instruments_by_ticker: Mapping[str, Iterable[Mapping[str, Any]]],
    option_quotes: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], tuple[tuple[str, str, str], ...]]:
    """Join instruments with their quotes into select_candidates rows.

    Returns (rows sorted by ticker, expiration, strike; dropped as (ticker, option_id, reason)).
    """
    dropped: set[tuple[str, str, str]] = set()
    instruments: dict[str, tuple[str, Mapping[str, Any]]] = {}
    for key, rows in instruments_by_ticker.items():
        ticker = _ticker(key)
        for inst in rows or ():
            iid = inst.get("id") if isinstance(inst, Mapping) else None
            if ticker and isinstance(iid, str) and iid:
                instruments[iid] = (ticker, inst)

    quotes: dict[str, Mapping[str, Any]] = {}
    ambiguous: set[str] = set()
    for result in option_quotes:
        q = result.get("quote") if isinstance(result, Mapping) else None
        iid = q.get("instrument_id") if isinstance(q, Mapping) else None
        if not isinstance(iid, str) or not iid:
            dropped.add(("?", "?", "invalid_quote"))
            continue
        if iid not in instruments:
            dropped.add(("?", iid, "unknown_instrument"))
            continue
        if iid in quotes and quotes[iid] != q:
            ambiguous.add(iid)  # two different quotes for one contract: which did the broker show?
        quotes[iid] = q  # type: ignore[assignment]

    out: list[dict[str, Any]] = []
    for iid, (ticker, inst) in instruments.items():
        problem = _instrument_problem(ticker, inst)
        if problem is None and iid in ambiguous:
            problem = "ambiguous_quote"
        if problem is None and iid not in quotes:
            problem = "missing_quote"
        row = None
        if problem is None:
            row, problem = _quote_row(ticker, iid, inst, quotes[iid])
        if row is None:
            dropped.add((ticker, iid, problem or "invalid_quote"))
            continue
        out.append(row)
    out.sort(key=lambda r: (r["ticker"], r["expiration_date"], r["strike"], r["option_id"]))
    return out, tuple(sorted(dropped))


# --- the whole capture ----------------------------------------------------------------------


def capture_from_bundle(bundle: Mapping[str, Any]) -> CaptureResult:
    """Everything one session's capture yields, as of the bundle's ``captured_at``.

    A quote or underlying stamped after ``captured_at`` is lookahead and is dropped; one from
    an earlier session is stale and dropped too (select_candidates would refuse both anyway,
    but the snapshot must only ever hold what was current when it was taken).
    """
    as_of = _capture_time(bundle)
    session = _session_of(as_of)
    rows, dropped_list = normalize_chain_rows(bundle.get("instruments") or {}, bundle.get("option_quotes") or ())
    dropped = set(dropped_list)

    kept: list[dict[str, Any]] = []
    for row in rows:
        if row["quote_at"] > as_of:
            dropped.add((row["ticker"], row["option_id"], "future_quote"))
        elif _session_of(row["quote_at"]) < session:
            dropped.add((row["ticker"], row["option_id"], "stale_quote"))
        else:
            kept.append(row)

    underlying: dict[str, dict[str, Any]] = {}
    for result in bundle.get("equity_quotes") or ():
        q = result.get("quote") if isinstance(result, Mapping) else None
        ticker = _ticker(q.get("symbol")) if isinstance(q, Mapping) else None
        if not ticker:
            continue
        parsed = underlying_quote(result)
        if parsed is None:
            dropped.add((ticker, "*", "invalid_underlying"))
        elif parsed["quote_at"] > as_of or _session_of(parsed["quote_at"]) < session:
            dropped.add((ticker, "*", "stale_underlying"))
        elif ticker in underlying and underlying[ticker] != parsed:
            dropped.add((ticker, "*", "ambiguous_underlying"))
            underlying[ticker] = {}  # marked; removed below
        else:
            underlying[ticker] = parsed
    underlying = {t: q for t, q in underlying.items() if q}
    return CaptureResult(session, as_of, tuple(kept), underlying, tuple(sorted(dropped)))


# --- the append-only snapshot store ---------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS csp_chain_snapshots (
    session DATE NOT NULL, ticker VARCHAR NOT NULL, option_id VARCHAR NOT NULL, expiration_date DATE NOT NULL,
    strike DOUBLE NOT NULL, bid DOUBLE NOT NULL, ask DOUBLE NOT NULL, delta DOUBLE NOT NULL,
    implied_volatility DOUBLE, quote_at TIMESTAMP NOT NULL, contract_multiplier INTEGER NOT NULL,
    underlying_price DOUBLE, underlying_quote_at TIMESTAMP,
    inputs_known_at TIMESTAMP NOT NULL, content_hash VARCHAR NOT NULL, source VARCHAR NOT NULL,
    as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL,
    PRIMARY KEY (session, option_id, content_hash)
);
"""

_DATA_COLUMNS = ("session", "ticker", "option_id", "expiration_date", "strike", "bid", "ask", "delta",
                 "implied_volatility", "quote_at", "contract_multiplier", "underlying_price", "underlying_quote_at")

# Newest version per contract per session, among versions written AND taken by ``known_at``.
_LATEST = (
    "SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY session, option_id "
    "ORDER BY as_of DESC, ingested_at DESC, content_hash) AS rn FROM csp_chain_snapshots "
    "WHERE session = ? AND as_of <= ? AND ingested_at <= ?) WHERE rn = 1 "
    "ORDER BY ticker, expiration_date, strike, option_id"
)


def ensure_capture_schema(con: duckdb.DuckDBPyConnection) -> None:
    """Create the snapshot table if absent. Idempotent; touches no other table."""
    con.execute(_SCHEMA)


def _content_hash(row: Mapping[str, Any]) -> str:
    # The DATA only, not provenance: re-recording the same capture under a new run_id is the
    # same fact and adds nothing; a changed quote is a new version with a new hash.
    values = [row[c].isoformat() if isinstance(row[c], (date, datetime)) else row[c] for c in _DATA_COLUMNS]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def record_capture(
    con: duckdb.DuckDBPyConnection,
    result: CaptureResult,
    *,
    run_id: str,
    clock: Callable[[], datetime] = _utc_now,
) -> int:
    """Append the capture's rows; returns how many were new. One transaction (csp_shadow._insert)."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise LedgerValidationError("run_id must be a non-empty string")
    ensure_capture_schema(con)
    ingested_at = clock()
    to_utc_naive(ingested_at)  # refuses a naive clock before anything is written
    rows: list[dict[str, Any]] = []
    for r in result.chain_rows:
        u = result.underlying_quotes.get(r["ticker"]) or {}
        row = {
            "session": result.session, "ticker": r["ticker"], "option_id": r["option_id"],
            "expiration_date": r["expiration_date"], "strike": r["strike"], "bid": r["bid"], "ask": r["ask"],
            "delta": r["delta"], "implied_volatility": r["implied_volatility"], "quote_at": r["quote_at"],
            "contract_multiplier": r["contract_multiplier"], "underlying_price": u.get("price"),
            "underlying_quote_at": u.get("quote_at"),
        }
        known = max([r["quote_at"]] + ([u["quote_at"]] if u.get("quote_at") else []))
        row.update(inputs_known_at=known, content_hash=_content_hash(row), source=SOURCE, as_of=result.as_of,
                   ingested_at=ingested_at, run_id=run_id)
        rows.append(row)
    return _insert(con, "csp_chain_snapshots", rows)


def captured_rows(
    con: duckdb.DuckDBPyConnection, session: date, *, known_at: datetime | None = None
) -> list[dict[str, Any]]:
    """The snapshot for ``session`` as it was known at ``known_at`` (default: now)."""
    ensure_capture_schema(con)
    k = _known(known_at)
    return _rows(con, _LATEST, [session, k, k])


__all__: Sequence[str] = (
    "CaptureBundleError", "CaptureConfig", "CaptureResult", "DEFAULT_CAPTURE", "SOURCE", "capture_from_bundle",
    "captured_rows", "ensure_capture_schema", "expirations_in_window", "load_bundle", "normalize_chain_rows",
    "quote_plan", "record_capture", "underlying_quote",
)
