#!/usr/bin/env python3
"""Assemble the candidate generators' market-data payload from raw Robinhood responses.

WHAT THIS IS FOR
----------------
The backup planner's safety gate (``compute --market-data``) runs the primary's own Gate 5 on one payload
shape (``scripts/generate_buy_candidates.py``, "INPUT SHAPE"). The Perplexity task must not reshape broker
responses itself: that is exactly the per-run compute, and the credit cost, this design removes. It dumps
the raw ``get_equity_historicals`` and ``get_equity_quotes`` responses it fetched into ONE file and runs
this script, which parses them with the repo's own ``RobinhoodDataProvider`` so the parsing is the
production parsing and not a second copy.

Usage::

    python3 backup_planner.pyz assemble-market-data --trading-day 2026-10-07 --raw raw.json --out market.json
    python3 -m scripts.assemble_market_data         --trading-day 2026-10-07 --raw raw.json --out market.json

``--raw`` may also be several files, or a directory (its ``*.json`` files, sorted by name), each holding any subset of
the three lists below; they are concatenated in order. Save each broker response to its own small part file
(``raw_001.json`` ...) when one ``raw.json`` would be too large to write in one go.

``--raw`` is ``{"historicals": [<raw response>, ...], "daily": [<raw response>, ...], "quotes": [<raw response>, ...]}``.
``historicals`` are the 5-minute ``get_equity_historicals`` responses, ``daily`` the same tool with ``interval=day``
(the previous completed session's bar), ``quotes`` the ``get_equity_quotes`` responses. A response may carry one
symbol or many, and several responses may cover one symbol (pages). ``daily`` is optional: without it the previous
bar is rebuilt from the quote alone and keeps the unknown-low sentinel (see below).

WHAT IT WRITES
--------------
``{"sources": {"finance": {}, "robinhood": {T: {previous_close_bar, intraday_bars, quote}}},
"source_errors": [], "ticker_errors": {"finance": [], "robinhood": [...]}}``.

* ``finance`` is an explicit ``{}``. This task fetches Robinhood only, and the backup policy is that a
  finance NO_DATA does not block a Robinhood-complete buy (the generator reads ``{}`` that way).
* A universe ticker with missing, partial or unusable raw data goes under ``ticker_errors.robinhood`` (an
  explicit error marker, which blocks an auto-buy for it), never silently dropped. Any non-finite or
  non-positive price, or a prior-close date that is not strictly before the trading day, is such a defect.
* The previous-close bar keeps the quote's OFFICIAL close and session. The ``daily`` response only adds the real
  open/high/low/volume to it, exactly as the primary's payload carries them, so a ticker trading below yesterday's
  low gets the primary's full first-allocation base. It is used only when it is that same session and its close
  agrees with the official close within 1%: a stale or conflicting daily bar is ignored, never allowed to move the
  reference close. Without a usable daily bar the bar keeps ``low=0.0``, the provider's "unknown" sentinel:
  fabricating a low would upgrade the base to the larger scale, so the reduced base can only be a conservative
  miss, never an over-buy. Daily and 5-minute responses are kept apart and never mix.
* Symbols outside the active universe, and the do-not-buy list, are ignored.

WHAT IT IS NOT
--------------
It makes no network calls and imports no broker code. Input that is unreadable, malformed or yields no
usable ticker writes NOTHING and exits 2; an earlier output file is left untouched. One JSON document on
stdout; exit 0 on success. Output bytes are deterministic.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import re
import sys
import tempfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Sequence
from zoneinfo import ZoneInfo

MIN_PYTHON = (3, 11)  # mirrors ``requires-python`` in pyproject.toml, scripts/backup_planner.py and the .pyz bootstrap


def require_supported_python(version_info) -> None:
    """Exit 2 with this tool's FAILED document when the interpreter is older than ``MIN_PYTHON``.

    ``backup_planner.pyz`` already refuses an old interpreter before it dispatches ``assemble-market-data``; this covers
    the documented ``python3 -m scripts.assemble_market_data`` form. 2026-10-16 09:49 ET: the Mac shell fallback ran
    Python 3.9, so the assembler would have died on an import or syntax error with no usable reason. The document and
    the exit code are the ones this tool already uses for every other refusal (module docstring, "WHAT IT IS NOT"), and
    it runs before argument parsing, so nothing is ever written. Called before any project import.
    """
    if tuple(version_info[:2]) >= MIN_PYTHON:
        return
    detail = "python>=%d.%d required, found %d.%d" % (MIN_PYTHON + tuple(version_info[:2]))
    print(json.dumps({"status": "FAILED", "errors": [{"code": "python_too_old", "detail": detail}]}, indent=2))
    sys.exit(2)


require_supported_python(sys.version_info)

import dca_config  # noqa: E402  (every project import must follow the version guard)
from dca_live_adapters import RobinhoodDataProvider  # noqa: E402

ET = ZoneInfo("America/New_York")
ROBINHOOD_BAR_MINUTES = 5  # StrategyConfig.robinhood_interval_minutes: the generator resolves bars at this cadence
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Refused(Exception):
    """A structured, never-raw refusal of the input as a whole."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code, self.detail = code, detail


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite constant {token}")


