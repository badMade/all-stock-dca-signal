"""Synthetic market data for the backup planner's safety-gate tests.

Two shapes of the same tape, so the assembler and the gate can be tested against each other:

* ``market_data(...)``: the payload the primary's candidate generators read (``sources.robinhood`` etc.).
* ``raw_historicals(...)`` / ``raw_quotes(...)``: what the Robinhood ``get_equity_historicals`` and
  ``get_equity_quotes`` tools return, i.e. what the Perplexity task dumps to a file.

Everything is built for 2026-10-07 (EDT, UTC-4) with a 5-minute bar cadence, the Robinhood interval in
``StrategyConfig.robinhood_interval_minutes``. The generator accepts these as a Robinhood-only BUY when
the ``finance`` source is an explicit empty object; the values were checked against
``scripts.generate_buy_candidates.generate_candidates`` when the tests were written.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DAY = "2026-10-07"
PREV_DAY = "2026-10-06"
BAR_MINUTES = 5

# (last completed bar, quote time) the Perplexity task would have when its backup fires, per slot: the stream
# generators treat a payload as current when a bar or quote falls inside [slot - one bar, slot + evaluation delay].
# Checked against scripts.stream_candidates and scripts.generate_buy_candidates when the tests were written.
SLOT_TIMES = {
    "0935": ((9, 50), (9, 54)),
    "1030": ((10, 45), (10, 49)),
    "1130": ((11, 45), (11, 49)),
    "1230": ((12, 45), (12, 49)),
    "1330": ((13, 45), (13, 49)),
    "1430": ((14, 45), (14, 49)),
    "1530": ((15, 25), (15, 29)),
}


def _stamps(end_hour: int = 15, end_minute: int = 25):
    t = datetime(2026, 10, 7, 9, 30, tzinfo=ET)
    end = datetime(2026, 10, 7, end_hour, end_minute, tzinfo=ET)
    while t <= end:
        yield t
        t += timedelta(minutes=BAR_MINUTES)


def _path(prev: float, last: float, end_hour: int, end_minute: int):
    """(timestamp, price) pairs drifting linearly from the prior close to ``last``."""
    stamps = list(_stamps(end_hour, end_minute))
    steps = max(len(stamps) - 1, 1)
    return [(t, round(prev + (last - prev) * i / steps, 4)) for i, t in enumerate(stamps)]


def snapshot(prev: float, last: float, *, slot: str = "1530") -> dict:
    """One ticker's entry in ``sources.<name>``: previous close bar, intraday bars, and a fresh quote."""
    (end_hour, end_minute), (quote_hour, quote_minute) = SLOT_TIMES[slot]
    return {
        "previous_close_bar": {"timestamp": f"{PREV_DAY}T16:00:00-04:00", "open": prev, "high": prev, "low": 0.0,
                               "close": prev, "volume": 0.0},
        "intraday_bars": [
            {"timestamp": t.isoformat(), "open": p, "high": p, "low": p, "close": p, "volume": 1000}
            for t, p in _path(prev, last, end_hour, end_minute)
        ],
        "quote": {"price": last, "as_of": datetime(2026, 10, 7, quote_hour, quote_minute, tzinfo=ET).isoformat()},
    }


def market_data(tickers: dict[str, tuple[float, float]], *, finance: dict | None = None, slot: str = "1530", **extra) -> dict:
    """The generator payload. ``tickers`` maps symbol -> (prior_close, last_price).

    ``finance`` defaults to an explicit ``{}``: the source is present but resolved nothing, which the
    generator reads as NO_DATA (not an error) and so allows a Robinhood-only BUY.
    """
    return {
        "sources": {"finance": {} if finance is None else finance,
                    "robinhood": {symbol: snapshot(prev, last, slot=slot) for symbol, (prev, last) in tickers.items()}},
        "source_errors": [],
        "ticker_errors": {"finance": [], "robinhood": []},
        **extra,
    }


def _utc(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def raw_historicals(symbol: str, prev: float, last: float, *, interpolated_at: int | None = None, slot: str = "1530") -> dict:
    """A raw ``get_equity_historicals`` response (5-minute bars, regular hours) for one symbol."""
    (end_hour, end_minute), _ = SLOT_TIMES[slot]
    bars = []
    for index, (t, p) in enumerate(_path(prev, last, end_hour, end_minute)):
        bars.append({"begins_at": _utc(t), "open_price": f"{p:.4f}", "high_price": f"{p:.4f}", "low_price": f"{p:.4f}",
                     "close_price": f"{p:.4f}", "volume": 1000, "session": "reg", "interpolated": index == interpolated_at})
    return {"results": [{"symbol": symbol, "interval": "5minute", "bounds": "regular", "bars": bars}]}


def raw_quotes(symbol: str, prev: float, last: float, *, slot: str = "1530") -> dict:
    """A raw ``get_equity_quotes`` response: last trade, and the official prior close under ``close``."""
    _, (quote_hour, quote_minute) = SLOT_TIMES[slot]
    return {"results": [{
        "quote": {"symbol": symbol, "last_trade_price": f"{last:.4f}", "previous_close": f"{prev:.4f}",
                  "previous_close_date": PREV_DAY, "venue_last_trade_time": _utc(datetime(2026, 10, 7, quote_hour, quote_minute, tzinfo=ET)),
                  "has_traded": True, "state": "active"},
        "close": {"price": f"{prev:.4f}", "date": PREV_DAY},
    }]}


def raw_bundle(tickers: dict[str, tuple[float, float]], *, slot: str = "1530") -> dict:
    """What the Perplexity task writes: every raw response it fetched, as two lists."""
    return {
        "historicals": [raw_historicals(s, prev, last, slot=slot) for s, (prev, last) in tickers.items()],
        "quotes": [raw_quotes(s, prev, last, slot=slot) for s, (prev, last) in tickers.items()],
    }
