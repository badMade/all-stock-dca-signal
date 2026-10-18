"""Proof of concept against the live Massive and Alpaca APIs (PLAN-price-source.md §7).

Opt-in only:

    PRICE_CONTRACT=1 MASSIVE_API_KEY=... ALPACA_KEY_ID=... ALPACA_SECRET_KEY=... \\
        DCA_TICKER_HISTORY_DIR=/path/to/ticker_data \\
        python -m pytest tests/prices/contract -m contract

20 tickers over the last 60 closed sessions: S&P 500 names including NVDA, plus a
reverse-split name, a high-dividend name, a delisted name and an IPO. The brief's "15 S&P
names + 4 roles" totals 19, so a 16th S&P name (COST) makes 20; flagged to the operator. The last four are
discovered from the data (the events fall after the author's knowledge cutoff), never
hard-coded. Writes docs/price_source_poc_report.md and re-records redacted live payloads
into tests/prices/fixtures/recorded/ for test_fixture_shapes.py.

The free Massive tier allows 5 calls a minute, so this takes roughly 15 minutes.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REQUIRED_ENV = ("MASSIVE_API_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY", "DCA_TICKER_HISTORY_DIR")
CORE_16 = (
    "NVDA",
    "AAPL",
    "MSFT",
    "AMZN",
    "GOOGL",
    "META",
    "JPM",
    "XOM",
    "JNJ",
    "PG",
    "KO",
    "WMT",
    "HD",
    "UNH",
    "V",
    "COST",
)
PACKAGE_ROOT = Path(__file__).resolve().parents[3]
REPO_ROOT = PACKAGE_ROOT.parent

pytestmark = [
    pytest.mark.contract,
    pytest.mark.skipif(
        os.environ.get("PRICE_CONTRACT") != "1" or not all(os.environ.get(k) for k in REQUIRED_ENV),
        reason="opt-in live test: set PRICE_CONTRACT=1 and " + ", ".join(REQUIRED_ENV),
    ),
]


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    from market_data_ingest.prices.poc import run_poc

    return run_poc(
        env=os.environ,
        db_path=tmp_path_factory.mktemp("poc") / "poc.duckdb",
        yahoo_dir=Path(os.environ["DCA_TICKER_HISTORY_DIR"]),
        core_symbols=CORE_16,
        n_sessions=60,
        report_path=REPO_ROOT / "docs" / "price_source_poc_report.md",
        record_dir=PACKAGE_ROOT / "tests" / "prices" / "fixtures" / "recorded",
    )


def test_universe_is_16_core_names_plus_4_discovered_roles_totalling_20(report):
    assert len(report.symbols) == 20
    assert set(CORE_16) <= set(report.symbols)
    assert set(report.roles) == {"reverse_split", "high_dividend", "delisted", "ipo"}
    discovered = list(report.roles.values())
    assert len(set(discovered)) == 4 and not set(discovered) & set(CORE_16)
    assert len(set(report.symbols)) == 20


def test_window_is_60_closed_sessions(report):
    assert len(report.sessions) == 60
    assert report.sessions == sorted(report.sessions)


def test_nvda_is_present_in_every_session(report):
    assert report.present_sessions["NVDA"] == report.sessions


def test_reverse_split_name_has_a_reverse_split_inside_the_window(report):
    sym = report.roles["reverse_split"]
    assert any(
        a.action_type == "reverse_split" and report.sessions[0] <= a.ex_date <= report.sessions[-1]
        for a in report.actions[sym]
    )


def test_delisted_name_appears_through_its_last_trading_date(report):
    sym = report.roles["delisted"]
    present = report.present_sessions[sym]
    last = report.delisted_last_session
    assert present[-1] == last, "the symbol is in every grouped batch up to and including its last session"
    assert last < report.sessions[-1], "and absent after it"
    assert present == [s for s in report.sessions if s <= last][-len(present) :]


def test_ipo_name_first_appears_inside_the_window(report):
    sym = report.roles["ipo"]
    assert report.present_sessions[sym][0] > report.sessions[0]


def test_every_symbol_has_a_yahoo_diff_or_an_explicit_reason_it_has_none(report):
    for sym in report.symbols:
        diff = report.yahoo_diffs.get(sym)
        assert diff is not None or sym in report.no_yahoo_reason, sym
        if diff is not None:
            assert diff.close_status in {"pass", "flag", "block", "insufficient"}
            assert diff.adj_close_worst_diff_pct is not None


def test_report_and_redacted_recordings_were_written(report):
    text = (REPO_ROOT / "docs" / "price_source_poc_report.md").read_text()
    for sym in report.symbols:
        assert sym in text
    for key in REQUIRED_ENV[:3]:
        assert os.environ[key] not in text
    recorded = PACKAGE_ROOT / "tests" / "prices" / "fixtures" / "recorded"
    assert list(recorded.glob("massive_grouped_*.json")) and list(recorded.glob("massive_splits*.json"))
    for f in recorded.glob("*.json"):
        body = f.read_text()
        assert all(os.environ[k] not in body for k in REQUIRED_ENV[:3]), f.name
