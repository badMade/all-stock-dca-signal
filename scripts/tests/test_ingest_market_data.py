"""Tests for ``scripts/ingest_market_data.py`` (written BEFORE the module exists).

Why this script exists (2026-10-05 incident, five of seven slots): the live prompts told
the executor to "save" 65 tickers of Robinhood bars and quotes into
``market_data_<day>_<slot>.json``, but MCP tool results land in the model's context, not on
disk, so every executor either re-typed the payload by hand, skipped the fetch, or fell back
to yfinance. Hand transcription is the failure this script removes the incentive for: the
session hands the raw ``get_equity_historicals`` / ``get_equity_quotes`` output (a saved
file, or stdin) to this command, which parses it with the same shapes
``RobinhoodDataProvider`` reads, validates it, and writes the exact payload the Gate 5
generators consume, plus an ``ingest`` evidence block carrying content hashes.

Contract pinned here (the implementation must match it; it is the spec):

* CLI, every input flag repeatable and taking one path or ``-`` for stdin (one stdin input
  at most)::

      --trading-day YYYY-MM-DD --window-start-et HH:MM --window-end-et HH:MM
      --expected-tickers A,B,C [--min-bars N] --out PATH
      --bars-5m FILE... --bars-daily FILE... --quotes FILE...

* Success (exit 0) prints one JSON object ``{"ok": true, "tickers_ingested", "tickers_failed",
  "payload_sha256", "provenance", "out"}`` and writes::

      {"sources": {"robinhood": {T: {"previous_close_bar", "intraday_bars", "quote"}}},
       "source_errors": [], "ticker_errors": {"robinhood": [failed tickers]},
       "ingest": {...evidence...}}

* Every whole-input refusal exits 2, prints ``{"ok": false, "reason", "detail"}`` and writes
  NOTHING (an existing ``--out`` file is left untouched), the same rule as
  ``scripts/assemble_order_pages.py``.
* A per-ticker problem is NOT silently trimmed and NOT a whole-input refusal: that ticker is
  left out of ``sources.robinhood``, named in ``ticker_errors.robinhood``, and its reason is
  recorded in ``ingest.ticker_failures``. If no ticker survives, the whole input is refused.
* Provenance is derived from the inputs, never asserted by the caller: ``spilled_file`` when
  every input was a file, ``stdin_paste`` when ANY input came from stdin. ``stdin_paste`` is
  the weaker class: the session re-typed or relayed the bytes, and the hash only proves the
  validated bytes are the ones used, not that they equal the broker's response.

Fixtures follow the raw shapes in ``dca_live_adapters`` (``begins_at``/``open_price``/
``close_price`` bars under ``data.results[].bars``; quotes under ``data.results[].quote``),
all with synthetic tickers.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")
DAY = date(2026, 8, 5)  # a Wednesday
PREVIOUS_SESSION = date(2026, 8, 4)
TICKERS = ("AAA", "BBB", "CCC")


class _LazyModule:
    """Resolve ``scripts.ingest_market_data`` on first attribute access, inside the test body.

    Imported at module level (or in the fixture itself) a missing module would surface as one
    collection error or a pile of setup ERRORs. Resolved lazily, every test below FAILS with
    the same clear ModuleNotFoundError until the module exists, which is the red state we want.
    """

    def __getattr__(self, name):
        return getattr(importlib.import_module("scripts.ingest_market_data"), name)


@pytest.fixture()
def ingest():
    return _LazyModule()


# --- raw-response builders --------------------------------------------------------


def _et(hour: int, minute: int, *, day: date = DAY) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ET)


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw_bar(moment: datetime, close: float, **overrides) -> dict:
    bar = {
        "begins_at": _iso_z(moment),
        "session": "reg",
        "open_price": f"{close:.4f}",
        "close_price": f"{close:.4f}",
        "high_price": f"{close + 0.01:.4f}",
        "low_price": f"{close - 0.01:.4f}",
        "volume": 1000,
        "interpolated": False,
    }
    bar.update(overrides)
    return bar


def _falling_bars(early: float = 99.0, late: float = 95.0) -> list:
    """12 completed 5-minute bars, 09:30 through 10:25 ET, stepping down at 10:00."""
    bars = []
    moment = _et(9, 30)
    while moment <= _et(10, 25):
        bars.append(_raw_bar(moment, late if moment >= _et(10, 0) else early))
        moment += timedelta(minutes=5)
    return bars


def _daily_bar(begins: date = PREVIOUS_SESSION, *, close: float = 100.0, low: float = 97.0, **overrides) -> dict:
    bar = {
        "begins_at": f"{begins.isoformat()}T00:00:00Z",
        "session": "reg",
        "open_price": f"{close:.4f}",
        "close_price": f"{close:.4f}",
        "high_price": f"{close + 1.0:.4f}",
        "low_price": f"{low:.4f}",
        "volume": 250000,
        "interpolated": False,
    }
    bar.update(overrides)
    return bar


def _quote_body(symbol: str, price: str = "95.0000", **overrides) -> dict:
    quote = {
        "symbol": symbol,
        "last_trade_price": price,
        "venue_last_trade_time": _iso_z(_et(10, 29)),
        "has_traded": True,
        "state": "active",
        "previous_close": "100.0000",
        "previous_close_date": PREVIOUS_SESSION.isoformat(),
    }
    quote.update(overrides)
    return quote


def _historicals(per_symbol: dict) -> dict:
    return {"data": {"results": [{"symbol": s, "bars": bars} for s, bars in per_symbol.items()]}}


def _quotes(per_symbol: dict) -> dict:
    return {
        "data": {
            "results": [
                {"quote": body, "close": {"price": "100.0000", "date": PREVIOUS_SESSION.isoformat()}}
                for body in per_symbol.values()
            ]
        }
    }


def _write(tmp_path, name: str, obj_or_text) -> str:
    path = tmp_path / name
    text = obj_or_text if isinstance(obj_or_text, str) else json.dumps(obj_or_text)
    path.write_text(text, encoding="utf-8")
    return str(path)


def _build(tmp_path, *, tickers=TICKERS, bars_by=None, daily_by=None, quote_by=None) -> dict:
    """Write one healthy response per role; ``*_by`` overrides one ticker's slice.

    An override of ``None`` removes that ticker from the response entirely; any other value
    replaces its bars / daily bars / quote body.
    """
    bars = {t: _falling_bars() for t in tickers}
    daily = {t: [_daily_bar()] for t in tickers}
    quotes = {t: _quote_body(t) for t in tickers}
    for target, overrides in ((bars, bars_by), (daily, daily_by), (quotes, quote_by)):
        for ticker, value in (overrides or {}).items():
            if value is None:
                target.pop(ticker, None)
            else:
                target[ticker] = value
    return {
        "bars_5m": [_write(tmp_path, "bars_5m.json", _historicals(bars))],
        "bars_daily": [_write(tmp_path, "bars_daily.json", _historicals(daily))],
        "quotes": [_write(tmp_path, "quotes.json", _quotes(quotes))],
    }


def _run(ingest, tmp_path, files, *, tickers=TICKERS, min_bars="6", extra=()):
    out = tmp_path / "market_data_out.json"
    argv = [
        "--trading-day", DAY.isoformat(),
        "--window-start-et", "09:30",
        "--window-end-et", "10:30",
        "--expected-tickers", ",".join(tickers),
        "--min-bars", min_bars,
        "--out", str(out),
        *extra,
    ]
    for role, flag in (("bars_5m", "--bars-5m"), ("bars_daily", "--bars-daily"), ("quotes", "--quotes")):
        for path in files.get(role, []):
            argv += [flag, path]
    return ingest.main(argv), out


def _payload(out) -> dict:
    return json.loads(out.read_bytes())


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _assert_refused(ingest, tmp_path, files, reason, capsys, **kwargs):
    code, out = _run(ingest, tmp_path, files, **kwargs)
    assert code == 2
    assert not out.exists(), "a refused run must not leave a payload behind"
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is False
    assert summary["reason"] == reason


# --- happy path and the exact payload shape -----------------------------------------


def test_happy_path_writes_the_generator_payload_shape(ingest, tmp_path, capsys):
    code, out = _run(ingest, tmp_path, _build(tmp_path))
    assert code == 0
    payload = _payload(out)
    assert payload["source_errors"] == []
    assert payload["ticker_errors"] == {"robinhood": []}
    assert sorted(payload["sources"]["robinhood"]) == list(TICKERS)
    row = payload["sources"]["robinhood"]["AAA"]
    assert set(row) == {"previous_close_bar", "intraday_bars", "quote"}
    assert len(row["intraday_bars"]) == 12
    assert set(row["previous_close_bar"]) == {"timestamp", "open", "high", "low", "close", "volume"}
    assert row["quote"]["price"] == pytest.approx(95.0)
    assert datetime.fromisoformat(row["quote"]["as_of"]) == _et(10, 29)
    summary = json.loads(capsys.readouterr().out)
    assert summary["ok"] is True
    assert summary["tickers_ingested"] == 3
    assert summary["tickers_failed"] == []
    assert summary["out"] == str(out)


def test_ingested_bars_equal_what_the_live_adapter_parser_reads(ingest, tmp_path):
    """One parsing truth: the payload's bars are the Bars RobinhoodDataProvider would build."""
    from dca_live_adapters import RobinhoodDataProvider

    raw = _historicals({"AAA": _falling_bars()})
    files = _build(tmp_path)
    code, out = _run(ingest, tmp_path, files)
    assert code == 0
    expected = RobinhoodDataProvider(historicals_fn=lambda **_: raw)._parse_bars(raw, "AAA")
    got = _payload(out)["sources"]["robinhood"]["AAA"]["intraday_bars"]
    assert [datetime.fromisoformat(b["timestamp"]) for b in got] == [b.timestamp for b in expected]
    assert [b["close"] for b in got] == [b.close for b in expected]
    assert [b["volume"] for b in got] == [b.volume for b in expected]


