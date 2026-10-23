"""Ingest raw Robinhood bars and quotes into the Gate 5 ``--market-data`` payload, verbatim and validated.

Why this exists (2026-10-05, five of seven slots): the live prompts told the executor to "save"
65 tickers of ``get_equity_historicals`` / ``get_equity_quotes`` output into
``market_data_<day>_<slot>.json``, but an MCP tool result lands in the model's context, not on
disk. Every session that could not spill the output to a file re-typed the payload by hand,
skipped the fetch, or fell back to yfinance, and recorded ``robinhood`` in ``source_errors``
without an outage. This command is the one validated path in: the session hands it the raw
tool output (a saved file, or stdin) and it writes exactly the payload the Gate 5 generators
consume, plus an ``ingest`` evidence block with content hashes. It makes no network call and
holds no credentials; the fetch itself still happens in the external agent session.

Usage (every input flag is repeatable and takes one path, or ``-`` for stdin, at most once)::

    python3 -m scripts.ingest_market_data --trading-day 2026-10-05 \\
        --window-start-et 09:30 --window-end-et 10:30 --expected-tickers AAPL,MSFT \\
        --bars-5m bars_a.json --bars-5m bars_b.json --bars-daily daily.json \\
        --quotes quotes.json --out "$ROOT/market_data_2026-10-05_1030.json"

Chunked responses (the live adapter fetches 25 tickers per call) are passed as repeated flags
and merged. Exit 0 prints ``{"ok": true, "tickers_ingested", "tickers_failed",
"payload_sha256", "provenance", "out"}``; every whole-input refusal exits 2, prints
``{"ok": false, "reason", "detail"}`` and writes nothing (an existing ``--out`` is left
untouched).

**Per-ticker problems are never trimmed away.** A ticker whose data fails a check is left out
of ``sources.robinhood``, named in ``ticker_errors.robinhood`` and given a reason in
``ingest.ticker_failures`` (``no_bars``, ``insufficient_bars``, ``bar_timestamp_invalid``,
``bar_outside_window``, ``duplicate_bar_timestamp``, ``bar_spacing_invalid``,
``non_finite_value``, ``bar_ohlc_inconsistent``, ``no_previous_close``, ``no_quote``,
``loader_rejected``). Only when no ticker survives is the whole input refused.

**The checks mirror the Gate 5 loader.** Numeric fields go through
``generate_buy_candidates._finite_float`` (no NaN, Infinity or JSON booleans); the previous
close must be dated the immediately previous *market session* (``_previous_trading_day``,
including the 00:00Z daily-bar stamp the loader accepts); duplicate and too-close bars are
rejected the way ``_StaticMarketData`` and ``_require_bars_match_configured_cadence`` reject
them. As a last gate every emitted row is run through the loader's own ``_parse_bar`` /
``_parse_quote``, so a row this script accepted can never be one the generator aborts on.

**Provenance is derived, never asserted.** ``spilled_file`` when every input was a file;
``stdin_paste`` when any input came from stdin. ``stdin_paste`` is the weaker class: the session
relayed the bytes, and the hash proves only that the validated bytes are the ones used, not
that they equal the broker's response. A value-level paste error that still parses, sits in the
window and has consistent OHLC cannot be detected mechanically.

Standard library only, plus the repo's own loaders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from dca_config import _reject_duplicate_keys
from dca_fileio import atomic_write_text
from dca_live_adapters import _parse_rfc3339, _unwrap
from scripts.generate_buy_candidates import (
    _finite_float,
    _parse_bar,
    _parse_quote,
    _previous_trading_day,
)

TOOL = "scripts.ingest_market_data"
SOURCE_KEY = "robinhood"
ET = ZoneInfo("America/New_York")

#: (role, CLI flag) in the fixed order the evidence block lists inputs.
ROLES: Tuple[Tuple[str, str], ...] = (
    ("bars_5m", "--bars-5m"),
    ("bars_daily", "--bars-daily"),
    ("quotes", "--quotes"),
)

_BAR_PRICE_FIELDS = ("open_price", "high_price", "low_price", "close_price")


class IngestRefused(Exception):
    """An input set that cannot be ingested at all; carries a stable reason code."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class _TickerFailure(Exception):
    """One ticker's data failed a check; its message is the reason code."""


