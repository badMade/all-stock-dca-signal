"""Tests for ``scripts/assemble_market_data.py``: raw Robinhood responses in, the generator's payload out.

Written test-first, no implementation yet.

Why it exists: the safety gate runs the primary's own candidate generator, which reads one payload shape
(``scripts.generate_buy_candidates`` docstring, "INPUT SHAPE"). The Perplexity task must not reshape
broker responses itself (that is the compute, and the credit cost, this design removes). It dumps the raw
``get_equity_historicals`` and ``get_equity_quotes`` responses it fetched into ONE file and runs this
script, which parses them with the repo's own ``RobinhoodDataProvider`` so the parsing is the production
parsing, not a second copy.

Contract:

* Input ``--raw FILE``: ``{"historicals": [<raw response>, ...], "quotes": [<raw response>, ...]}``. A response
  may carry one symbol or many; several responses may cover one symbol (pages).
* Output ``--out FILE``: ``{"sources": {"finance": {}, "robinhood": {T: {previous_close_bar, intraday_bars,
  quote}}}, "source_errors": [], "ticker_errors": {"finance": [], "robinhood": [...]}}``.
  ``finance`` is an explicit ``{}``: this task fetches Robinhood only, and the backup policy is that a
  finance NO_DATA does not block a Robinhood-complete buy (the generator reads ``{}`` that way).
* A universe ticker with missing, partial or unusable raw data is listed under ``ticker_errors.robinhood`` (an
  explicit error marker, which blocks an auto-buy for it) rather than silently dropped.
* All-or-nothing on the input as a whole: unreadable or malformed input writes nothing and exits 2.
* One JSON document on stdout; exit 0 on success, 2 on any failure. Deterministic bytes. No network, no broker.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from market_data_fixtures import DAY, market_data, raw_bundle, raw_historicals, raw_quotes  # noqa: E402

import dca_config  # noqa: E402
from scripts import assemble_market_data as amd  # noqa: E402
from scripts import generate_buy_candidates as gbc  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 7, 15, 30, tzinfo=ZoneInfo("America/New_York"))
TAPE = {"VT": (100.0, 97.0), "MSFT": (100.0, 100.0)}


@pytest.fixture(scope="module")
def config():
    return dca_config.load_strategy_config(dca_config.get_config_dir())


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj))
    return path


def _assemble(tmp_path, capsys, raw, *, out_name="market.json"):
    out = tmp_path / out_name
    code = amd.main(["--trading-day", DAY, "--raw", str(_write(tmp_path, "raw.json", raw)), "--out", str(out)])
    report = json.loads(capsys.readouterr().out)
    return code, report, out


def _decisions(config, payload):
    rows = gbc.generate_candidates(
        slot="1530ET", now=NOW, config=config, market_data_by_source=payload["sources"],
        source_errors=payload["source_errors"], ticker_errors=payload["ticker_errors"],
    )
    return {r["ticker"]: r for r in rows}


# ---------------------------------------------------------------------------
# The payload it writes
# ---------------------------------------------------------------------------


def test_it_writes_the_generators_payload_shape(tmp_path, capsys):
    code, report, out = _assemble(tmp_path, capsys, raw_bundle(TAPE))
    assert (code, report["status"]) == (0, "ASSEMBLED")
    payload = json.loads(out.read_text())
    assert set(payload) == {"sources", "source_errors", "ticker_errors"}
    assert payload["sources"]["finance"] == {}
    assert set(payload["sources"]["robinhood"]) == set(TAPE)
    assert payload["source_errors"] == [] and payload["ticker_errors"]["finance"] == []
    # Every universe ticker this fixture did not fetch is flagged (a partial fetch must not read as a quiet tape);
    # the two it did fetch are not.
    assert set(payload["ticker_errors"]["robinhood"]).isdisjoint(TAPE) and payload["ticker_errors"]["robinhood"]
    entry = payload["sources"]["robinhood"]["VT"]
    assert set(entry) == {"previous_close_bar", "intraday_bars", "quote"}
    assert entry["quote"]["price"] == 97.0 and entry["quote"]["as_of"]
    assert all(b["timestamp"].endswith(("-04:00", "+00:00", "Z")) for b in entry["intraday_bars"])  # tz-aware


def test_the_previous_close_comes_from_the_official_close_with_the_unknown_low_sentinel(tmp_path, capsys):
    _, _, out = _assemble(tmp_path, capsys, raw_bundle(TAPE))
    bar = json.loads(out.read_text())["sources"]["robinhood"]["VT"]["previous_close_bar"]
    assert bar["close"] == 100.0
    assert bar["low"] == 0.0  # "unknown": a quote-derived bar must not fabricate a low that upgrades the base to $2
    assert bar["timestamp"] < f"{DAY}T00:00:00"  # strictly before the trading day


def test_interpolated_gap_fill_bars_are_dropped_and_bars_are_sorted_oldest_first(tmp_path, capsys):
    raw = {"historicals": [raw_historicals("VT", 100.0, 97.0, interpolated_at=3)], "quotes": [raw_quotes("VT", 100.0, 97.0)]}
    raw["historicals"][0]["results"][0]["bars"].reverse()
    _, _, out = _assemble(tmp_path, capsys, raw)
    bars = json.loads(out.read_text())["sources"]["robinhood"]["VT"]["intraday_bars"]
    stamps = [b["timestamp"] for b in bars]
    assert stamps == sorted(stamps)
    assert len(bars) == len(raw_historicals("VT", 100.0, 97.0)["results"][0]["bars"]) - 1


def test_a_multi_symbol_response_and_split_pages_assemble_the_same_as_one_per_symbol(tmp_path, capsys):
    merged_hist = raw_historicals("VT", 100.0, 97.0)
    merged_hist["results"] += raw_historicals("MSFT", 100.0, 100.0)["results"]
    merged_quotes = raw_quotes("VT", 100.0, 97.0)
    merged_quotes["results"] += raw_quotes("MSFT", 100.0, 100.0)["results"]
    _, _, one = _assemble(tmp_path, capsys, {"historicals": [merged_hist], "quotes": [merged_quotes]}, out_name="a.json")
    _, _, two = _assemble(tmp_path, capsys, raw_bundle(TAPE), out_name="b.json")
    assert json.loads(one.read_text()) == json.loads(two.read_text())


# ---------------------------------------------------------------------------
# It feeds the primary's generator and the answer is right
# ---------------------------------------------------------------------------


def test_the_assembled_payload_gives_the_same_decisions_as_a_hand_built_one(tmp_path, capsys, config):
    _, _, out = _assemble(tmp_path, capsys, raw_bundle(TAPE))
    assembled = _decisions(config, json.loads(out.read_text()))
    direct = _decisions(config, market_data(TAPE))
    for ticker in TAPE:
        keys = ("action", "reason", "buy_multiplier", "suggested_amount_usd", "target_price")
        assert {k: assembled[ticker][k] for k in keys} == {k: direct[ticker][k] for k in keys}
    assert assembled["VT"]["action"] == "BUY" and assembled["MSFT"]["action"] != "BUY"
    assert "finance source is unavailable" in assembled["VT"]["reason"]  # the Robinhood-only path the policy allows


# ---------------------------------------------------------------------------
# Missing and bad data is flagged, never dropped
# ---------------------------------------------------------------------------


def test_a_ticker_with_no_raw_data_at_all_is_an_explicit_error_marker_and_cannot_be_bought(tmp_path, capsys, config):
    raw = raw_bundle({"MSFT": (100.0, 100.0)})
    _, report, out = _assemble(tmp_path, capsys, raw)
    payload = json.loads(out.read_text())
    assert "AAPL" in payload["ticker_errors"]["robinhood"]  # in the universe, nothing was fetched for it
    assert "AAPL" not in payload["sources"]["robinhood"]
    assert report["ticker_errors"] == payload["ticker_errors"]["robinhood"]
    assert _decisions(config, payload)["AAPL"]["action"] != "BUY"


def test_a_ticker_with_bars_but_no_quote_is_an_error_marker(tmp_path, capsys):
    raw = raw_bundle(TAPE)
    raw["quotes"] = [raw_quotes("MSFT", 100.0, 100.0)]
    _, _, out = _assemble(tmp_path, capsys, raw)
    payload = json.loads(out.read_text())
    assert "VT" in payload["ticker_errors"]["robinhood"] and "VT" not in payload["sources"]["robinhood"]


def test_a_non_positive_or_non_finite_price_marks_the_ticker_instead_of_trading_on_it(tmp_path, capsys):
    bad = raw_bundle(TAPE)
    bad["quotes"][0]["results"][0]["quote"]["last_trade_price"] = "0"
    bad["quotes"][0]["results"][0]["close"]["price"] = "NaN"
    bad["quotes"][0]["results"][0]["quote"]["previous_close"] = "0"
    _, _, out = _assemble(tmp_path, capsys, bad)
    payload = json.loads(out.read_text())
    assert "VT" in payload["ticker_errors"]["robinhood"] and "VT" not in payload["sources"]["robinhood"]


def test_the_do_not_buy_ticker_is_never_assembled_or_flagged(tmp_path, capsys):
    raw = raw_bundle({**TAPE, "QBUT": (100.0, 90.0)})
    _, _, out = _assemble(tmp_path, capsys, raw)
    payload = json.loads(out.read_text())
    assert "QBUT" not in payload["sources"]["robinhood"] and "QBUT" not in payload["ticker_errors"]["robinhood"]


def test_a_symbol_outside_the_universe_is_ignored(tmp_path, capsys):
    raw = raw_bundle({**TAPE, "ZZZZ": (10.0, 9.0)})
    _, _, out = _assemble(tmp_path, capsys, raw)
    payload = json.loads(out.read_text())
    assert "ZZZZ" not in payload["sources"]["robinhood"] and "ZZZZ" not in payload["ticker_errors"]["robinhood"]


# ---------------------------------------------------------------------------
# Fail closed on the input as a whole
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["", "{oops", "[]", "null", '{"historicals": [], "quotes": []}',
                                  '{"historicals": "x", "quotes": []}', '{"historicals": [], "quotes": 3}',
                                  '{"historicals": [], "historicals": [], "quotes": []}'])
def test_unreadable_or_empty_input_writes_nothing_and_exits_2(tmp_path, capsys, text):
    code, report, out = _assemble(tmp_path, capsys, text)
    assert code == 2 and report["status"] == "FAILED" and report["errors"]
    assert not out.exists()


def test_a_missing_raw_file_fails_closed(tmp_path, capsys):
    out = tmp_path / "market.json"
    code = amd.main(["--trading-day", DAY, "--raw", str(tmp_path / "nope.json"), "--out", str(out)])
    report = json.loads(capsys.readouterr().out)
    assert (code, report["status"]) == (2, "FAILED") and not out.exists()


@pytest.mark.parametrize("day", ["2026-13-01", "10/07/2026", "", "tomorrow"])
def test_a_bad_trading_day_fails_closed(tmp_path, capsys, day):
    out = tmp_path / "market.json"
    code = amd.main(["--trading-day", day, "--raw", str(_write(tmp_path, "raw.json", raw_bundle(TAPE))), "--out", str(out)])
    assert code == 2 and not out.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "FAILED"


def test_a_failed_run_leaves_an_earlier_output_untouched(tmp_path, capsys):
    _, _, out = _assemble(tmp_path, capsys, raw_bundle(TAPE))
    before = out.read_bytes()
    code, _, _ = _assemble(tmp_path, capsys, "{oops")
    assert code == 2 and out.read_bytes() == before


# ---------------------------------------------------------------------------
# Hygiene
# ---------------------------------------------------------------------------


def test_the_output_is_deterministic_and_carries_no_account_number(tmp_path, capsys):
    _, _, first = _assemble(tmp_path, capsys, raw_bundle(TAPE), out_name="one.json")
    _, _, second = _assemble(tmp_path, capsys, raw_bundle(TAPE), out_name="two.json")
    assert first.read_bytes() == second.read_bytes()
    assert "999000111" not in first.read_text()


def test_the_assembler_is_stdlib_plus_repo_only_with_no_network_or_broker_calls():
    source = (REPO / "scripts" / "assemble_market_data.py").read_text()
    tree = ast.parse(source)
    repo_local = {"dca_live_adapters", "dca_config", "dca_runner", "near_close_dca", "scripts"}
    for node in ast.walk(tree):
        names = [a.name.split(".")[0] for a in node.names] if isinstance(node, ast.Import) else \
            [(node.module or "").split(".")[0]] if isinstance(node, ast.ImportFrom) else []
        for name in names:
            assert name in sys.stdlib_module_names or name in repo_local, name
    for forbidden in ("place_equity_order", "urllib.request", "socket", "requests", "http.client"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------
# Every saved response must be well-formed, not just the two outer lists (Copilot review on PR 1624)
# ---------------------------------------------------------------------------

_BAD_RESPONSES = [42, "x", None, [], {}, {"error": "rate limited"}, {"results": "nope"}, {"results": [42]},
                  {"results": [None]}, {"data": {"results": 3}}, {"data": 7}]


@pytest.mark.parametrize("bad", _BAD_RESPONSES, ids=lambda v: repr(v)[:30])
@pytest.mark.parametrize("where", ["historicals", "quotes"])
def test_one_malformed_saved_response_fails_the_whole_input_even_beside_valid_ones(tmp_path, capsys, where, bad):
    """A truncated or failed broker response beside good data would otherwise assemble quietly and hide an incomplete fetch."""
    raw = raw_bundle(TAPE)
    raw[where].append(bad)
    code, report, out = _assemble(tmp_path, capsys, raw)
    assert (code, report["status"]) == (2, "FAILED")
    assert report["errors"][0]["code"] == "raw_corrupt" and not out.exists()


def test_a_well_formed_response_with_no_results_is_still_fine(tmp_path, capsys):
    """An empty `results` list is a real answer (the broker knew nothing); the ticker is flagged, the input is not refused."""
    raw = raw_bundle(TAPE)
    raw["historicals"].append({"results": []})
    raw["quotes"].append({"data": {"results": []}})
    code, report, _ = _assemble(tmp_path, capsys, raw)
    assert (code, report["status"]) == (0, "ASSEMBLED")
