#!/usr/bin/env python3
"""Pre-built Robinhood market-data payload for the six Robinhood-primary live slots.

Why this exists (2026-10-15): with the yfinance fallback retired (692b5887) the live sessions had to pull
~67 tickers of 5-minute bars through MCP tool output themselves, judged that infeasible inside the slot
window, and the 11:30, 1:30 and 2:30 runs all aborted at Gate 5 with nothing placed. Repo code cannot call
Robinhood (a headless client cannot obtain tokens), so the fetch is done OUTSIDE the slot session by an agent
that already holds Robinhood MCP access -- the Codex CLI on the operator's Mac, driven by launchd
(``deploy/launchd/robinhood_payload.sh``). This module is the deterministic, stdlib-only part around it:

* ``plan``    -- which slot is due, its universe, bar window and output path.
* ``prompt``  -- the fixed instruction text handed to ``codex exec`` for one small chunk of tickers.
* ``merge``   -- validate the per-chunk JSON the agent returned and publish ONE payload in the shape
  ``scripts/stream_candidates.py`` documents (``sources.robinhood`` only; there is still no second source).
* ``broker-prompt`` / ``ingest`` -- the plan-first half (2026-10-26, replaces the retired Claude courier): the prompt for the
  one worker that reads order history and buying power, and the step that turns the published payload plus that reply
  into recommendation files.
* ``check``   -- re-validate a published payload for a slot (used by the live prompts' Gate 5).

The agent is an LLM copying numbers, so nothing it returns is trusted: every bar is range- and
consistency-checked, a quote that disagrees with its own last bar is rejected, and a ticker that fails any
check goes to ``ticker_errors.robinhood`` (a fetch that raised) rather than being scored on doubtful data.
Too little coverage publishes nothing, so the slot aborts at Gate 5 instead of trading a thin universe.

The 3:30 PM slot is Finance-primary today, so it is NOT one of the six scheduled slots: ``due_slot`` (launchd's
``auto``) never selects it and no live prompt reads a 3:30 payload. It can be planned and built on demand
(``plan --slot 1530``, ``deploy/launchd/robinhood_payload.sh 1530``) so the Mac-side build can be shadow-tested before
the 3:30 slot is moved onto it. See docs/robinhood_payload.md, "The 3:30 PM slot (shadow only)".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

#: Robinhood-primary slots -> earliest 5-minute bar the slot's trend validation needs (each prompt's "AT LEAST"
#: line; a later start silently turns qualifying buys into manual_review_candidates).
BAR_START_ET = {
    "0935": time(9, 30),
    "1030": time(9, 30),
    "1130": time(10, 30),
    "1230": time(11, 30),
    "1330": time(12, 30),
    "1430": time(13, 30),
}
SLOTS = tuple(BAR_START_ET)
#: Slots that can be planned and built on demand but are NOT scheduled: ``due_slot`` ignores them, so launchd's
#: ``auto`` never fires one and no live prompt waits on one. 3:30 only needs the lookback bar (decision anchor minus
#: ``trend_lookback_minutes``, measured from the completed 15:25 bar = 14:25) and the decision bar, because ``scripts/generate_buy_candidates`` bypasses the
#: trend filter at 1530ET; the live prompt's "09:30 onward" is wider than the generator's own check and would turn a
#: 5-minute fetch into 72 bars per ticker for no scoring benefit.
SHADOW_BAR_START_ET = {
    "1530": time(14, 25),
}
PLANNABLE_SLOTS = SLOTS + tuple(SHADOW_BAR_START_ET)

#: Tickers per worker call. 4 since 2026-11-02 (was 8): the workers fetched every ticker but answered most of them
#: "full response exceeds output capacity", refusing 0935, 1130, 1230, 1330 and 1530 (15/84). Half the tickers is half
#: the reply one worker has to write back; the compact list bars in ``build_prompt`` cut it again.
DEFAULT_CHUNK_SIZE = 4
#: Share of the universe that must validate before a payload is published at all. It was 1.0 (2026-10-15
#: adversarial review: Gate 6 allocates a binding cap by allocation_priority, so a missing high-priority ticker
#: could hand its budget to lower-priority names). 0.95 since 2026-10-21: one stale thin name (TOPT, QTOP, DXYZ
#: each cost a slot at 89/90 that day) aborted the whole universe, while the daily and weekly caps never got
#: past ~42% / ~13% of their ceilings in the recorded runs, so the reshuffle that justified 1.0 had no room to
#: happen. A failed ticker stays in ticker_errors.robinhood, so Gate 5 still scores it NO_DATA (never a BUY).
#: At 90 tickers this tolerates 4 misses; a stale-feed slot (2026-10-20 13:30: 8 of 90) still publishes nothing.
DEFAULT_MIN_COVERAGE = 0.95
#: Share of the universe that may be "quiet" (no bar in the slot window) before the payload is refused anyway.
#: History: introduced for 2026-10-27 13:30, where eight names printed nothing between 12:30 and 13:30; corrected
#: 2026-10-31, when SPCX, LUNR and DFTX (1-68M shares the session before) showed that was a dropped chunk, not thin
#: names (see ``rescue_targets``). Since 2026-11-03 "quiet" also requires a thin prior session
#: (< ``LIQUID_PRIOR_VOLUME``): a liquid name with no bars is a real failure counted against the coverage floor, so
#: this ceiling only bounds genuinely thin names and was raised from 10% to 25% (the 11-02 probe had 10 of 90 empty).
#: A quiet name is reported under ``ticker_errors.quiet`` (Gate 5 scores it NO_DATA, never a BUY) and leaves the
#: coverage denominator. A feed outage also returns empty series, so more than this share still refuses.
MAX_QUIET_SHARE = 0.25
#: A payload is built one minute after the decision time and read by a session inside the slot's ~30-40-minute
#: window; anything older than this was left by an earlier attempt and is rebuilt or refused, never scored as
#: current (2026-10-15 adversarial review: a retry reused a prior payload with no age bound).
DEFAULT_MAX_AGE_MINUTES = 45
#: Clock skew tolerated between the builder and the checker (same Mac, so seconds; generous on purpose).
FUTURE_SKEW = timedelta(minutes=2)
EXIT_STALE = 6
#: Launchd fires one minute after the decision time (the decision bar's predecessor has closed); a late wake is
#: accepted for up to 14 minutes, after which a payload helps nobody -- the session has already run Gate 5.
AUTO_MIN_AFTER = 1
AUTO_MAX_AFTER = 15
#: A quote further than this from its own last bar close is treated as a transcription error, not a gap move.
MAX_QUOTE_VS_LAST_CLOSE = 0.25
#: Quotes may be stamped after the decision while launchd and the chunk fetch finish; later stamps belong to a
#: re-run, not this slot. A thin ticker's quote may predate the decision, so freshness is checked across the payload.
#: The 30-minute window covers AUTO_MAX_AFTER plus the worst-case chunk rounds (3 x 240 s) of the fetch.
QUOTE_MAX_AFTER_DECISION = timedelta(minutes=30)
#: Clock skew allowed between the worker's quote timestamp and the validating host; beyond it the stamp is future.
QUOTE_FUTURE_SKEW = timedelta(minutes=2)
#: A bar whose close is further than this from the series median close is treated as a transcription error.
MAX_BAR_VS_MEDIAN = 0.5
EXIT_OUTSIDE_WINDOW = 3
EXIT_NOT_TRADING_DAY = 4
EXIT_NO_SLOT_DUE = 5


class PayloadError(ValueError):
    """A ticker's data failed validation; the message becomes the ticker's recorded provider error."""


class QuietTickerError(PayloadError):
    """A ticker printed no bar at all in the slot window: it did not trade, which is not a failed fetch."""


# --------------------------------------------------------------------------- validation


def _parse_ts(value: Any) -> datetime:
    if not isinstance(value, str):
        raise PayloadError(f"timestamp is not a string: {value!r}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError(f"unparseable timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise PayloadError(f"timestamp {value!r} is not timezone-aware")
    return parsed.astimezone(ET)


def _num(value: Any, field: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise PayloadError(f"{field} is not a number: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"{field} is not a number: {value!r}") from exc
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        raise PayloadError(f"{field} out of range: {value!r}")
    return number


#: Field order of a compact list bar, the shape ``build_prompt`` asks workers for (2026-11-02).
COMPACT_BAR_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")


def validate_bar(raw: Any) -> dict[str, Any]:
    """One OHLCV bar with a tz-aware timestamp and a self-consistent range; returns the normalized bar.

    Accepts the object form or the compact ``[timestamp, open, high, low, close, volume]`` list the worker prompt
    asks for since 2026-11-02, and normalizes both to the same object, so ``check`` and the generator never see
    the difference. A list of any other length is refused rather than guessed at.
    """
    if isinstance(raw, list):
        if len(raw) != len(COMPACT_BAR_FIELDS):
            raise PayloadError(f"compact bar needs {len(COMPACT_BAR_FIELDS)} fields, got {len(raw)}: {raw!r}")
        raw = dict(zip(COMPACT_BAR_FIELDS, raw, strict=True))
    if not isinstance(raw, dict):
        raise PayloadError(f"bar is not an object: {raw!r}")
    stamp = _parse_ts(raw.get("timestamp"))
    o, h, low, c = (_num(raw.get(k), k) for k in ("open", "high", "low", "close"))
    volume = _num(raw.get("volume", 0), "volume", allow_zero=True)
    if not (low <= min(o, c) and max(o, c) <= h):
        raise PayloadError(f"bar {stamp.isoformat()} has an inconsistent range (o={o} h={h} l={low} c={c})")
    return {"timestamp": stamp.isoformat(), "open": o, "high": h, "low": low, "close": c, "volume": volume}


def validate_ticker(
    raw: Any, *, day: date, bar_start: time, decision: time, interval: int, now: datetime | None = None
) -> dict[str, Any]:
    """Validate one ticker's ``{previous_close_bar, intraday_bars, quote}``; raises :class:`PayloadError`.

    ``now`` is the validating clock (default: the real ET clock), used only to refuse a future-stamped quote.
    """
    if not isinstance(raw, dict):
        raise PayloadError("ticker entry is not an object")
    if raw.get("error"):
        raise PayloadError(f"agent reported: {str(raw['error'])[:200]}")
    from scripts.generate_buy_candidates import _previous_trading_day
    from scripts.ingest_market_data import _session_date

    prev = validate_bar(raw.get("previous_close_bar"))
    if _session_date(_parse_ts(prev["timestamp"])) != _previous_trading_day(day):
        raise PayloadError("previous_close_bar is not from the immediately prior session")

    start = datetime.combine(day, bar_start, ET)
    anchor = datetime.combine(day, decision, ET)
    by_stamp: dict[datetime, dict[str, Any]] = {}
    raw_bars = raw.get("intraday_bars")
    if not isinstance(raw_bars, list):
        raise PayloadError("intraday_bars is not a list")
    for item in raw_bars:
        bar = validate_bar(item)
        stamp = _parse_ts(bar["timestamp"])
        if stamp.date() != day:
            raise PayloadError(f"intraday bar {bar['timestamp']} is not from {day}")
        # Window: from the slot's trend start up to (not including) the still-open decision bar.
        if start <= stamp < anchor:
            if stamp in by_stamp:
                raise PayloadError(f"duplicate intraday timestamp {stamp.isoformat()}")
            by_stamp[stamp] = bar
    if not by_stamp:
        gap = f"no intraday bars between {bar_start:%H:%M} and {decision:%H:%M} ET"
        # 2026-11-03: quiet means thin. A name that traded LIQUID_PRIOR_VOLUME or more the session before and prints
        # nothing for the window is a dropped series (10-30 AMZN, GOOG, GOOGL; 11-02 10:30 the same), so it is a real
        # failure that counts against the coverage floor, never filed quiet. A missing prior volume reads as 0 (thin).
        if prev["volume"] >= LIQUID_PRIOR_VOLUME:
            raise PayloadError(f"{gap}, although the prior session was liquid ({prev['volume']:,.0f} shares)")
        raise QuietTickerError(gap)
    # The bar AT the window start is required, not one interval after it (2026-10-30). The old `> start + interval`
    # slack let a 3:30 series beginning at 14:30 pass `merge` and `check` against a 14:25 start, but the generator's
    # 60-minute lookback bar is exactly 14:25 (bars before `start` are dropped above, so nothing else can stand in):
    # every such ticker then scored NO_DATA/provider_error behind a payload that had just checked 91/91, which is how
    # the 10-30 slot aborted with an unexplained market_data_unavailable. A late series now fails here, by name, and
    # counts against the coverage floor. Cost: none of the 5,646 published per-ticker series (all seven slots,
    # 2026-10-19..10-30) started anywhere but exactly on their planned start.
    if min(by_stamp) > start:
        raise PayloadError(
            f"intraday bars do not cover the {bar_start:%H:%M} trend-window start "
            f"(first bar {min(by_stamp):%H:%M} ET)"
        )
    bars = [by_stamp[k] for k in sorted(by_stamp)]
    last_stamp = max(by_stamp)
    # The newest completed bar must be near the anchor, or the series stopped early (a truncated copy).
    if anchor - last_stamp > timedelta(minutes=interval):
        raise PayloadError(f"newest intraday bar {last_stamp:%H:%M} is far before the {decision:%H:%M} anchor")
    closes = [b["close"] for b in bars]
    mid = median(closes)
    for bar in bars:
        if abs(bar["close"] - mid) / mid > MAX_BAR_VS_MEDIAN:
            raise PayloadError(f"bar {bar['timestamp']} close {bar['close']} is far from the series median {mid:.4f}")

    quote = raw.get("quote")
    if not isinstance(quote, dict):
        raise PayloadError("quote missing")
    price = _num(quote.get("price"), "quote.price")
    as_of = _parse_ts(quote.get("as_of"))
    if as_of.date() != day:
        raise PayloadError(f"quote as_of {quote.get('as_of')} is not from {day}")
    if as_of < start:
        raise PayloadError(f"quote as_of {as_of:%H:%M} ET predates the {bar_start:%H:%M} trend-window start")
    if as_of > anchor + QUOTE_MAX_AFTER_DECISION:
        raise PayloadError(f"quote as_of {as_of:%H:%M} ET is too long after the {decision:%H:%M} decision")
    if as_of > (now or datetime.now(ET)).astimezone(ET) + QUOTE_FUTURE_SKEW:
        raise PayloadError(f"quote as_of {as_of.isoformat()} is in the future")
    if abs(price - closes[-1]) / closes[-1] > MAX_QUOTE_VS_LAST_CLOSE:
        raise PayloadError(f"quote {price} disagrees with the last bar close {closes[-1]}")
    normalized_quote: dict[str, Any] = {"price": price, "as_of": as_of.isoformat()}
    # Optional two-sided quote (2026-10-26): plan-first Gate 6.7 needs both sides to prove the spread, and a quote without
    # them drops every order as ``spread_unavailable``. Absent keys leave the entry exactly as before, so the live prompts'
    # Gate 5 is unaffected; a PRESENT but doubtful pair fails the ticker rather than being quietly dropped.
    if "bid_price" in quote or "ask_price" in quote:
        bid, ask = _num(quote.get("bid_price"), "quote.bid_price"), _num(quote.get("ask_price"), "quote.ask_price")
        if ask < bid:
            raise PayloadError(f"quote ask {ask} is below its bid {bid}")
        normalized_quote.update(bid_price=bid, ask_price=ask)
    return {
        "previous_close_bar": prev,
        "intraday_bars": bars,
        "quote": normalized_quote,
    }


# --------------------------------------------------------------------------- plan


def _slot_spec(slot: str):
    if slot == "0935":
        from scripts.generate_market_open_candidates import MARKET_OPEN_SPEC as spec
    elif slot == "1030":
        from scripts.generate_morning_candidates import MORNING_SPEC as spec
    elif slot == "1230":
        from scripts.generate_midday_candidates import MIDDAY_SPEC as spec
    else:
        from scripts.generate_intraday_candidates import INTRADAY_SPECS

        spec = INTRADAY_SPECS[slot]
    return spec


def due_slot(day: date, now_et: time) -> str | None:
    """The slot whose decision time passed ``AUTO_MIN_AFTER``..``AUTO_MAX_AFTER`` minutes ago, newest first."""
    now = datetime.combine(day, now_et)
    due = [
        s
        for s in SLOTS
        if datetime.combine(day, time(int(s[:2]), int(s[2:]))) + timedelta(minutes=AUTO_MIN_AFTER)
        <= now
        < datetime.combine(day, time(int(s[:2]), int(s[2:]))) + timedelta(minutes=AUTO_MAX_AFTER)
    ]
    return due[-1] if due else None


def output_path(day: date, slot: str) -> str:
    import dca_paths

    return os.path.join(
        dca_paths.stream_root("robinhood_payload", day.isoformat()), f"robinhood_payload_{day.isoformat()}_{slot}.json"
    )


def _final_buy_universe(config_dir: Path, day: date) -> tuple[list[str], time, int]:
    """(tickers, decision time, Robinhood bar interval) the 1530ET generator scores, loaded the way it loads them.

    ``scripts/generate_buy_candidates`` iterates ``load_strategy_config(...).tickers`` minus
    ``excluded_tickers`` at ``SLOT_SPECS["1530ET"]["decision_time_et"]``, and reads Robinhood bars at
    ``robinhood_interval_minutes``. Reusing those names (not a copy of their values) keeps this plan from drifting.
    """
    import dca_config
    from scripts.generate_buy_candidates import SLOT_SPECS

    config = dca_config.load_strategy_config(config_dir, now=datetime.combine(day, time(15, 30), ET))
    tickers = [s for s in config.tickers if s not in config.excluded_tickers]
    return tickers, SLOT_SPECS["1530ET"]["decision_time_et"], int(config.robinhood_interval_minutes)


def build_plan(slot: str, day: date) -> dict[str, Any]:
    """What the slot's Gate 5 generator scores, exactly as it scores it (same loader and narrowing)."""
    import dca_config
    import dca_runner

    if slot not in PLANNABLE_SLOTS:
        raise SystemExit(f"slot {slot!r} cannot be planned; expected one of {', '.join(PLANNABLE_SLOTS)}")
    config_dir = dca_config.get_config_dir()
    shadow = slot in SHADOW_BAR_START_ET
    if shadow:
        tickers, decision, interval = _final_buy_universe(config_dir, day)
    else:
        spec = _slot_spec(slot)
        config = spec.config_loader(config_dir, allow_unpinned_account=True)
        executor = spec.executor_cls(config=config, mode=spec.live_mode, **spec.executor_kwargs)
        interval = spec.source_interval_minutes or config.interval_minutes
        tickers = [
            s for s in config.tickers if s not in config.excluded_tickers and s not in spec.ticker_source_overrides
        ]
        decision = executor.decision_time
    status = dca_runner.market_day_status(day)
    if status.is_closed or (status.is_early_close and decision >= time(13, 0)):
        raise SystemExit(EXIT_NOT_TRADING_DAY)
    approved = dca_config.load_approved_dates(config_dir).get("approved_test_dates")
    if not approved or day not in approved:
        raise SystemExit(EXIT_OUTSIDE_WINDOW)
    if not tickers:
        raise SystemExit(f"slot {slot} has no tickers to fetch")
    return {
        "slot": slot,
        "trading_day": day.isoformat(),
        "decision_time_et": decision.strftime("%H:%M"),
        "bar_start_et": (SHADOW_BAR_START_ET if shadow else BAR_START_ET)[slot].strftime("%H:%M"),
        "interval_minutes": int(interval),
        "tickers": tickers,
        "out": output_path(day, slot),
        # True when nothing schedules this slot: no launchd entry, no live prompt reads the file.
        "shadow": shadow,
    }