def test_timestamps_are_timezone_aware(ingest, tmp_path):
    code, out = _run(ingest, tmp_path, _build(tmp_path))
    assert code == 0
    row = _payload(out)["sources"]["robinhood"]["AAA"]
    for bar in [row["previous_close_bar"], *row["intraday_bars"]]:
        assert datetime.fromisoformat(bar["timestamp"]).tzinfo is not None


def test_previous_close_is_the_latest_daily_bar_before_the_trading_day(ingest, tmp_path):
    daily = [
        _daily_bar(date(2026, 8, 3), close=90.0, low=88.0),
        _daily_bar(PREVIOUS_SESSION, close=100.0, low=97.0),
        _daily_bar(DAY, close=95.0, low=94.0),  # today's still-forming daily bar
        _daily_bar(date(2026, 8, 2), close=1.0, low=1.0, interpolated=True),
    ]
    code, out = _run(ingest, tmp_path, _build(tmp_path, daily_by={"AAA": daily}))
    assert code == 0
    bar = _payload(out)["sources"]["robinhood"]["AAA"]["previous_close_bar"]
    assert bar["close"] == pytest.approx(100.0)
    assert bar["low"] == pytest.approx(97.0)


def test_interpolated_bars_are_excluded_and_not_counted(ingest, tmp_path):
    bars = _falling_bars() + [_raw_bar(_et(10, 25), 50.0, interpolated=True)]
    code, out = _run(ingest, tmp_path, _build(tmp_path, bars_by={"AAA": bars}))
    assert code == 0
    payload = _payload(out)
    assert len(payload["sources"]["robinhood"]["AAA"]["intraday_bars"]) == 12
    assert payload["ingest"]["bar_counts"]["AAA"] == 12


