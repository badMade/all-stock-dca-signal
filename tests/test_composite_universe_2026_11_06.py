"""32 composite-rank S&P 500 names join the active DCA universe (2026-11-06, user-requested)."""
import csv
import json
from pathlib import Path

import near_close_dca

ROOT = Path(__file__).resolve().parent.parent
NEW = (
    "GWW KO ED HLT TRV ACGL EG BDX MAR RSG KDP MO AIZ CB KIM EXR AWK MRSH MSI EW VLTO WELL LH PSA CTAS SPG "
    "REGN DRI ALL EBAY CPAY CINF"
).split()


def _load(name):
    return json.loads((ROOT / "config" / name).read_text())


def test_new_names_are_active_in_constant_and_config():
    active = _load("tickers.json")["active"]
    for ticker in NEW:
        assert ticker in near_close_dca.ACTIVE_TICKERS
        assert ticker not in near_close_dca.EXCLUDED_TICKERS
        assert ticker in active


def test_every_new_name_has_a_sizing_entry():
    entries = _load("per_ticker_sizing.json")["tickers"]
    for ticker in NEW:
        assert entries[ticker]["tiers"]


def test_ticker_history_records_the_universe_with_dates():
    with (ROOT / "config" / "ticker_history.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows and set(rows[0]) == {"date", "ticker"}
    latest = max(row["date"] for row in rows)
    assert {row["ticker"] for row in rows if row["date"] == latest} == set(_load("tickers.json")["active"])