# --- reading and parsing the raw inputs ---------------------------------------------


def _reject_constant(token: str) -> None:
    # json.loads accepts the bare tokens NaN / Infinity / -Infinity by default; a payload
    # carrying one must not parse (the loader would turn it into a silent NO_ACTION).
    raise ValueError(f"non-finite JSON token {token}")


def _loads(text: str) -> Any:
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)


def _parse_response(text: str, name: str) -> Any:
    """Parse one raw tool output, unwrapping MCP's single ``{"type": "text"}`` block."""
    try:
        payload = _loads(text)
        if (
            isinstance(payload, list)
            and len(payload) == 1
            and isinstance(payload[0], dict)
            and payload[0].get("type") == "text"
            and isinstance(payload[0].get("text"), str)
        ):
            payload = _loads(payload[0]["text"])
    except ValueError as exc:
        # A truncated save is the failure mode this script exists for: it must read as
        # unparseable evidence, never as a shorter-but-valid response.
        raise IngestRefused("input_unparseable", f"{name}: {type(exc).__name__}") from exc
    return payload


def _read_input(ref: str) -> Tuple[str, bytes, str]:
    """Return ``(text, raw_bytes, source)`` for one path, or ``-`` for stdin."""
    if ref == "-":
        text = sys.stdin.read()
        return text, text.encode("utf-8"), "stdin"
    try:
        raw = Path(ref).read_bytes()
        return raw.decode("utf-8"), raw, "file"
    except (OSError, UnicodeDecodeError) as exc:
        raise IngestRefused("input_unreadable", f"{ref}: {type(exc).__name__}") from exc


def _result_symbol(role: str, result: Mapping[str, Any]) -> Optional[str]:
    if role == "quotes":
        quote = result.get("quote")
        return quote.get("symbol") if isinstance(quote, Mapping) else None
    return result.get("symbol")