def test_symbols_the_response_echoes_but_nobody_asked_for_are_ignored(ingest, tmp_path):
    files = _build(tmp_path, tickers=(*TICKERS, "ZZZ"))
    code, out = _run(ingest, tmp_path, files)  # ZZZ is in the responses, not in --expected-tickers
    assert code == 0
    payload = _payload(out)
    assert "ZZZ" not in payload["sources"]["robinhood"]
    assert "ZZZ" not in payload["ticker_errors"]["robinhood"]


def test_chunked_responses_are_merged(ingest, tmp_path):
    """The adapter fetches 25 tickers per call, so a universe arrives as several responses."""
    first = _write(tmp_path, "bars_a.json", _historicals({"AAA": _falling_bars(), "BBB": _falling_bars()}))
    second = _write(tmp_path, "bars_b.json", _historicals({"CCC": _falling_bars()}))
    files = _build(tmp_path)
    files["bars_5m"] = [first, second]
    code, out = _run(ingest, tmp_path, files)
    assert code == 0
    assert sorted(_payload(out)["sources"]["robinhood"]) == list(TICKERS)


def _role_response(role, symbols):
    """A healthy raw response for ``role`` carrying exactly ``symbols``."""
    if role == "bars_5m":
        return _historicals({t: _falling_bars() for t in symbols})
    if role == "bars_daily":
        return _historicals({t: [_daily_bar()] for t in symbols})
    return _quotes({t: _quote_body(t) for t in symbols})


@pytest.mark.parametrize("role", ["bars_5m", "bars_daily", "quotes"])
def test_the_same_symbol_in_two_responses_is_refused(ingest, tmp_path, capsys, role):
    """Two answers for one ticker is ambiguous; picking one would be a silent choice.

    Every role is repeatable and quotes have their own response shape, so the refusal is pinned
    for all three (Copilot review, PR #1609): an implementation that silently overwrote a
    duplicate daily-bar or quote response would otherwise pass.
    """
    first = _write(tmp_path, f"{role}_a.json", _role_response(role, ("AAA", "BBB")))
    second = _write(tmp_path, f"{role}_b.json", _role_response(role, ("BBB", "CCC")))
    files = _build(tmp_path)
    files[role] = [first, second]
    _assert_refused(ingest, tmp_path, files, "duplicate_symbol", capsys)


