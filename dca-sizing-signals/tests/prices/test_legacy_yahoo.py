"""Import of the existing Yahoo archive (ticker_data/*.csv) as source='yahoo_legacy'.

Expected behaviour (PLAN-price-source.md §3, operator answer Q5):
- rows go to yahoo_legacy_bars, labelled split_adjusted_at_download, never to
  daily_bars_raw (Yahoo Close is not raw, so the adjusted view would adjust it twice);
- re-importing an unchanged file adds nothing;
- a malformed file is rejected whole, with the file and line named; a directory import
  reports every failed file instead of skipping it quietly.
"""

from __future__ import annotations

from datetime import date

import pytest

from market_data_ingest.prices.errors import ValidationError
from market_data_ingest.prices.legacy_yahoo import import_csv, import_dir

from .conftest import utc

NOW = utc(2026, 10, 18, 12)
HEADER = "Date,Open,High,Low,Close,Adj Close,Volume,Dividends,Stock Splits\n"
NVDA_ROWS = (
    "2024-06-07,119.77,121.69,118.02,120.888,120.86,412386000.0,0.0,0.0\n"
    "2024-06-10,120.37,123.10,117.01,121.79,121.76,314162700.0,0.0,10.0\n"
)


def test_import_labels_rows_yahoo_legacy_and_keeps_them_out_of_raw(store, tmp_path):
    path = tmp_path / "NVDA.csv"
    path.write_text(HEADER + NVDA_ROWS)

    assert import_csv(store, path, run_id="y1", now=NOW) == 2

    rows = sorted(store.yahoo_legacy_bars("NVDA"), key=lambda r: r["session"])
    assert [r["session"] for r in rows] == [date(2024, 6, 7), date(2024, 6, 10)]
    assert {r["source"] for r in rows} == {"yahoo_legacy"}
    assert {r["adjustment_basis"] for r in rows} == {"split_adjusted_at_download"}
    assert rows[1]["close"] == pytest.approx(121.79)
    assert rows[1]["adj_close"] == pytest.approx(121.76)
    assert rows[1]["stock_splits"] == pytest.approx(10.0)
    assert all(r["file_sha256"] and r["run_id"] == "y1" for r in rows)
    assert store.row_count("daily_bars_raw") == 0
    assert store.adjusted_bars("NVDA", as_of_date=date(2026, 10, 18)) == [], "legacy rows never enter the raw views"


def test_reimporting_an_unchanged_file_adds_nothing(store, tmp_path):
    path = tmp_path / "NVDA.csv"
    path.write_text(HEADER + NVDA_ROWS)
    import_csv(store, path, run_id="y1", now=NOW)

    assert import_csv(store, path, run_id="y2", now=NOW) == 0
    assert store.row_count("yahoo_legacy_bars") == 2


@pytest.mark.parametrize(
    ("bad_line", "why"),
    [
        ("04-27,1,1,1,1,1,100,0,0\n", "yearless date (the CBU.csv case that parses to year 0001)"),
        ("2024-06-11,1,1,1,,1,100,0,0\n", "missing close"),
        ("2024-06-11,1,1,1,nan,1,100,0,0\n", "NaN close"),
        ("2024-06-10,1,1,1,1,1,100,0,0\n", "duplicate session"),
    ],
)
def test_a_malformed_file_is_rejected_whole_with_file_and_line(store, tmp_path, bad_line, why):
    path = tmp_path / "CBU.csv"
    path.write_text(HEADER + NVDA_ROWS + bad_line)

    with pytest.raises(ValidationError) as excinfo:
        import_csv(store, path, run_id="y1", now=NOW)
    message = str(excinfo.value)
    assert "CBU.csv" in message and "line 4" in message.lower(), f"names the file and line ({why})"
    assert store.row_count("yahoo_legacy_bars") == 0


def test_wrong_header_is_rejected(store, tmp_path):
    path = tmp_path / "NVDA.csv"
    path.write_text("Date,Close\n2024-06-10,121.79\n")

    with pytest.raises(ValidationError):
        import_csv(store, path, run_id="y1", now=NOW)


def test_directory_import_reports_failures_instead_of_skipping_them(store, tmp_path):
    (tmp_path / "NVDA.csv").write_text(HEADER + NVDA_ROWS)
    (tmp_path / "CBU.csv").write_text(HEADER + "04-27,1,1,1,1,1,100,0,0\n")

    report = import_dir(store, tmp_path, run_id="y1", now=NOW)

    assert report.imported == {"NVDA": 2}
    assert set(report.failed) == {"CBU"}
    assert report.ok is False
    assert store.yahoo_legacy_bars("CBU") == []