_RAW_KINDS = ("historicals", "daily", "quotes")


def _load_json(path: Path) -> Any:
    if not path.is_file():
        raise _Refused("raw_missing", path.name)
    try:
        return json.loads(
            path.read_bytes(), object_pairs_hook=dca_config._reject_duplicate_keys, parse_constant=_reject_constant
        )
    except (OSError, ValueError) as exc:
        raise _Refused("raw_corrupt", f"{path.name}: {type(exc).__name__}") from exc


def _expand_parts(paths: Sequence[Path]) -> list[Path]:
    """Files to read, in order: a directory stands for its ``*.json`` files, sorted by name (not recursive).

    2026-11-13: the Perplexity backup could not write one ``raw.json`` holding every raw 5-minute response for the
    whole universe (the file was too large for a single write, and the run stalled while the agent chunked it by
    hand). Each broker response can now be saved as its own small part file and passed here, so no single file has
    to be large and nothing has to be merged or retyped by hand.
    """
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(p for p in path.iterdir() if p.suffix == ".json" and p.is_file())
            if not found:
                raise _Refused("raw_empty", f"{path.name}: no .json part files")
            files.extend(found)
        else:
            files.append(path)
    return files


def _read_raw(path: Path | Sequence[Path]) -> dict[str, list[Any]]:
    paths = [path] if isinstance(path, Path) else list(path)
    files = _expand_parts(paths)
    if not files:
        raise _Refused("raw_missing", "no raw files given")
    if len(files) == 1 and len(paths) == 1 and not paths[0].is_dir():
        # The original single-file form keeps its strict shape: both lists present.
        document = _load_json(files[0])
        if not isinstance(document, dict) or not all(isinstance(document.get(k), list) for k in ("historicals", "quotes")):
            raise _Refused("raw_corrupt", "expected an object with 'historicals' and 'quotes' lists")
    else:
        # Part files: each is the same shape with any subset of the three lists; they are concatenated in file order.
        document = {kind: [] for kind in _RAW_KINDS}
        for file in files:
            part = _load_json(file)
            if not isinstance(part, dict) or not part or any(k not in _RAW_KINDS for k in part):
                raise _Refused("raw_corrupt", f"{file.name}: expected an object with only {', '.join(_RAW_KINDS)} lists")
            for kind, responses in part.items():
                if not isinstance(responses, list):
                    raise _Refused("raw_corrupt", f"{file.name}: '{kind}' must be a list")
                document[kind].extend(responses)
    if not document["historicals"] and not document["quotes"]:
        raise _Refused("raw_empty", "no raw responses to assemble")
    daily = document.get("daily", [])
    if not isinstance(daily, list):
        raise _Refused("raw_corrupt", "'daily' must be a list of get_equity_historicals (interval=day) responses")
    # Every saved response must be well-formed, not just the two outer lists: a truncated or failed broker
    # response beside good data would otherwise be skipped silently and hide an incomplete fetch.
    for kind, responses in (("historicals", document["historicals"]), ("daily", daily), ("quotes", document["quotes"])):
        for index, response in enumerate(responses):
            _results_of(response, f"{kind}[{index}]")
    return {"historicals": document["historicals"], "daily": daily, "quotes": document["quotes"]}