def test_mcp_text_content_block_wrapper_is_unwrapped(ingest, tmp_path):
    files = _build(tmp_path)
    for role in ("bars_5m", "bars_daily", "quotes"):
        path = files[role][0]
        text = open(path, encoding="utf-8").read()
        _write(tmp_path, path.rsplit("/", 1)[1], [{"type": "text", "text": text}])
    code, out = _run(ingest, tmp_path, files)
    assert code == 0
    assert sorted(_payload(out)["sources"]["robinhood"]) == list(TICKERS)


# --- partial ticker failure ---------------------------------------------------------


def test_partial_ticker_failure_names_the_ticker_and_keeps_the_rest(ingest, tmp_path):
    code, out = _run(ingest, tmp_path, _build(tmp_path, bars_by={"BBB": None}))
    assert code == 0
    payload = _payload(out)
    assert sorted(payload["sources"]["robinhood"]) == ["AAA", "CCC"]
    assert payload["ticker_errors"] == {"robinhood": ["BBB"]}
    assert payload["ingest"]["ticker_failures"] == {"BBB": "no_bars"}
    assert payload["source_errors"] == []


_INTERPOLATED_ONLY = [_raw_bar(_et(9, 30), 99.0, interpolated=True)]
_TODAYS_DAILY_ONLY = [_daily_bar(DAY)]


@pytest.mark.parametrize(
    ("reason", "overrides"),
    [
        ("no_bars", {"bars_by": {"BBB": None}}),
        ("no_bars", {"bars_by": {"BBB": _INTERPOLATED_ONLY}}),
        ("insufficient_bars", {"bars_by": {"BBB": _falling_bars()[:5]}}),
        ("bar_outside_window", {"bars_by": {"BBB": [_raw_bar(_et(9, 25), 99.0), *_falling_bars()]}}),
        ("bar_outside_window", {"bars_by": {"BBB": [*_falling_bars(), _raw_bar(_et(10, 35), 95.0)]}}),
        ("bar_outside_window", {"bars_by": {"BBB": [_raw_bar(_et(10, 0, day=PREVIOUS_SESSION), 99.0), *_falling_bars()]}}),
        ("non_finite_value", {"bars_by": {"BBB": [*_falling_bars(), _raw_bar(_et(10, 30), 95.0, close_price="NaN")]}}),
        ("non_finite_value", {"bars_by": {"BBB": [*_falling_bars()[:-1], _raw_bar(_et(10, 25), 95.0, close_price=None)]}}),
        ("bar_ohlc_inconsistent", {"bars_by": {"BBB": [*_falling_bars()[:-1], _raw_bar(_et(10, 25), 95.0, close_price="96.5000")]}}),
        ("bar_ohlc_inconsistent", {"bars_by": {"BBB": [*_falling_bars()[:-1], _raw_bar(_et(10, 25), 95.0, close_price="0")]}}),
        # Two REAL bars at one timestamp, and a bar closer than one interval to its neighbour: the
        # Gate 5 loader rejects both and would abort the whole run, so ingest must fail the ONE
        # ticker instead (Copilot review, PR #1609).
        ("duplicate_bar_timestamp", {"bars_by": {"BBB": [*_falling_bars(), _raw_bar(_et(10, 25), 94.0)]}}),
        ("bar_spacing_invalid", {"bars_by": {"BBB": [*_falling_bars(), _raw_bar(_et(10, 27), 95.0)]}}),
        # Only a stale daily bar: the newest one is Mon Aug 3 for a Wed Aug 5 run, so the Tue Aug 4
        # session is missing. "Latest date before the trading day" is not enough; it must be the
        # immediately previous session, which the candidate loader enforces.
        ("no_previous_close", {"daily_by": {"BBB": [_daily_bar(date(2026, 8, 3))]}}),
        ("no_previous_close", {"daily_by": {"BBB": None}}),
        ("no_previous_close", {"daily_by": {"BBB": _TODAYS_DAILY_ONLY}}),
        ("no_quote", {"quote_by": {"BBB": None}}),
        ("no_quote", {"quote_by": {"BBB": _quote_body("BBB", has_traded=False)}}),
        ("no_quote", {"quote_by": {"BBB": _quote_body("BBB", state="inactive")}}),
        ("no_quote", {"quote_by": {"BBB": _quote_body("BBB", price="0")}}),
        ("no_quote", {"quote_by": {"BBB": _quote_body("BBB", price="NaN")}}),
    ],
)
def test_a_bad_ticker_is_reported_with_its_reason_never_trimmed_or_kept(ingest, tmp_path, reason, overrides):
    code, out = _run(ingest, tmp_path, _build(tmp_path, **overrides))
    assert code == 0
    payload = _payload(out)
    assert "BBB" not in payload["sources"]["robinhood"]
    assert payload["ticker_errors"] == {"robinhood": ["BBB"]}
    assert payload["ingest"]["ticker_failures"] == {"BBB": reason}
    assert sorted(payload["sources"]["robinhood"]) == ["AAA", "CCC"]


