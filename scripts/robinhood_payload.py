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
* ``check``   -- re-validate a published payload for a slot (used by the live prompts' Gate 5).

The agent is an LLM copying numbers, so nothing it returns is trusted: every bar is range- and
consistency-checked, a quote that disagrees with its own last bar is rejected, and a ticker that fails any
check goes to ``ticker_errors.robinhood`` (a fetch that raised) rather than being scored on doubtful data.
Too little coverage publishes nothing, so the slot aborts at Gate 5 instead of trading a thin universe.

The 3:30 PM slot is Finance-primary and is deliberately not covered here.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, time, timedelta
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

DEFAULT_CHUNK_SIZE = 8
#: Share of the universe that must validate before a payload is published at all.
DEFAULT_MIN_COVERAGE = 0.8
#: Launchd fires one minute after the decision time (the decision bar's predecessor has closed); a late wake is
#: accepted for up to 14 minutes, after which a payload helps nobody -- the session has already run Gate 5.
AUTO_MIN_AFTER = 1
AUTO_MAX_AFTER = 15
#: A quote further than this from its own last bar close is treated as a transcription error, not a gap move.
MAX_QUOTE_VS_LAST_CLOSE = 0.25
#: Quote freshness, measured from the slot's decision time (Codex adversarial review: a same-day quote from hours
#: earlier passed, and the payload feeds Gate 5's candidate list). Not before the newest completed bar the series
#: must carry (decision minus one interval), so it describes the price at the decision; and not after this, which
#: covers launchd's AUTO_MAX_AFTER window plus the worst-case chunk rounds (3 x 240 s) of the fetch. A quote stamped
#: later belongs to a re-run hours after the slot, not to this slot's decision.
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


def validate_bar(raw: Any) -> dict[str, Any]:
    """One OHLCV bar with a tz-aware timestamp and a self-consistent range; returns the normalized bar."""
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
        raise PayloadError(f"no intraday bars between {bar_start:%H:%M} and {decision:%H:%M} ET")
    if min(by_stamp) > start + timedelta(minutes=interval):
        raise PayloadError(f"intraday bars do not cover the {bar_start:%H:%M} trend-window start")
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
    if as_of < anchor - timedelta(minutes=interval):
        raise PayloadError(f"quote as_of {as_of:%H:%M} ET is stale for the {decision:%H:%M} decision")
    if as_of > anchor + QUOTE_MAX_AFTER_DECISION:
        raise PayloadError(f"quote as_of {as_of:%H:%M} ET is too long after the {decision:%H:%M} decision")
    if as_of > (now or datetime.now(ET)).astimezone(ET) + QUOTE_FUTURE_SKEW:
        raise PayloadError(f"quote as_of {as_of.isoformat()} is in the future")
    if abs(price - closes[-1]) / closes[-1] > MAX_QUOTE_VS_LAST_CLOSE:
        raise PayloadError(f"quote {price} disagrees with the last bar close {closes[-1]}")
    return {
        "previous_close_bar": prev,
        "intraday_bars": bars,
        "quote": {"price": price, "as_of": as_of.isoformat()},
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


def build_plan(slot: str, day: date) -> dict[str, Any]:
    """What the slot's Gate 5 generator scores, exactly as it scores it (same loader and narrowing)."""
    import dca_config
    import dca_runner

    if slot not in BAR_START_ET:
        raise SystemExit(f"slot {slot!r} is not a Robinhood-primary slot; expected one of {', '.join(SLOTS)}")
    config_dir = dca_config.get_config_dir()
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
        "bar_start_et": BAR_START_ET[slot].strftime("%H:%M"),
        "interval_minutes": int(interval),
        "tickers": tickers,
        "out": output_path(day, slot),
    }


# --------------------------------------------------------------------------- prompt


def build_prompt(plan: dict[str, Any], tickers: list[str]) -> str:
    return f"""You are a data-retrieval worker. Use ONLY the Robinhood MCP tools that read market data
(historicals and quotes). Never call any order, cancel, watchlist or alert tool.

Trading day: {plan['trading_day']} (America/New_York). Tickers: {', '.join(tickers)}

For EACH ticker fetch:
1. {plan['interval_minutes']}-minute bars for today (historicals, interval {plan['interval_minutes']}minute, span day), covering
   {plan['bar_start_et']} ET through {plan['decision_time_et']} ET. Keep only bars whose START timestamp is at or after
   {plan['bar_start_et']} ET and strictly before {plan['decision_time_et']} ET (omit the still-open bar).
2. The previous completed regular session's daily bar (historicals, interval day).
3. A current quote.

Reply with ONE JSON object and nothing else (no prose, no code fence). Keys are the tickers above:
{{"TICKER": {{"previous_close_bar": BAR, "intraday_bars": [BAR, ...], "quote": {{"price": number, "as_of": "<iso with offset>"}}}}}}
where BAR is {{"timestamp": "<iso with offset>", "open": n, "high": n, "low": n, "close": n, "volume": n}}.

Rules: copy every number and timestamp exactly as the tool returned it; never estimate, round, interpolate or
fill a gap. If a ticker's data cannot be fetched, give {{"error": "<short reason>"}} for that ticker and keep going.
Every requested ticker must appear exactly once. Do not write files; your final message is the whole output."""


# --------------------------------------------------------------------------- merge / publish


def _load_chunk(path: str) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8").strip()
    if text.startswith("```"):  # tolerate a fenced reply despite the instruction
        text = text.strip("`")
        text = text[text.index("{") :] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise PayloadError("no JSON object in chunk output")
    from dca_runner import _reject_duplicate_keys

    loaded = json.loads(text[start : end + 1], object_pairs_hook=_reject_duplicate_keys)
    if not isinstance(loaded, dict):
        raise PayloadError("chunk output is not an object")
    return loaded


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
    for ticker in plan["tickers"]:
        try:
            if ticker in duplicated:
                raise PayloadError(f"returned by more than one chunk reply ({', '.join(duplicated[ticker])})")
            if ticker not in merged:
                raise PayloadError("no data returned for this ticker")
            good[ticker] = validate_ticker(
                merged[ticker], day=day, bar_start=bar_start, decision=decision, interval=interval
            )
        except PayloadError as exc:
            failed[ticker] = str(exc)
    payload = {
        "sources": {"robinhood": good},
        "source_errors": [],
        "ticker_errors": {"robinhood": sorted(failed)},
    }
    return payload, failed


def publish(plan: dict[str, Any], payload: dict[str, Any], failed: dict[str, str], *, min_coverage: float) -> int:
    universe = len(plan["tickers"])
    ok = len(payload["sources"]["robinhood"])
    coverage = ok / universe
    for ticker, reason in sorted(failed.items()):
        print(f"robinhood_payload: {ticker}: {reason}", file=sys.stderr)
    if coverage < min_coverage:
        print(
            f"robinhood_payload: coverage {ok}/{universe} ({coverage:.0%}) is below {min_coverage:.0%}; "
            "publishing nothing so the slot aborts at Gate 5",
            file=sys.stderr,
        )
        return 2
    out = Path(plan["out"])
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        print(f"robinhood_payload: {out} already exists; leaving the published payload in place")
        return 0
    meta = {
        "slot": plan["slot"],
        "trading_day": plan["trading_day"],
        "generated_at_et": datetime.now(ET).isoformat(),
        "fetched_by": "codex exec via robinhood-trading MCP (scripts/robinhood_payload.py)",
        "universe": universe,
        "valid": ok,
        "failed": failed,
    }
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=1)
        # ln-style no-overwrite publish: a concurrent run that finished first wins, nobody sees a partial file.
        try:
            os.link(tmp, out)
        except FileExistsError:
            print(f"robinhood_payload: {out} appeared during assembly; leaving it in place")
            return 0
    finally:
        os.unlink(tmp)
    out.with_suffix(".meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"robinhood_payload: wrote {out} ({ok}/{universe} tickers)")
    return 0


def check(plan: dict[str, Any], *, min_coverage: float) -> int:
    """Re-validate the published payload for ``plan``'s slot; non-zero exit means 'treat as unavailable'."""
    out = Path(plan["out"])
    if not out.is_file():
        print(f"robinhood_payload: missing {out}", file=sys.stderr)
        return 2
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
    if ok / len(plan["tickers"]) < min_coverage:
        print(f"robinhood_payload: only {ok}/{len(plan['tickers'])} tickers validate", file=sys.stderr)
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

    p_check = sub.add_parser("check", help="validate a published payload")
    p_check.add_argument("--plan", required=True)
    p_check.add_argument("--min-coverage", type=float, default=DEFAULT_MIN_COVERAGE)

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
    if args.cmd == "merge":
        try:
            payload, failed = assemble(plan, args.chunks)
        except PayloadError as exc:  # a plan whose chunk ownership is unprovable publishes nothing
            print(f"robinhood_payload: {exc}; nothing published", file=sys.stderr)
            return 2
        return publish(plan, payload, failed, min_coverage=args.min_coverage)
    return check(plan, min_coverage=args.min_coverage)


if __name__ == "__main__":
    sys.exit(main())