def _results_of(response: Any, label: str) -> list[Any]:
    """The ``results`` list of one saved response (bare, or wrapped in ``data``), else a refusal naming which one."""
    inner = response.get("data") if isinstance(response, dict) and "data" in response else response
    results = inner.get("results") if isinstance(inner, dict) else None
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise _Refused("raw_corrupt", f"{label} is not a response with a 'results' list of objects")
    return results


class _Replay:
    """The two tool callables ``RobinhoodDataProvider`` takes, answering from the saved responses."""

    def __init__(self, raw: dict[str, list[Any]], historicals_key: str = "historicals") -> None:
        self._raw = raw
        self._historicals_key = historicals_key  # "historicals" (5-minute) or "daily": the two are never mixed

    def historicals(self, *, symbols: list[str], **_ignored: Any) -> dict[str, Any]:
        wanted = {s.upper() for s in symbols}
        merged: list[dict[str, Any]] = []
        for response in self._raw[self._historicals_key]:
            for result in _results_of(response, self._historicals_key):
                if str(result.get("symbol", "")).upper() in wanted:
                    merged.append(result)
        # Pages of one symbol become one result so the provider sees every bar.
        bars = [bar for result in merged for bar in (result.get("bars") or [])]
        return {"results": [{"symbol": sorted(wanted)[0], "bars": bars}] if merged else []}

    def quotes(self, *, symbols: list[str], **_ignored: Any) -> dict[str, Any]:
        wanted = {s.upper() for s in symbols}
        found = []
        for response in self._raw["quotes"]:
            for result in _results_of(response, "quotes"):
                quote = result.get("quote")
                if isinstance(quote, dict) and str(quote.get("symbol", "")).upper() in wanted:
                    found.append(result)
        return {"results": found}


def _finite_positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


DAILY_CLOSE_TOLERANCE = 0.01  # a daily close more than 1% from the quote's official close is a conflict, not rounding


def _with_real_range(official: Any, daily_provider: RobinhoodDataProvider, ticker: str, as_of: datetime, trading_day: date) -> Any:
    """``official`` (the quote-derived prior-session bar) with the real open/high/low/volume from the daily bar.

    The daily bar may only ADD the real range to the official prior session; it never replaces the official close or
    names the session. So it is used only when it is that same session (a bar from days earlier, or a missing
    session papered over by an older one, would move the reference close and manufacture a drop), its close agrees
    with the official close within ``DAILY_CLOSE_TOLERANCE``, and its prices are finite with a positive low. Anything
    else returns ``official`` unchanged: the unknown-low sentinel, which can only pick the reduced base.

    The daily provider is built without a quote function, so it takes its daily-historicals path (the production
    parsing) and never the quote-derived bar.
    """
    bar = daily_provider.get_previous_close_bar(ticker, as_of)
    if bar is None or bar.timestamp.astimezone(ET).date() >= trading_day:
        return official
    if bar.timestamp.astimezone(ET).date() != official.timestamp.astimezone(ET).date():
        return official  # not the session the quote calls the prior close
    prices = (bar.open, bar.high, bar.low, bar.close)
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in prices) or bar.low <= 0 or bar.close <= 0:
        return official
    if abs(bar.close - official.close) > DAILY_CLOSE_TOLERANCE * official.close:
        return official
    return dataclasses.replace(official, open=bar.open, high=bar.high, low=bar.low, volume=bar.volume)