def test_every_ticker_failing_is_refused(ingest, tmp_path, capsys):
    """Responses are non-empty, but no ticker survives its own checks: nothing to ingest."""
    files = _build(tmp_path, bars_by={t: _falling_bars()[:5] for t in TICKERS})
    _assert_refused(ingest, tmp_path, files, "no_tickers_ingested", capsys)


_NUMERIC_BAD_VALUES = ["NaN", "Infinity", "-Infinity", True, False]
_BAR_NUMERIC_FIELDS = ["open_price", "high_price", "low_price", "close_price", "volume"]


@pytest.mark.parametrize("value", _NUMERIC_BAD_VALUES)
@pytest.mark.parametrize("field", _BAR_NUMERIC_FIELDS)
def test_a_non_finite_or_boolean_intraday_field_fails_that_ticker(ingest, tmp_path, field, value):
    """Every emitted numeric field is checked, not just ``close_price``; JSON booleans are not numbers."""
    bars = [*_falling_bars()[:-1], _raw_bar(_et(10, 25), 95.0, **{field: value})]
    code, out = _run(ingest, tmp_path, _build(tmp_path, bars_by={"BBB": bars}))
    assert code == 0
    payload = _payload(out)
    assert "BBB" not in payload["sources"]["robinhood"]
    assert payload["ingest"]["ticker_failures"] == {"BBB": "non_finite_value"}


@pytest.mark.parametrize("value", _NUMERIC_BAD_VALUES)
@pytest.mark.parametrize("field", _BAR_NUMERIC_FIELDS)
def test_a_non_finite_or_boolean_previous_close_field_fails_that_ticker(ingest, tmp_path, field, value):
    code, out = _run(ingest, tmp_path, _build(tmp_path, daily_by={"BBB": [_daily_bar(**{field: value})]}))
    assert code == 0
    payload = _payload(out)
    assert "BBB" not in payload["sources"]["robinhood"]
    assert payload["ingest"]["ticker_failures"] == {"BBB": "non_finite_value"}


@pytest.mark.parametrize("price", [True, False])
def test_a_boolean_quote_price_fails_that_ticker(ingest, tmp_path, price):
    code, out = _run(ingest, tmp_path, _build(tmp_path, quote_by={"BBB": _quote_body("BBB", price=price)}))
    assert code == 0
    assert _payload(out)["ingest"]["ticker_failures"] == {"BBB": "no_quote"}


def test_both_window_endpoints_are_inclusive(ingest, tmp_path):
    """The window is [start, end]: a healthy 10:30 bar must be kept (``ts < end`` would drop it),
    and the 09:30 opening bar the fixture already carries sits on the start boundary."""
    bars = [*_falling_bars(), _raw_bar(_et(10, 30), 95.0)]
    code, out = _run(ingest, tmp_path, _build(tmp_path, bars_by={"BBB": bars}))
    assert code == 0
    payload = _payload(out)
    got = payload["sources"]["robinhood"]["BBB"]["intraday_bars"]
    assert len(got) == 13
    assert datetime.fromisoformat(got[0]["timestamp"]) == _et(9, 30)
    assert datetime.fromisoformat(got[-1]["timestamp"]) == _et(10, 30)
    assert payload["ticker_errors"] == {"robinhood": []}


def test_previous_session_across_a_weekend_is_the_friday_bar(ingest, tmp_path):
    """A Monday run's previous session is Friday. A Thursday-only response skips Friday, so that
    ticker fails with ``no_previous_close`` while the Friday-backed tickers ingest."""
    monday, friday, thursday = date(2026, 8, 10), date(2026, 8, 7), date(2026, 8, 6)
    bars = [_raw_bar(_et(9, 30, day=monday) + timedelta(minutes=5 * i), 95.0) for i in range(12)]
    quote = lambda t: _quote_body(  # noqa: E731 - a one-line test builder
        t, venue_last_trade_time=_iso_z(_et(10, 29, day=monday)), previous_close_date=friday.isoformat()
    )
    files = _build(
        tmp_path,
        bars_by={t: bars for t in TICKERS},
        daily_by={"AAA": [_daily_bar(friday)], "BBB": [_daily_bar(thursday)], "CCC": [_daily_bar(friday)]},
        quote_by={t: quote(t) for t in TICKERS},
    )
    code, out = _run(ingest, tmp_path, files, extra=("--trading-day", monday.isoformat()))
    assert code == 0
    payload = _payload(out)
    assert sorted(payload["sources"]["robinhood"]) == ["AAA", "CCC"]
    assert payload["ingest"]["ticker_failures"] == {"BBB": "no_previous_close"}