def _collect(files: Mapping[str, Sequence[str]], expected: Sequence[str]) -> Tuple[Dict[str, Dict[str, Mapping]], List[dict], str]:
    """Read, hash and group every input: ``{role: {symbol: result}}``, evidence rows, provenance."""
    for role, _flag in ROLES:
        if not files.get(role):
            raise IngestRefused("missing_input_role", role)
    if sum(ref == "-" for role, _ in ROLES for ref in files[role]) > 1:
        raise IngestRefused("multiple_stdin_inputs", "stdin can supply only one input")
    wanted = set(expected)
    grouped: Dict[str, Dict[str, Mapping]] = {role: {} for role, _ in ROLES}
    evidence: List[dict] = []
    used_stdin = False
    for role, _flag in ROLES:
        for ref in files[role]:
            text, raw, source = _read_input(ref)
            used_stdin = used_stdin or source == "stdin"
            evidence.append(
                {"role": role, "source": source, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
            )
            name = "stdin" if source == "stdin" else ref
            data = _unwrap(_parse_response(text, name))
            results = data.get("results")
            if not isinstance(results, list):
                raise IngestRefused("input_unparseable", f"{name}: no results list")
            if not results:
                # {"results": []} is the broker saying "nothing"; it must not read as a clean
                # response for a universe of tickers.
                raise IngestRefused("empty_response", f"{role}: {name}")
            for result in results:
                if not isinstance(result, Mapping):
                    raise IngestRefused("input_unparseable", f"{name}: result is not an object")
                symbol = _result_symbol(role, result)
                if symbol not in wanted:
                    continue  # unrequested or related symbol: never feed sizing
                if symbol in grouped[role]:
                    # Two answers for one ticker is ambiguous; picking one would be a silent choice.
                    raise IngestRefused("duplicate_symbol", f"{role}: {symbol}")
                grouped[role][symbol] = result
    return grouped, evidence, "stdin_paste" if used_stdin else "spilled_file"


# --- per-ticker validation -----------------------------------------------------------


def _numeric(raw: Any) -> float:
    try:
        return _finite_float(raw, field="value")
    except ValueError as exc:
        raise _TickerFailure("non_finite_value") from exc


def _real_bars(result: Optional[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    bars = (result or {}).get("bars")
    if not isinstance(bars, list):
        return []
    # Interpolated bars are gap-fillers with no trade behind them (the adapter drops them too).
    return [bar for bar in bars if isinstance(bar, Mapping) and not bar.get("interpolated")]


def _bar_row(timestamp: datetime, raw: Mapping[str, Any]) -> Dict[str, Any]:
    open_, high, low, close = (_numeric(raw.get(field)) for field in _BAR_PRICE_FIELDS)
    volume = _numeric(raw.get("volume"))
    if min(open_, high, low, close) <= 0 or high < max(open_, close) or low > min(open_, close) or high < low:
        raise _TickerFailure("bar_ohlc_inconsistent")
    return {
        "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def _intraday_rows(
    result: Optional[Mapping[str, Any]],
    *,
    start: datetime,
    end: datetime,
    interval: timedelta,
    min_bars: int,
) -> List[Dict[str, Any]]:
    real = _real_bars(result)
    if not real:
        raise _TickerFailure("no_bars")
    stamped: List[Tuple[datetime, Mapping[str, Any]]] = []
    for bar in real:
        timestamp = _parse_rfc3339(bar.get("begins_at"))
        if timestamp is None:
            raise _TickerFailure("bar_timestamp_invalid")
        # Inclusive on both ends: the 09:30 opening bar and a healthy bar stamped at the
        # decision time are both kept; a bar outside is a fetch problem, never trimmed.
        if not start <= timestamp.astimezone(ET) <= end:
            raise _TickerFailure("bar_outside_window")
        stamped.append((timestamp, bar))
    stamps = [timestamp for timestamp, _ in stamped]
    if len(set(stamps)) != len(stamps):
        raise _TickerFailure("duplicate_bar_timestamp")
    ordered = sorted(stamps)
    if any(later - earlier < interval for earlier, later in zip(ordered, ordered[1:], strict=False)):
        raise _TickerFailure("bar_spacing_invalid")
    rows = [_bar_row(timestamp, bar) for timestamp, bar in sorted(stamped, key=lambda pair: pair[0])]
    if len(rows) < min_bars:
        raise _TickerFailure("insufficient_bars")
    return rows


def _session_date(timestamp: datetime) -> date:
    """The market session a daily bar belongs to.

    Robinhood stamps a daily bar at 00:00Z of its OWN session date (the live 2026-10-05 payload
    carries ``2026-10-02T00:00:00+00:00`` for the Friday session), which converted to ET reads as
    the evening before. A bar at exactly 00:00Z is therefore dated by its UTC date; anything else
    by its ET date. This is stricter than ``_StaticMarketData``, which converts to ET first and so
    would also accept TODAY's still-forming 00:00Z bar as the previous session.
    """
    utc = timestamp.astimezone(timezone.utc)
    if utc.time() == time(0, 0):
        return utc.date()
    return timestamp.astimezone(ET).date()


def _previous_close_row(result: Optional[Mapping[str, Any]], previous_session: date) -> Dict[str, Any]:
    candidates = []
    for bar in _real_bars(result):
        timestamp = _parse_rfc3339(bar.get("begins_at"))
        if timestamp is not None and _session_date(timestamp) == previous_session:
            candidates.append((timestamp, bar))
    if not candidates:
        # Not "the latest bar before today": the loader requires the immediately previous
        # session, so a stale or weekend-skipping response must fail this ticker.
        raise _TickerFailure("no_previous_close")
    timestamp, bar = max(candidates, key=lambda pair: pair[0])
    return _bar_row(timestamp, bar)


def _quote_row(result: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    quote = (result or {}).get("quote")
    if not isinstance(quote, Mapping):
        raise _TickerFailure("no_quote")
    if not quote.get("has_traded", True) or quote.get("state", "active") != "active":
        raise _TickerFailure("no_quote")
    try:
        price = _finite_float(quote.get("last_trade_price"), field="last_trade_price")
    except ValueError as exc:
        raise _TickerFailure("no_quote") from exc
    as_of = _parse_rfc3339(quote.get("venue_last_trade_time"))
    if price <= 0 or as_of is None:
        raise _TickerFailure("no_quote")
    return {"price": price, "as_of": as_of.astimezone(timezone.utc).isoformat()}


def _loader_accepts(row: Mapping[str, Any]) -> None:
    """Last gate: the shared Gate 5 loader's own parsers must accept what is about to be written."""
    try:
        _parse_bar(row["previous_close_bar"], allow_unknown_low=True)
        for bar in row["intraday_bars"]:
            _parse_bar(bar)
        _parse_quote(row["quote"])
    except (KeyError, TypeError, ValueError) as exc:
        raise _TickerFailure("loader_rejected") from exc


def _ticker_row(
    symbol: str,
    grouped: Mapping[str, Mapping[str, Mapping]],
    *,
    start: datetime,
    end: datetime,
    interval: timedelta,
    min_bars: int,
    previous_session: date,
) -> Dict[str, Any]:
    intraday = _intraday_rows(
        grouped["bars_5m"].get(symbol), start=start, end=end, interval=interval, min_bars=min_bars
    )
    row = {
        "previous_close_bar": _previous_close_row(grouped["bars_daily"].get(symbol), previous_session),
        "intraday_bars": intraday,
        "quote": _quote_row(grouped["quotes"].get(symbol)),
    }
    _loader_accepts(row)
    return row


# --- assembling the payload ----------------------------------------------------------


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validated_day(value: str) -> date:
    day = value.strip()
    try:
        # fromisoformat accepts compact forms on 3.11+; every consumer compares the dashed string.
        if len(day) != 10 or date.fromisoformat(day).isoformat() != day:
            raise ValueError(day)
        return date.fromisoformat(day)
    except ValueError as exc:
        raise IngestRefused("invalid_trading_day", repr(value)) from exc


def _validated_clock(value: str, label: str) -> time:
    try:
        v = value.strip()
        # ⚡ Bolt optimization: Fast-path HH:MM parsing preserving strptime behavior
        parts = v.split(":")
        if len(parts) != 2:
            raise ValueError
        h, m = int(parts[0]), int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError
        return time(h, m)
    except ValueError as exc:
        raise IngestRefused("invalid_window", f"{label} must be HH:MM, got {value!r}") from exc


def ingest(
    files: Mapping[str, Sequence[str]],
    *,
    trading_day: str,
    window_start_et: str,
    window_end_et: str,
    expected_tickers: Sequence[str],
    min_bars: int,
    interval_minutes: float,
) -> Tuple[dict, str]:
    """Return ``(payload, payload_sha256)`` for a validated input set.

    Raises :class:`IngestRefused` for anything that cannot be ingested at all. Per-ticker
    problems do not raise: they land in ``ticker_errors`` and ``ingest.ticker_failures``.
    """
    day = _validated_day(trading_day)
    start_clock = _validated_clock(window_start_et, "--window-start-et")
    end_clock = _validated_clock(window_end_et, "--window-end-et")
    if start_clock >= end_clock:
        raise IngestRefused("invalid_window", f"{window_start_et} is not before {window_end_et}")
    expected = list(dict.fromkeys(symbol.strip() for symbol in expected_tickers if symbol.strip()))
    if not expected:
        raise IngestRefused("no_expected_tickers")
    if min_bars < 1 or interval_minutes <= 0:
        raise IngestRefused("invalid_argument", "--min-bars and --interval-minutes must be positive")

    grouped, evidence, provenance = _collect(files, expected)
    try:
        previous_session = _previous_trading_day(day)
    except ValueError as exc:
        raise IngestRefused("previous_session_unknown", str(exc)) from exc

    start = datetime.combine(day, start_clock, tzinfo=ET)
    end = datetime.combine(day, end_clock, tzinfo=ET)
    sources: Dict[str, Any] = {}
    failures: Dict[str, str] = {}
    for symbol in expected:
        try:
            sources[symbol] = _ticker_row(
                symbol,
                grouped,
                start=start,
                end=end,
                interval=timedelta(minutes=interval_minutes),
                min_bars=min_bars,
                previous_session=previous_session,
            )
        except _TickerFailure as exc:
            failures[symbol] = str(exc)
    if not sources:
        raise IngestRefused("no_tickers_ingested", ",".join(f"{s}:{r}" for s, r in failures.items()))

    digest = hashlib.sha256(_canonical({SOURCE_KEY: sources})).hexdigest()
    payload = {
        "sources": {SOURCE_KEY: sources},
        # Ingest succeeded, so the fetch was attempted and returned data: nothing to record here.
        "source_errors": [],
        "ticker_errors": {SOURCE_KEY: sorted(failures)},
        "ingest": {
            "tool": TOOL,
            "provenance": provenance,
            "trading_day": day.isoformat(),
            "window_et": [start_clock.strftime("%H:%M"), end_clock.strftime("%H:%M")],
            "inputs": evidence,
            "payload_sha256": digest,
            "tickers_expected": expected,
            "tickers_ingested": [symbol for symbol in expected if symbol in sources],
            "ticker_failures": failures,
            "bar_counts": {symbol: len(row["intraday_bars"]) for symbol, row in sources.items()},
        },
    }
    return payload, digest


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trading-day", required=True, help="Trading day, YYYY-MM-DD, America/New_York.")
    parser.add_argument("--window-start-et", required=True, help="Earliest bar start, HH:MM ET, inclusive.")
    parser.add_argument("--window-end-et", required=True, help="Latest bar start, HH:MM ET, inclusive.")
    parser.add_argument("--expected-tickers", required=True, help="Comma-separated universe the slot scores.")
    parser.add_argument("--min-bars", type=int, default=6, help="Minimum bars per ticker (default 6, the trend window).")
    parser.add_argument("--interval-minutes", type=float, default=5, help="Bar cadence; closer bars are rejected.")
    parser.add_argument("--out", required=True, help="Payload path (the generator's --market-data file).")
    for role, flag in ROLES:
        parser.add_argument(flag, action="append", default=[], dest=role, metavar="FILE|-", help=f"Raw {role} output.")
    args = parser.parse_args(argv)

    try:
        payload, digest = ingest(
            {role: getattr(args, role) for role, _ in ROLES},
            trading_day=args.trading_day,
            window_start_et=args.window_start_et,
            window_end_et=args.window_end_et,
            expected_tickers=args.expected_tickers.split(","),
            min_bars=args.min_bars,
            interval_minutes=args.interval_minutes,
        )
        atomic_write_text(args.out, json.dumps(payload, indent=2) + "\n")
    except IngestRefused as exc:
        print(json.dumps({"ok": False, "reason": exc.reason, "detail": exc.detail}))
        return 2
    except OSError as exc:
        print(json.dumps({"ok": False, "reason": "out_unwritable", "detail": type(exc).__name__}))
        return 2

    summary = {
        "ok": True,
        "tickers_ingested": len(payload["ingest"]["tickers_ingested"]),
        "tickers_failed": sorted(payload["ingest"]["ticker_failures"]),
        "payload_sha256": digest,
        "provenance": payload["ingest"]["provenance"],
        "out": args.out,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