# --------------------------------------------------------------------------- prompt


def _utc_window(plan: dict[str, Any]) -> tuple[str, str]:
    """The plan's ET window as RFC3339 UTC stamps (``YYYY-MM-DDTHH:MM:SSZ``) for the tool's ``start_time``/``end_time``.

    2026-11-02: the clocks went back on 11-01 and every worker kept filtering "10:30-11:30 ET" against UTC stamps as if
    ET were UTC-4, so it returned 09:30-10:25 ET bars and 31/91 tickers read as quiet. The offset is computed here
    with zoneinfo, never left to the model. The same day's 15:30 chunk logs showed the Robinhood MCP
    ``get_equity_historicals`` schema is ``additionalProperties: false`` with a required ``start_time`` and no ``span``,
    so the range is passed as explicit stamps rather than a span name the tool rejects.
    """
    day = date.fromisoformat(plan["trading_day"])
    start = datetime.combine(day, time.fromisoformat(plan["bar_start_et"]), ET).astimezone(timezone.utc)
    end = datetime.combine(day, time.fromisoformat(plan["decision_time_et"]), ET).astimezone(timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ")


#: Calendar days back the previous-session daily bar is requested from: covers a long weekend plus a holiday while
#: staying a handful of bars, so the reply is not truncated.
DAILY_LOOKBACK_DAYS = 7


def _daily_start(plan: dict[str, Any]) -> str:
    day = date.fromisoformat(plan["trading_day"]) - timedelta(days=DAILY_LOOKBACK_DAYS)
    return f"{day.isoformat()}T00:00:00Z"


def build_prompt(plan: dict[str, Any], tickers: list[str]) -> str:
    utc_start, utc_end = _utc_window(plan)
    daily_start = _daily_start(plan)
    return f"""You are a data-retrieval worker. Use ONLY the Robinhood MCP tools that read market data
(historicals and quotes). Never call any order, cancel, watchlist or alert tool.

Trading day: {plan['trading_day']} (America/New_York). Tickers: {', '.join(tickers)}

For EACH ticker fetch:
1. {plan['interval_minutes']}-minute bars (historicals, interval {plan['interval_minutes']}minute) for the window {plan['bar_start_et']} ET
   through {plan['decision_time_et']} ET. Pass exactly start_time {utc_start} and end_time {utc_end} (UTC, computed for today's
   offset; do NOT work the offset out yourself) and no other parameter beyond symbols, interval, start_time and end_time.
   Keep only bars whose START timestamp is at or after {utc_start} and strictly before {utc_end} (omit the still-open
   bar). Copy each kept bar's timestamp exactly as returned.
2. The previous completed regular session's daily bar (historicals, interval day, start_time {daily_start} and no end_time
   -- a handful of sessions only, never a long history, so the reply is not truncated; use the newest completed
   session before {plan['trading_day']}).
3. A current quote, with its best bid and best ask.

Reply with ONE JSON object and nothing else (no prose, no code fence). Keys are the tickers above:
{{"TICKER": {{"previous_close_bar": BAR, "intraday_bars": [BAR, ...], "quote": {{"price": number, "as_of": "<iso with offset>", "bid_price": number, "ask_price": number}}}}}}
where BAR is a list of exactly six values, [timestamp, open, high, low, close, volume], with the timestamp an
"<iso with offset>" string and the rest numbers; no field names, no extra whitespace. This compact form keeps the
whole reply short enough to write in full.

Rules: copy every number and timestamp exactly as the tool returned it; never estimate, round, interpolate or
fill a gap. Never convert a timestamp between time zones and never attach an offset the tool did not return:
if the tool gives a UTC time ("Z" or +00:00), write it back with that same UTC offset. A quote "as_of" is the tool's
own timestamp for that quote, copied verbatim. If a ticker's data cannot be fetched, give {{"error": "<short reason>"}} for that ticker and keep going.
Every requested ticker must appear exactly once. Do not write files; your final message is the whole output."""


# --------------------------------------------------------------------------- broker state (plan-first)


def _account_number() -> str:
    """The configured brokerage account, resolved from ``$ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` (never from a file here)."""
    import dca_config

    configured = dca_config.expand_config_env("${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", source="robinhood payload")
    return dca_config.resolve_broker_account(configured, source="robinhood payload")


def build_broker_prompt(plan: dict[str, Any], account_number: str) -> str:
    """The instruction text for the one worker that reads order history and buying power (read tools only).

    Replaces the retired Claude courier's ``get_equity_orders`` and ``get_portfolio`` calls (2026-10-26). The account
    number is part of the tool arguments, exactly as the live prompts already pass it; this text goes to the worker's
    stdin and is never written to disk by this program.
    """
    from dca_market_history import orders_window_start

    since = orders_window_start(date.fromisoformat(plan["trading_day"])).isoformat()
    return f"""You are a data-retrieval worker. Use ONLY these two read tools: get_equity_orders and get_portfolio.
Never call any order, cancel, watchlist or alert tool.

1. get_equity_orders with account_number "{account_number}" and created_at_gte "{since}". Follow the pagination cursor
   until the tool reports no further page. Keep EVERY page, in the order received, exactly as the tool returned it.
2. get_portfolio with account_number "{account_number}". Keep the response exactly as returned.

Reply with ONE JSON object and nothing else (no prose, no code fence):
{{"order_pages": [PAGE_0, PAGE_1, ...], "portfolio": PORTFOLIO}}
where each PAGE and PORTFOLIO is the tool's full response, copied verbatim. Never summarize, drop, reorder, round or
retype a field. If a page is too large to copy in full, or a tool errors, reply {{"error": "<short reason>"}} instead."""


def load_broker_reply(path: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """``(order_pages, portfolio)`` from the broker worker's reply, each normalised like a saved courier response."""
    from dca_market_history import MarketHistoryStore

    reply = _load_chunk(path)
    if reply.get("error"):
        raise PayloadError(f"broker worker reported: {str(reply['error'])[:200]}")
    pages = reply.get("order_pages")
    if not isinstance(pages, list) or not pages:
        raise PayloadError("broker reply carries no order pages")
    portfolio = reply.get("portfolio")
    normalize = MarketHistoryStore.normalize_response
    return [normalize(page) for page in pages], normalize(portfolio) if isinstance(portfolio, dict) else {}


#: Longest an ingest may trail the payload's own build, and a broker reply may sit from the ingest. The recommendation
#: file is stamped with the payload's build time, so a late ingest must not make old data look fresh (same bound as the
#: retired courier's ``MAX_RUN_AGE_SECONDS``).
MAX_INGEST_AGE = timedelta(minutes=10)


def _quote_sides(entries: dict[str, Any]) -> dict[str, list[float] | None]:
    sides: dict[str, list[float] | None] = {}
    for ticker, entry in entries.items():
        quote = entry.get("quote", {})
        bid, ask = quote.get("bid_price"), quote.get("ask_price")
        sides[ticker.upper()] = [bid, ask] if bid is not None and ask is not None else None
    return sides


def ingest(
    plan: dict[str, Any],
    broker_reply: str,
    *,
    now: datetime | None = None,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
    config_dir: Path | None = None,
) -> int:
    """Turn this slot's published payload plus the broker reply into plan-first recommendation files.

    Exit 0 stored, 2 refused or unusable input, 3 another ingest holds the lease, 6 the payload is stale. Nothing here
    places an order. Everything downstream is stamped with the payload's build time, never the (later) ingest clock.
    """
    import dca_config
    import dca_paths
    from dca_fill_compare import FillComparator
    from dca_market_history import MarketHistoryStore
    from dca_market_sources import SourceUnavailable
    from dca_rec_models import MarketSnapshot
    from dca_rec_publish import build_document, publish_all, publishers_from_settings
    from dca_rec_store import RecommendationStore, load_settings
    from dca_recommender import RecommendationEngine
    from scripts.assemble_order_pages import _order_rows
    from scripts.scheduler_lease import SchedulerLease, window_key

    now = (now or datetime.now(ET)).astimezone(ET)
    status = check(plan, min_coverage=min_coverage, now=now, max_age_minutes=max_age_minutes)
    if status:
        return status
    out = Path(plan["out"])
    day, slot = date.fromisoformat(plan["trading_day"]), plan["slot"]
    try:
        meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(meta["generated_at_et"])
        reply_at = datetime.fromtimestamp(Path(broker_reply).stat().st_mtime, ET)
        if now - generated > MAX_INGEST_AGE:
            raise PayloadError(f"payload built {generated:%H:%M:%S} ET is too old to ingest")
        if abs(now - reply_at) > MAX_INGEST_AGE:
            raise PayloadError(f"broker reply written {reply_at:%H:%M:%S} ET is not within {MAX_INGEST_AGE} of now")
        payload = json.loads(out.read_text(encoding="utf-8"))
        account = _account_number()
        config_dir = config_dir or dca_config.get_config_dir()
        pages, portfolio = load_broker_reply(broker_reply)
        broker = MarketHistoryStore.broker_state(pages, portfolio, trading_day=day, now=generated, account_number=account)
        snapshot = MarketSnapshot(
            day, generated, "robinhood_agent_fetch", payload, _quote_sides(payload["sources"]["robinhood"])
        )
        settings = load_settings(config_dir)
    except (PayloadError, SourceUnavailable, ValueError, OSError, KeyError, TypeError) as exc:
        print(f"robinhood_payload: ingest refused: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    store = RecommendationStore()
    lease = SchedulerLease(
        Path(dca_paths.cron_tracking_root()) / "scheduler_leases", window_key(day.isoformat(), "plan_first", "ingest")
    )
    holder = f"payload-{os.getpid()}-{generated.timestamp()}"
    acquired = lease.acquire(holder)
    if not getattr(acquired, "acquired", acquired):
        print("robinhood_payload: another ingest holds the lease", file=sys.stderr)
        return 3
    try:
        result = RecommendationEngine(config_dir=config_dir).compute(snapshot, broker, generated, slot=slot)
        if result.status == "OK" and result.recommendations and result.expected:
            store.write(result.recommendations, result.expected)
            publish_results = publish_all(
                publishers_from_settings(settings.publish, repo_dir=Path(__file__).resolve().parents[1]),
                build_document({slot: result.recommendations}, generated_at=generated),
            )
            for item in publish_results:
                print(f"robinhood_payload: publish {item.channel} ok={item.ok} {item.detail}", file=sys.stderr)
        comparator = FillComparator(store)
        rows = [row for page in pages for row in _order_rows(page)]
        comparator.write_report(day, comparator.compare(day, rows), generated_at=generated)
        orders = len(result.recommendations.items) if result.recommendations else 0
        print(json.dumps({"status": result.status, "slot": slot, "reasons": list(result.reasons), "orders": orders}))
        return 0 if result.status == "OK" else 2
    finally:
        lease.release(holder)


# --------------------------------------------------------------------------- merge / publish


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``object_pairs_hook`` that raises on a duplicate key in one JSON object, at every nesting level.

    Mirrors ``dca_runner._reject_duplicate_keys`` exactly (as ``dca_config``'s copy does), duplicated rather than
    imported (2026-11-15): ``_load_chunk`` used to import it from ``dca_runner``, which pulls in ``near_close_dca`` and
    costs ~0.3 s of CPU per process. The launcher's rescue pass calls ``_load_chunk`` from two inline ``python -c``
    snippets per chunk (62 fresh interpreters for a 31-chunk plan), so that import alone was ~20 s of every build and
    made the launcher tests in tests/test_robinhood_payload.py time out under ``pytest -n auto`` on the self-hosted
    runner (PR 2328's core CI job, 2026-11-15). tests/test_robinhood_payload.py pins the parity and that the import stays out.
    """
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key {key!r} in JSON document")
        seen.add(key)
        result[key] = value
    return result


def _load_chunk(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8").strip()
    if text.startswith("```"):  # tolerate a fenced reply despite the instruction
        text = text.strip("`")
        text = text[text.index("{") :] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise PayloadError("no JSON object in chunk output")
    loaded = json.loads(text[start : end + 1], object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(loaded, dict):
        raise PayloadError("chunk output is not an object")
    return loaded


#: Prior-session share volume at or above which a ticker that came back with no intraday bars is re-asked for instead
#: of being filed "quiet" (2026-10-31). At 1M shares a day a name averages ~2,500 shares a minute, so an hour with no
#: 5-minute bar at all is a worker that dropped the series, not a quiet hour. The genuinely thin names of the universe
#: sit well below it (QTOP, GFUZ and CHE under 100k; FLKR, DXYZ, IRDM, EVR, TRIN and AAAU 0.3-0.6M, 2026-10-15..10-30)
#: and keep the old behaviour; NVDA, GOOGL, GOOG and AMZN (10-29 15:31: 10-78M) are far above it.
LIQUID_PRIOR_VOLUME = 1_000_000


def _implausibly_quiet(entry: Any) -> bool:
    """True when ``entry`` carries no intraday bars although its prior session traded ``LIQUID_PRIOR_VOLUME`` or more.

    A malformed or missing ``previous_close_bar`` reads as not liquid: this only decides whether to ask again, and a
    ticker that is not re-asked for is still validated (and failed) by the merge exactly as before.
    """
    if not isinstance(entry, dict) or entry.get("error") or entry.get("intraday_bars"):
        return False
    prev = entry.get("previous_close_bar")
    volume = prev.get("volume") if isinstance(prev, dict) else None
    return isinstance(volume, (int, float)) and not isinstance(volume, bool) and volume >= LIQUID_PRIOR_VOLUME


def rescue_targets(reply: dict[str, Any], tickers: list[str]) -> list[str]:
    """The tickers of one chunk the launcher's rescue pass re-asks for, given that chunk's reply (``{}`` if none).

    Absent or per-ticker-error tickers (2026-10-26); every ticker when every answered one has no bars (2026-10-30, the
    whole-chunk case of 10-29 15:31); otherwise also any answered ticker with no bars whose prior session was liquid
    (2026-10-31: the same 10-29 failure with one ticker of the chunk still answered would have filed NVDA, GOOGL, GOOG
    and AMZN quiet again). Only *which tickers are asked for again* changes here: the merge still validates whatever
    comes back, and a liquid name that is empty again is a real failure (``validate_ticker``) counted against the coverage floor.
    """
    need = [t for t in tickers if t not in reply or (isinstance(reply[t], dict) and reply[t].get("error"))]
    answered = [reply[t] for t in tickers if t not in need]
    if answered and all(isinstance(e, dict) and not e.get("intraday_bars") for e in answered):
        return list(tickers)  # every answered ticker came back with no bars: the worker dropped the series
    return [t for t in tickers if t in need or _implausibly_quiet(reply[t])]


# The launchd script writes worker i's reply to chunk_<i>.json, where i indexes plan["chunks"]. The index is the only
# link between a reply and the tickers its prompt asked for, so a file named any other way is not merged.
_CHUNK_FILE = re.compile(r"^chunk_(\d+)\.json$")


def _chunk_assignments(plan: dict[str, Any]) -> list[frozenset[str]]:
    """``plan["chunks"]`` as ticker sets, proven to partition ``plan["tickers"]``; PayloadError otherwise.

    Ownership has to be explicit (Codex adversarial review): without it a worker that returned another chunk's
    ticker silently replaced that ticker's own fetch, and the replacement only had to look plausible to be scored.
    """
    chunks, tickers = plan.get("chunks"), set(plan["tickers"])
    if not isinstance(chunks, list) or not chunks:
        raise PayloadError("plan carries no chunk assignments")
    assigned: set[str] = set()
    result: list[frozenset[str]] = []
    for chunk in chunks:
        if not isinstance(chunk, list) or not all(isinstance(t, str) for t in chunk):
            raise PayloadError("plan chunk is not a list of tickers")
        if assigned & set(chunk):
            raise PayloadError(f"plan assigns {', '.join(sorted(assigned & set(chunk)))} to more than one chunk")
        if set(chunk) - tickers:
            raise PayloadError(f"plan chunk names tickers outside the plan: {', '.join(sorted(set(chunk) - tickers))}")
        assigned |= set(chunk)
        result.append(frozenset(chunk))
    if assigned != tickers:
        raise PayloadError(f"plan leaves {', '.join(sorted(tickers - assigned))} out of every chunk")
    return result


def assemble(plan: dict[str, Any], chunk_paths: list[str]) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate every planned ticker against the chunks; returns (payload, {ticker: reason} for failures).

    Each reply counts only for the tickers its own chunk was assigned. Any other key is dropped and reported, never
    merged, and a ticker that more than one accepted reply carries (the same chunk passed twice, say) fails instead of
    last-write-wins.
    """
    assignments = _chunk_assignments(plan)
    merged: dict[str, Any] = {}
    returned_by: dict[str, list[str]] = {}
    chunk_failures: list[str] = []
    for path in chunk_paths:
        name = os.path.basename(path)
        match = _CHUNK_FILE.match(name)
        if match is None or int(match.group(1)) >= len(assignments):
            chunk_failures.append(f"{name}: not a chunk_<index>.json reply of this plan; not merged")
            continue
        try:
            loaded = _load_chunk(path)
        except (OSError, ValueError) as exc:  # a lost chunk fails its tickers, not the whole run
            chunk_failures.append(f"{name}: {exc}")
            continue
        assigned = assignments[int(match.group(1))]
        unexpected = sorted(str(key) for key in loaded if key not in assigned)
        if unexpected:
            chunk_failures.append(f"{name}: ignored tickers it was not assigned: {', '.join(unexpected)}")
        for ticker in assigned & set(loaded):
            returned_by.setdefault(ticker, []).append(name)
            merged[ticker] = loaded[ticker]
    duplicated = {ticker: names for ticker, names in returned_by.items() if len(names) > 1}
    for line in chunk_failures:
        print(f"robinhood_payload: chunk {line}", file=sys.stderr)
    return _validate_entries(plan, merged, duplicated)


def _require_fresh_fetch(
    good: dict[str, Any], failed: dict[str, str], *, day: date, decision: time, interval: int
) -> dict[str, Any]:
    """Reject the whole payload when no quote in it was traded near the decision time.

    A payload built hours late, or left over from an earlier attempt, passes every per-ticker check. Liquid names
    trade every minute, so the newest ``as_of`` across the payload dates the fetch; if even that predates the
    decision bar, nothing here describes the slot's market. Every ticker is moved to ``failed`` so ``publish`` and
    ``check`` both treat the payload as unavailable.
    """
    if not good:
        return good
    newest = max(_parse_ts(v["quote"]["as_of"]) for v in good.values())
    floor = datetime.combine(day, decision, ET) - timedelta(minutes=interval)
    if newest >= floor:
        return good
    reason = f"stale payload: newest quote {newest.astimezone(ET):%H:%M} ET predates {floor:%H:%M} ET"
    for ticker in good:
        failed[ticker] = reason
    return {}


def _validate_entries(
    plan: dict[str, Any], merged: dict[str, Any], duplicated: dict[str, list[str]] | None = None
) -> tuple[dict[str, Any], dict[str, str]]:
    """Validate each planned ticker's entry; ``assemble`` for worker replies, ``check`` for a published payload."""
    day = date.fromisoformat(plan["trading_day"])
    bar_start = time.fromisoformat(plan["bar_start_et"])
    decision = time.fromisoformat(plan["decision_time_et"])
    interval = int(plan["interval_minutes"])
    duplicated = duplicated or {}
    good: dict[str, Any] = {}
    failed: dict[str, str] = {}
    quiet: set[str] = set()
    for ticker in plan["tickers"]:
        try:
            if ticker in duplicated:
                raise PayloadError(f"returned by more than one chunk reply ({', '.join(duplicated[ticker])})")
            if ticker not in merged:
                raise PayloadError("no data returned for this ticker")
            good[ticker] = validate_ticker(
                merged[ticker], day=day, bar_start=bar_start, decision=decision, interval=interval
            )
        except QuietTickerError as exc:
            failed[ticker] = str(exc)
            quiet.add(ticker)
        except PayloadError as exc:
            failed[ticker] = str(exc)
    good = _require_fresh_fetch(good, failed, day=day, decision=decision, interval=interval)
    ticker_errors: dict[str, list[str]] = {"robinhood": sorted(set(failed) - quiet)}
    # Internal to build/publish: ``publish`` folds these back into ``robinhood`` before writing, because
    # generate_buy_candidates rejects any ticker_errors key that is not a source name.
    if quiet:
        ticker_errors["quiet"] = sorted(quiet)
    payload = {"sources": {"robinhood": good}, "source_errors": [], "ticker_errors": ticker_errors}
    return payload, failed


def _coverage_problem(ok: int, universe: int, quiet: int, min_coverage: float) -> str | None:
    """Why ``ok`` valid tickers of ``universe`` (``quiet`` of them did not trade) cannot be published, or None."""
    if quiet / universe > MAX_QUIET_SHARE:
        return f"{quiet}/{universe} tickers printed no bar in the window, over the {MAX_QUIET_SHARE:.0%} quiet cap"
    traded = universe - quiet
    coverage = ok / traded
    if coverage < min_coverage:
        return f"coverage {ok}/{traded} ({coverage:.0%}) is below {min_coverage:.0%}"
    return None


def _refusal_path(out: Path) -> Path:
    return out.with_suffix(".refused.json")


def _record_refusal(
    plan: dict[str, Any], reason: str, *, ok: int, universe: int, failed: dict[str, str],
    min_coverage: float, now: datetime,
) -> None:
    """Leave a durable record that ``publish`` ran and chose not to publish, and why.

    Without it a slot's Gate 5 sees only "missing" and cannot tell a refusal (89/90 tickers fresh, one thin
    name stale) from a launchd job that never ran. 2026-10-21: the 09:35, 12:30 and 13:30 slots aborted on
    exactly that, and the 13:30 alert blamed a sleeping Mac while the job's own log said it had refused.
    Best-effort: a marker that cannot be written must never change the refusal itself.
    """
    marker = {
        "slot": plan["slot"],
        "trading_day": plan["trading_day"],
        "refused_at_et": now.isoformat(),
        "reason": reason,
        "universe": universe,
        "valid": ok,
        "min_coverage": min_coverage,
        "failed": failed,
    }
    path = _refusal_path(Path(plan["out"]))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marker, indent=1), encoding="utf-8")
    except OSError as exc:
        print(f"robinhood_payload: could not write refusal marker {path.name}: {exc.__class__.__name__}", file=sys.stderr)
    # After the marker, so a notifier that hangs or fails can never cost the durable record.
    _notify_refusal(plan, reason, ok=ok, universe=universe, failed=failed)


#: Failed tickers named in the refusal notification before "+N more"; the log and the marker carry the full list.
NOTIFY_TICKER_LIMIT = 5
#: macOS truncates a long notification body anyway; keep the useful part (reason and counts) well inside it.
NOTIFY_MESSAGE_LIMIT = 250


def _notify_refusal(plan: dict[str, Any], reason: str, *, ok: int, universe: int, failed: dict[str, str]) -> None:
    """Raise a macOS notification that this slot's payload build was refused (2026-11-03).

    2026-11-02: five of seven builds were refused and each only surfaced in a launchd log and at the slot's Gate 5
    abort. Opt-in (``DCA_NOTIFY_REFUSAL=1``, which the launcher exports by default) so the test suite and direct
    callers stay silent. Best-effort like the marker: a missing, failing or hung notifier changes neither the exit
    status nor the marker, and the log note names only the exception class, never the message text. Reuses
    ``schedule_watchdog.notify_macos`` (text as argv, not interpolated into AppleScript) instead of a second notifier.
    """
    if os.environ.get("DCA_NOTIFY_REFUSAL") != "1":
        return
    import subprocess

    from scripts import schedule_watchdog

    names = sorted(failed)
    listed = ", ".join(names[:NOTIFY_TICKER_LIMIT]) or "none"
    if len(names) > NOTIFY_TICKER_LIMIT:
        listed += f" +{len(names) - NOTIFY_TICKER_LIMIT} more"
    message = f"{reason} [{ok}/{universe} valid; failed: {listed}]"[:NOTIFY_MESSAGE_LIMIT]
    try:
        schedule_watchdog.notify_macos(f"Payload refused: slot {plan['slot']} {plan['trading_day']}", message)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"robinhood_payload: could not raise the refusal notification: {exc.__class__.__name__}", file=sys.stderr)


def _refusal_note(plan: dict[str, Any]) -> str | None:
    """One line describing this slot's recorded refusal, or None when there is none for THIS slot and day.

    A marker for another slot or day, or an unreadable one, is ignored: it must never explain (or excuse)
    a different slot's missing payload.
    """
    try:
        marker = json.loads(_refusal_path(Path(plan["out"])).read_text(encoding="utf-8"))
        if marker["slot"] != plan["slot"] or marker["trading_day"] != plan["trading_day"]:
            return None
        stale = ", ".join(f"{t} ({r})" for t, r in sorted(marker["failed"].items())) or "none listed"
        refused_at = datetime.fromisoformat(marker["refused_at_et"])
        return (f"publisher ran and refused at {refused_at:%H:%M} ET: {marker['reason']} "
                f"[{marker['valid']}/{marker['universe']} valid; not valid: {stale}]")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def publish(
    plan: dict[str, Any],
    payload: dict[str, Any],
    failed: dict[str, str],
    *,
    min_coverage: float,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now(ET)
    anchor = datetime.combine(date.fromisoformat(plan["trading_day"]), time.fromisoformat(plan["decision_time_et"]), ET)
    if now < anchor:
        # The decision bar's predecessor has not closed yet: the newest bar would be a still-forming one.
        reason = f"built at {now:%H:%M} ET, before the {anchor:%H:%M} anchor"
        print(f"robinhood_payload: {reason}; publishing nothing", file=sys.stderr)
        _record_refusal(plan, reason, ok=len(payload["sources"]["robinhood"]), universe=len(plan["tickers"]),
                        failed=failed, min_coverage=min_coverage, now=now)
        return 2
    universe = len(plan["tickers"])
    ok = len(payload["sources"]["robinhood"])
    quiet = payload["ticker_errors"].get("quiet", [])
    for ticker, reason in sorted(failed.items()):
        print(f"robinhood_payload: {ticker}: {reason}", file=sys.stderr)
    reason = _coverage_problem(ok, universe, len(quiet), min_coverage)
    if reason:
        print(f"robinhood_payload: {reason}; publishing nothing so the slot aborts at Gate 5", file=sys.stderr)
        _record_refusal(plan, reason, ok=ok, universe=universe, failed=failed, min_coverage=min_coverage, now=now)
        return 2
    out = Path(plan["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    # A quiet name must stay in ticker_errors.robinhood: omitting it would read as "Robinhood does not cover it",
    # which lets reconcile_signals allow a single-source BUY, while an errored name is scored NO_DATA, never a BUY.
    # The sidecar's "quiet" list is how the prompts and ``check`` tell these names from failed fetches.
    payload = {**payload, "ticker_errors": {"robinhood": sorted(failed)}}
    if out.exists():
        print(f"robinhood_payload: {out} already exists; leaving the published payload in place")
        return 0
    meta = {
        "slot": plan["slot"],
        "trading_day": plan["trading_day"],
        "generated_at_et": now.isoformat(),
        "fetched_by": "codex exec via robinhood-trading MCP (scripts/robinhood_payload.py)",
        "universe": universe,
        "valid": ok,
        "quiet": quiet,
        "failed": failed,
    }
    body = json.dumps(payload, indent=1)
    # Binds the sidecar to THIS file's bytes, so a payload paired with a fresher sidecar fails `check`.
    meta["payload_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", dir=out.parent)
    try:
        # newline="": write the exact bytes the sidecar's SHA-256 was taken over. Text mode turns "\n" into "\r\n" on
        # Windows, so `check` read back a different file than `publish` hashed (a no-op on the Mac).
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(body)
        # ln-style no-overwrite publish: a concurrent run that finished first wins, nobody sees a partial file.
        try:
            os.link(tmp, out)
        except FileExistsError:
            print(f"robinhood_payload: {out} appeared during assembly; leaving it in place")
            return 0
    finally:
        os.unlink(tmp)
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    # A payload now exists, so an earlier attempt's refusal is history, not the slot's state.
    _refusal_path(out).unlink(missing_ok=True)
    # A partial payload is allowed above the floor; say which tickers it leaves to ticker_errors so the log shows it.
    skipped = f"; ticker_errors: {', '.join(sorted(failed))}" if failed else ""
    skipped += f" ({len(quiet)} quiet)" if quiet else ""
    print(f"robinhood_payload: wrote {out} ({ok}/{universe} tickers{skipped})")
    return 0


def _freshness_problem(plan: dict[str, Any], out: Path, now: datetime, max_age_minutes: int) -> str | None:
    """Why ``out`` cannot be treated as THIS slot's current payload, or None when it can.

    Uses the sidecar written by ``publish`` (a quote's ``as_of`` cannot stand in: it is the last-trade or
    snapshot time and is legitimately old for an illiquid ticker). Fails closed on a missing or mismatched sidecar.
    """
    meta_path = out.with_suffix(".meta.json")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        generated = datetime.fromisoformat(meta["generated_at_et"])
        digest = meta["payload_sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f"no usable sidecar {meta_path.name} ({exc.__class__.__name__}); cannot prove when it was built"
    if generated.tzinfo is None:
        return "sidecar generated_at_et is not timezone-aware"
    if meta.get("slot") != plan["slot"] or meta.get("trading_day") != plan["trading_day"]:
        return f"sidecar is for {meta.get('trading_day')} {meta.get('slot')}, not {plan['trading_day']} {plan['slot']}"
    if hashlib.sha256(out.read_bytes()).hexdigest() != digest:
        return "payload does not match its sidecar checksum"
    anchor = datetime.combine(date.fromisoformat(plan["trading_day"]), time.fromisoformat(plan["decision_time_et"]), ET)
    if generated < anchor:
        return f"built at {generated:%H:%M} ET, before the {anchor:%H:%M} anchor"
    if generated - now > FUTURE_SKEW:
        return f"built at {generated:%H:%M:%S} ET, which is in the future"
    age = now - generated
    if age > timedelta(minutes=max_age_minutes):
        return f"built {int(age.total_seconds() // 60)} minutes ago, older than the {max_age_minutes}-minute bound"
    return None


def _sidecar_quiet(out: Path) -> list[str]:
    """The quiet tickers ``publish`` recorded beside ``out`` (already bound to the payload by the checksum)."""
    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
    return list(meta.get("quiet", []))


def check(
    plan: dict[str, Any],
    *,
    min_coverage: float,
    now: datetime | None = None,
    max_age_minutes: int = DEFAULT_MAX_AGE_MINUTES,
) -> int:
    """Re-validate the published payload for ``plan``'s slot; non-zero exit means 'treat as unavailable'.

    Exit 6 means specifically 'stale or unprovable age': the launcher rebuilds on it, a session aborts on it.
    """
    out = Path(plan["out"])
    if not out.is_file():
        # Say whether the publisher ran: "missing" alone reads as "job not running" and sends the operator to
        # the wrong place (2026-10-21).
        note = _refusal_note(plan) or "no record that the publisher ran for this slot (job not run, or log only)"
        print(f"robinhood_payload: missing {out}: {note}", file=sys.stderr)
        return 2
    problem = _freshness_problem(plan, out, now or datetime.now(ET), max_age_minutes)
    if problem:
        print(f"robinhood_payload: stale payload {out.name}: {problem}", file=sys.stderr)
        return EXIT_STALE
    payload = json.loads(out.read_text(encoding="utf-8"))
    # Already merged by `merge`, so chunk ownership is behind it: re-run only the per-ticker validation.
    rebuilt, _failed = _validate_entries(plan, payload["sources"]["robinhood"])
    invalid_present = {
        ticker: reason for ticker, reason in _failed.items() if ticker in payload["sources"]["robinhood"]
    }
    if invalid_present:
        for ticker, reason in sorted(invalid_present.items()):
            print(f"robinhood_payload: {ticker}: {reason}", file=sys.stderr)
        return 2
    ok = len(rebuilt["sources"]["robinhood"])
    quiet = _sidecar_quiet(out)
    problem = _coverage_problem(ok, len(plan["tickers"]), len(quiet), min_coverage)
    if problem:
        print(f"robinhood_payload: only {ok}/{len(plan['tickers'])} tickers validate: {problem}", file=sys.stderr)
        return 2
    print(f"robinhood_payload: OK {out} ({ok}/{len(plan['tickers'])})")
    return 0


# --------------------------------------------------------------------------- cli


def _read_plan(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan", help="print the plan JSON for a slot (or the one due now with --slot auto)")
    p_plan.add_argument("--slot", required=True)
    p_plan.add_argument("--day", default=None, help="ET trading day (default: today in ET)")
    p_plan.add_argument("--now-et", default=None, help="HH:MM override for --slot auto")
    p_plan.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE)

    p_prompt = sub.add_parser("prompt", help="print the agent prompt for one chunk")
    p_prompt.add_argument("--plan", required=True)
    p_prompt.add_argument("--tickers", required=True, help="comma-separated chunk")

    p_merge = sub.add_parser("merge", help="validate chunk outputs and publish the payload")
    p_merge.add_argument("--plan", required=True)
    p_merge.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)
    p_merge.add_argument("chunks", nargs="*")

    p_broker = sub.add_parser("broker-prompt", help="print the prompt for the order-history and buying-power worker")
    p_broker.add_argument("--plan", required=True)

    p_ingest = sub.add_parser("ingest", help="compute plan-first recommendations from the payload and a broker reply")
    p_ingest.add_argument("--plan", required=True)
    p_ingest.add_argument("--broker-reply", required=True)
    p_ingest.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)
    p_ingest.add_argument("--max-age-minutes", type=int, default=DEFAULT_MAX_AGE_MINUTES)
    p_ingest.add_argument("--now", default=None, help="ISO tz-aware clock override (tests and replays only)")

    p_check = sub.add_parser("check", help="validate a published payload")
    p_check.add_argument("--plan", required=True)
    p_check.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)
    p_check.add_argument("--max-age-minutes", type=int, default=DEFAULT_MAX_AGE_MINUTES)
    p_check.add_argument("--now", default=None, help="ISO tz-aware clock override (tests and replays only)")

    args = parser.parse_args(argv)
    if args.cmd == "plan":
        today = args.day or datetime.now(ET).date().isoformat()
        day = date.fromisoformat(today)
        slot = args.slot.upper().removesuffix("ET")
        if slot == "AUTO":
            now = time.fromisoformat(args.now_et) if args.now_et else datetime.now(ET).time().replace(second=0, microsecond=0)
            slot = due_slot(day, now)
            if slot is None:
                return EXIT_NO_SLOT_DUE
        plan = build_plan(slot, day)
        plan["chunks"] = [
            plan["tickers"][i : i + args.chunk_size] for i in range(0, len(plan["tickers"]), args.chunk_size)
        ]
        print(json.dumps(plan))
        return 0
    plan = _read_plan(args.plan)
    if args.cmd == "prompt":
        print(build_prompt(plan, [t for t in args.tickers.split(",") if t]))
        return 0
    if args.cmd == "broker-prompt":
        print(build_broker_prompt(plan, _account_number()))
        return 0
    if args.cmd == "ingest":
        ingest_now = datetime.fromisoformat(args.now).astimezone(ET) if args.now else None
        return ingest(plan, args.broker_reply, now=ingest_now, min_coverage=args.min_coverage,
                      max_age_minutes=args.max_age_minutes)
    if args.cmd == "merge":
        try:
            payload, failed = assemble(plan, args.chunks)
        except PayloadError as exc:  # a plan whose chunk ownership is unprovable publishes nothing
            print(f"robinhood_payload: {exc}; nothing published", file=sys.stderr)
            return 2
        return publish(plan, payload, failed, min_coverage=args.min_coverage)
    now = datetime.fromisoformat(args.now).astimezone(ET) if args.now else None
    return check(plan, min_coverage=args.min_coverage, now=now, max_age_minutes=args.max_age_minutes)


if __name__ == "__main__":
    sys.exit(main())