def _snapshot(
    provider: RobinhoodDataProvider, ticker: str, trading_day: date, daily_provider: RobinhoodDataProvider | None = None,
) -> dict[str, Any] | None:
    """One ticker's payload entry, or ``None`` when its raw data is missing, partial or unusable."""
    as_of = datetime.combine(trading_day, time(16, 0), tzinfo=ET)
    open_bell = datetime.combine(trading_day, time(9, 30), tzinfo=ET)
    bars = provider.get_intraday_bars(ticker, open_bell, as_of, ROBINHOOD_BAR_MINUTES)
    previous = provider.get_previous_close_bar(ticker, as_of)
    if daily_provider is not None and previous is not None:
        previous = _with_real_range(previous, daily_provider, ticker, as_of, trading_day)  # the real low when it checks out
    quote = provider.get_quote(ticker, as_of)
    if not bars or previous is None or quote is None:
        return None
    price, quote_time = quote
    if not _finite_positive(price) or not _finite_positive(previous.close):
        return None
    if previous.timestamp.astimezone(ET).date() >= trading_day:
        return None  # a same-day or future "previous close" is not the prior completed session
    by_time: dict[datetime, Any] = {}
    for bar in bars:
        if not all(_finite_positive(v) for v in (bar.open, bar.high, bar.low, bar.close)) or not math.isfinite(bar.volume):
            return None
        by_time[bar.timestamp] = bar  # overlapping pages repeat a bar; the last copy wins
    entry: dict[str, Any] = {
        "previous_close_bar": {
            "timestamp": previous.timestamp.astimezone(ET).isoformat(), "open": previous.open, "high": previous.high,
            "low": previous.low, "close": previous.close, "volume": previous.volume,
        },
        "intraday_bars": [
            {"timestamp": b.timestamp.astimezone(ET).isoformat(), "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume}
            for _, b in sorted(by_time.items())
        ],
        "quote": {"price": price},
    }
    if quote_time is not None:
        entry["quote"]["as_of"] = quote_time.astimezone(ET).isoformat()
    return entry


def assemble(raw: dict[str, list[Any]], trading_day: date, universe: list[str]) -> dict[str, Any]:
    """The payload for ``universe``; tickers with unusable data are error-marked, not dropped."""
    replay = _Replay(raw)
    provider = RobinhoodDataProvider(replay.historicals, replay.quotes)
    daily_provider = RobinhoodDataProvider(_Replay(raw, "daily").historicals, None) if raw.get("daily") else None
    robinhood: dict[str, Any] = {}
    errors: list[str] = []
    for ticker in sorted(universe):
        try:
            entry = _snapshot(provider, ticker, trading_day, daily_provider)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            entry = None
        if entry is None:
            errors.append(ticker)
        else:
            robinhood[ticker] = entry
    return {
        "sources": {"finance": {}, "robinhood": robinhood},
        "source_errors": [],
        "ticker_errors": {"finance": [], "robinhood": errors},
    }


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _report(status: str, **fields: Any) -> dict[str, Any]:
    return {"status": status, **fields}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trading-day", required=True, help="YYYY-MM-DD (America/New_York).")
    parser.add_argument("--raw", required=True, type=Path, nargs="+",
                        help="Raw responses file(s) or a directory of part files; see the module docstring.")
    parser.add_argument("--out", required=True, type=Path, help="Where to write the payload.")
    parser.add_argument("--config-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        if not _DAY_RE.match(args.trading_day):
            raise _Refused("bad_trading_day", "expected YYYY-MM-DD")
        try:
            trading_day = date.fromisoformat(args.trading_day)
        except ValueError as exc:
            raise _Refused("bad_trading_day", "not a calendar date") from exc
        raw = _read_raw(args.raw)
        config = dca_config.load_strategy_config(args.config_dir or dca_config.get_config_dir())
        universe = [t for t in config.tickers if t not in set(config.excluded_tickers)]
        payload = assemble(raw, trading_day, universe)
        if not payload["sources"]["robinhood"]:
            raise _Refused("no_usable_data", "no universe ticker had complete, usable raw data")
        _write_atomic(args.out, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except _Refused as refusal:
        print(json.dumps(_report("FAILED", errors=[{"code": refusal.code, "detail": refusal.detail}]), indent=2))
        return 2
    except Exception as exc:  # noqa: BLE001 - last fail-closed boundary; type name only, a message can carry data
        print(json.dumps(_report("FAILED", errors=[{"code": "unexpected_error", "detail": type(exc).__name__}]), indent=2))
        return 2
    print(json.dumps(_report("ASSEMBLED", path=str(args.out), assembled=len(payload["sources"]["robinhood"]),
                             ticker_errors=payload["ticker_errors"]["robinhood"]), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