# --- empty and malformed inputs: whole-input refusal ---------------------------------


@pytest.mark.parametrize("role", ["bars_5m", "bars_daily", "quotes"])
def test_an_empty_results_response_is_refused(ingest, tmp_path, capsys, role):
    files = _build(tmp_path)
    files[role] = [_write(tmp_path, "empty.json", {"data": {"results": []}})]
    _assert_refused(ingest, tmp_path, files, "empty_response", capsys)


@pytest.mark.parametrize("role", ["bars_5m", "bars_daily", "quotes"])
def test_a_missing_input_role_is_refused(ingest, tmp_path, capsys, role):
    files = _build(tmp_path)
    files[role] = []
    _assert_refused(ingest, tmp_path, files, "missing_input_role", capsys)


def test_a_truncated_response_file_is_refused(ingest, tmp_path, capsys):
    files = _build(tmp_path)
    full = open(files["bars_5m"][0], encoding="utf-8").read()
    files["bars_5m"] = [_write(tmp_path, "cut.json", full[: len(full) // 2])]
    _assert_refused(ingest, tmp_path, files, "input_unparseable", capsys)


def test_duplicate_json_keys_are_refused(ingest, tmp_path, capsys):
    files = _build(tmp_path)
    files["quotes"] = [_write(tmp_path, "dup.json", '{"data": {"results": []}, "data": {"results": []}}')]
    _assert_refused(ingest, tmp_path, files, "input_unparseable", capsys)


def test_a_bare_nan_token_is_refused(ingest, tmp_path, capsys):
    """``json.loads`` accepts a bare NaN by default; a payload carrying one must not parse."""
    files = _build(tmp_path)
    text = open(files["bars_5m"][0], encoding="utf-8").read().replace('"close_price": "99.0000"', '"close_price": NaN', 1)
    assert "NaN" in text
    files["bars_5m"] = [_write(tmp_path, "nan.json", text)]
    _assert_refused(ingest, tmp_path, files, "input_unparseable", capsys)


def test_a_text_block_holding_an_error_message_is_refused(ingest, tmp_path, capsys):
    files = _build(tmp_path)
    files["bars_5m"] = [_write(tmp_path, "err.json", [{"type": "text", "text": "Error: output too large"}])]
    _assert_refused(ingest, tmp_path, files, "input_unparseable", capsys)


def test_an_unreadable_input_file_is_refused(ingest, tmp_path, capsys):
    files = _build(tmp_path)
    files["quotes"] = [str(tmp_path / "nope.json")]
    _assert_refused(ingest, tmp_path, files, "input_unreadable", capsys)


def test_two_stdin_inputs_are_refused(ingest, tmp_path, capsys, monkeypatch):
    files = _build(tmp_path)
    files["bars_daily"] = ["-"]
    files["quotes"] = ["-"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    _assert_refused(ingest, tmp_path, files, "multiple_stdin_inputs", capsys)


def test_refusal_leaves_an_existing_payload_untouched(ingest, tmp_path):
    out = tmp_path / "market_data_out.json"
    out.write_text('{"sentinel": true}', encoding="utf-8")
    files = _build(tmp_path)
    files["bars_5m"] = [_write(tmp_path, "cut.json", "{")]
    code, _ = _run(ingest, tmp_path, files)
    assert code == 2
    assert out.read_text(encoding="utf-8") == '{"sentinel": true}'


def test_unwritable_out_path_is_refused(ingest, tmp_path, capsys):
    files = _build(tmp_path)
    argv = [
        "--trading-day", DAY.isoformat(), "--window-start-et", "09:30", "--window-end-et", "10:30",
        "--expected-tickers", ",".join(TICKERS), "--min-bars", "6",
        "--out", str(tmp_path / "no_such_dir" / "out.json"),
        "--bars-5m", files["bars_5m"][0], "--bars-daily", files["bars_daily"][0], "--quotes", files["quotes"][0],
    ]
    assert ingest.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "out_unwritable"


# --- provenance and content hashes ---------------------------------------------------


def test_file_inputs_are_recorded_as_spilled_file_with_their_own_hashes(ingest, tmp_path):
    files = _build(tmp_path)
    code, out = _run(ingest, tmp_path, files)
    assert code == 0
    block = _payload(out)["ingest"]
    assert block["tool"] == "scripts.ingest_market_data"
    assert block["provenance"] == "spilled_file"
    by_role = {item["role"]: item for item in block["inputs"]}
    assert set(by_role) == {"bars_5m", "bars_daily", "quotes"}
    for role, item in by_role.items():
        raw = open(files[role][0], "rb").read()
        assert item["sha256"] == hashlib.sha256(raw).hexdigest()
        assert item["bytes"] == len(raw)
        assert item["source"] == "file"


def test_a_stdin_input_downgrades_provenance_to_stdin_paste(ingest, tmp_path, monkeypatch):
    files = _build(tmp_path)
    text = open(files["quotes"][0], encoding="utf-8").read()
    files["quotes"] = ["-"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code, out = _run(ingest, tmp_path, files)
    assert code == 0
    block = _payload(out)["ingest"]
    assert block["provenance"] == "stdin_paste"
    quotes = next(item for item in block["inputs"] if item["role"] == "quotes")
    assert quotes["source"] == "stdin"
    assert quotes["sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_payload_hash_is_the_canonical_hash_of_the_sources_and_is_reported(ingest, tmp_path, capsys):
    code, out = _run(ingest, tmp_path, _build(tmp_path))
    assert code == 0
    payload = _payload(out)
    digest = hashlib.sha256(_canonical(payload["sources"])).hexdigest()
    assert payload["ingest"]["payload_sha256"] == digest
    assert json.loads(capsys.readouterr().out)["payload_sha256"] == digest


def test_the_same_inputs_give_the_same_hash_and_one_changed_value_changes_it(ingest, tmp_path):
    first_dir, second_dir, third_dir = (tmp_path / n for n in ("a", "b", "c"))
    for directory in (first_dir, second_dir, third_dir):
        directory.mkdir()
    _, out_a = _run(ingest, first_dir, _build(first_dir))
    _, out_b = _run(ingest, second_dir, _build(second_dir))
    changed = _falling_bars()
    changed[3] = _raw_bar(_et(9, 45), 98.0)
    _, out_c = _run(ingest, third_dir, _build(third_dir, bars_by={"AAA": changed}))
    assert _payload(out_a)["ingest"]["payload_sha256"] == _payload(out_b)["ingest"]["payload_sha256"]
    assert _payload(out_a)["ingest"]["payload_sha256"] != _payload(out_c)["ingest"]["payload_sha256"]


def test_the_evidence_block_records_what_was_expected_and_what_was_found(ingest, tmp_path):
    code, out = _run(ingest, tmp_path, _build(tmp_path, quote_by={"CCC": None}))
    assert code == 0
    block = _payload(out)["ingest"]
    assert block["trading_day"] == DAY.isoformat()
    assert block["window_et"] == ["09:30", "10:30"]
    assert block["tickers_expected"] == list(TICKERS)
    assert block["tickers_ingested"] == ["AAA", "BBB"]
    assert block["ticker_failures"] == {"CCC": "no_quote"}
    assert block["bar_counts"] == {"AAA": 12, "BBB": 12}


# --- the payload is what the real Gate 5 generator consumes ---------------------------


def test_the_payload_feeds_the_morning_generator_and_scores_as_robinhood(ingest, tmp_path):
    """Contract test: ingest -> generate_morning_candidates, no hand-written payload between."""
    from test_generate_morning_candidates import _config
    from test_generate_morning_candidates import _run as generate

    files = _build(tmp_path, tickers=("AAA",))
    code, out = _run(ingest, tmp_path, files, tickers=("AAA",))
    assert code == 0
    document = generate(_payload(out), _config(tickers=("AAA",)))
    assert document["source"] == "robinhood"
    assert document["provider_error_count"] == 0
    assert document["buy_count"] == 1


# --- argument and calendar refusals, and the shared-loader safety net -----------------


@pytest.mark.parametrize("bad_day", ["", "2026-8-5", "20260805", "today"])
def test_a_malformed_trading_day_is_refused(ingest, tmp_path, capsys, bad_day):
    _assert_refused(ingest, tmp_path, _build(tmp_path), "invalid_trading_day", capsys, extra=("--trading-day", bad_day))


@pytest.mark.parametrize(
    "override",
    [("--window-end-et", "09:00"), ("--window-end-et", "09:30"), ("--window-start-et", "9:3x"), ("--window-end-et", "")],
)
def test_an_unusable_window_is_refused(ingest, tmp_path, capsys, override):
    _assert_refused(ingest, tmp_path, _build(tmp_path), "invalid_window", capsys, extra=override)


def test_an_empty_expected_ticker_list_is_refused(ingest, tmp_path, capsys):
    _assert_refused(ingest, tmp_path, _build(tmp_path), "no_expected_tickers", capsys, tickers=())


def test_a_trading_day_the_calendar_cannot_resolve_is_refused(ingest, tmp_path, capsys):
    """No guessing the previous session: a day outside config/market_calendar.json fails closed."""
    _assert_refused(
        ingest, tmp_path, _build(tmp_path), "previous_session_unknown", capsys, extra=("--trading-day", "2099-01-05")
    )


def test_a_response_with_no_results_list_is_refused(ingest, tmp_path, capsys):
    files = _build(tmp_path)
    files["quotes"] = [_write(tmp_path, "odd.json", {"data": {"status": "ok"}})]
    _assert_refused(ingest, tmp_path, files, "input_unparseable", capsys)


def test_a_result_that_is_not_an_object_is_refused(ingest, tmp_path, capsys):
    files = _build(tmp_path)
    files["bars_daily"] = [_write(tmp_path, "odd.json", {"data": {"results": ["AAA"]}})]
    _assert_refused(ingest, tmp_path, files, "input_unparseable", capsys)


def test_an_unparseable_bar_timestamp_fails_that_ticker(ingest, tmp_path):
    bars = [*_falling_bars()[:-1], _raw_bar(_et(10, 25), 95.0, begins_at="not-a-time")]
    code, out = _run(ingest, tmp_path, _build(tmp_path, bars_by={"BBB": bars}))
    assert code == 0
    assert _payload(out)["ingest"]["ticker_failures"] == {"BBB": "bar_timestamp_invalid"}


def test_a_row_the_shared_loader_rejects_fails_that_ticker_even_if_our_checks_passed(ingest, tmp_path, monkeypatch):
    """The last gate: whatever the Gate 5 loader's own parser would abort on never gets written."""
    from scripts.generate_buy_candidates import _parse_bar as real_parse_bar

    module = importlib.import_module("scripts.ingest_market_data")

    def strict(raw, **kwargs):
        if raw.get("close") == 77.0:
            raise ValueError("loader says no")
        return real_parse_bar(raw, **kwargs)

    monkeypatch.setattr(module, "_parse_bar", strict)
    bars = [*_falling_bars()[:-1], _raw_bar(_et(10, 25), 77.0)]
    code, out = _run(module, tmp_path, _build(tmp_path, bars_by={"BBB": bars}))
    assert code == 0
    payload = _payload(out)
    assert payload["ingest"]["ticker_failures"] == {"BBB": "loader_rejected"}
    assert sorted(payload["sources"]["robinhood"]) == ["AAA", "CCC"]


def test_todays_forming_daily_bar_is_never_the_previous_close(ingest, tmp_path):
    """Dated by its own session, a 00:00Z bar for TODAY is not the previous session (the loader's
    ET-first dating would read it as yesterday evening, so ingest is deliberately stricter)."""
    daily = [_daily_bar(DAY, close=95.0, low=94.0)]
    code, out = _run(ingest, tmp_path, _build(tmp_path, daily_by={"BBB": daily}))
    assert code == 0
    assert _payload(out)["ingest"]["ticker_failures"] == {"BBB": "no_previous_close"}


def test_the_command_runs_as_a_module_from_the_repo_root(tmp_path):
    import subprocess
    from pathlib import Path

    files = _build(tmp_path)
    out = tmp_path / "market_data_out.json"
    argv = [
        sys.executable, "-m", "scripts.ingest_market_data",
        "--trading-day", DAY.isoformat(), "--window-start-et", "09:30", "--window-end-et", "10:30",
        "--expected-tickers", ",".join(TICKERS), "--min-bars", "6", "--out", str(out),
        "--bars-5m", files["bars_5m"][0], "--bars-daily", files["bars_daily"][0], "--quotes", files["quotes"][0],
    ]
    done = subprocess.run(argv, cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["ok"] is True
    assert sorted(_payload(out)["sources"]["robinhood"]) == list(TICKERS)


@pytest.mark.parametrize("override", [("--min-bars", "0"), ("--interval-minutes", "0"), ("--interval-minutes", "-5")])
def test_a_non_positive_min_bars_or_interval_is_refused(ingest, tmp_path, capsys, override):
    _assert_refused(ingest, tmp_path, _build(tmp_path), "invalid_argument", capsys, extra=override)


def test_a_daily_bar_stamped_at_midnight_eastern_is_dated_by_its_eastern_session(ingest, tmp_path):
    """Not every daily stamp is 00:00Z; one at 04:00Z (midnight EDT) belongs to its ET date."""
    daily = [_daily_bar(PREVIOUS_SESSION, begins_at=f"{PREVIOUS_SESSION.isoformat()}T04:00:00Z")]
    code, out = _run(ingest, tmp_path, _build(tmp_path, daily_by={"BBB": daily}))
    assert code == 0
    assert _payload(out)["ticker_errors"] == {"robinhood": []}
