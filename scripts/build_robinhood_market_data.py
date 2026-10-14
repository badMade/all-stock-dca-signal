"""Build the intraday market-data payload from raw Robinhood MCP responses.

Inputs are the JSON responses of get_equity_historicals (5minute and day) and
get_equity_quotes, saved as-is; nothing is transcribed by hand. Output matches the
payload documented in docs/intraday_signal_entry.md. Pure function, no I/O, no network.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SOURCE = "robinhood"


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ET)


def _pos(value) -> float:
    x = float(value)
    if not math.isfinite(x) or x <= 0:
        raise ValueError(f"non-positive or non-finite value: {value!r}")
    return x


def _bar(raw: dict) -> dict:
    return {
        "timestamp": _ts(raw["begins_at"]).isoformat(),
        "open": _pos(raw["open_price"]),
        "high": _pos(raw["high_price"]),
        "low": _pos(raw["low_price"]),
        "close": _pos(raw["close_price"]),
        "volume": raw.get("volume", 0),
    }


def _bars_by_symbol(responses: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for resp in responses:
        for res in resp.get("data", {}).get("results", []):
            out.setdefault(res["symbol"], []).extend(res.get("bars", []))
    return out


def _quotes_by_symbol(responses: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for resp in responses:
        for res in resp.get("data", {}).get("results", []):
            q = res.get("quote", {})
            if "symbol" in q:
                out[q["symbol"]] = q
    return out


def build_payload(*, intraday: list[dict], daily: list[dict], quotes: list[dict],
                  trading_day: str, requested: list[str] | None = None) -> dict:
    day = date.fromisoformat(trading_day)
    intra, dly, qts = _bars_by_symbol(intraday), _bars_by_symbol(daily), _quotes_by_symbol(quotes)
    symbols = sorted(set(requested or []) | set(intra) | set(dly) | set(qts))
    data: dict[str, dict] = {}
    errors: list[str] = []
    for sym in symbols:
        try:
            bars = [_bar(b) for b in intra.get(sym, []) if not b.get("interpolated")]
            prior = [b for b in dly.get(sym, [])
                     if _ts(b["begins_at"]).date() < day and not b.get("interpolated")]
            q = qts[sym]
            if not bars or not prior:
                raise ValueError("missing bars")
            data[sym] = {
                "previous_close_bar": _bar(max(prior, key=lambda b: b["begins_at"])),
                "intraday_bars": bars,
                "quote": {"price": _pos(q["last_trade_price"]),
                          "as_of": _ts(q["venue_last_trade_time"]).isoformat()},
            }
        except (KeyError, ValueError, TypeError):
            errors.append(sym)
    return {
        "sources": {SOURCE: data},
        "source_errors": [] if data else [SOURCE],
        "ticker_errors": {SOURCE: errors},
    }
