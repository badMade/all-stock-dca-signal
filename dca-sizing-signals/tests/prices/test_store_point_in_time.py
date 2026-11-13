"""PriceStore: append-only raw storage, point-in-time reads, adjusted values only in views.

Expected behaviour (PLAN-price-source.md §3):
- every data table carries source, as_of, ingested_at, run_id;
- ingesting the same data twice adds nothing; a vendor correction adds a new row with a
  later as_of and never overwrites the first;
- a batch with one invalid row writes nothing (one batch = one transaction);
- raw values are never modified; adjusted values come from views;
- an as-of query never applies a corporate action dated after it (no lookahead), and a
  known_at query never sees a row we learned about later.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from market_data_ingest.prices.errors import ValidationError
from market_data_ingest.prices.models import AdjustedBar, CorporateAction, DailyBar

from .conftest import utc

TODAY = date(2026, 10, 18)
FIRST_SEEN = utc(2026, 10, 17, 1)


def bar(symbol, session, close, *, source="massive", volume=1_000_000.0, o=None, h=None, low=None):
    o = close if o is None else o
    h = max(o, close) * 1.01 if h is None else h
    low = min(o, close) * 0.99 if low is None else low
    return DailyBar(
        source=source,
        symbol=symbol,
        session=session,
        open=o,
        high=h,
        low=low,
        close=close,
        volume=volume,
        vwap=None,
        trades=None,
    )


def split(symbol, ex_date, split_from, split_to, *, action_type="forward_split", source="massive"):
    return CorporateAction(
        source=source, action_type=action_type, symbol=symbol, ex_date=ex_date, split_from=split_from, split_to=split_to
    )


def cash_dividend(symbol, ex_date, amount, *, source="massive"):
    return CorporateAction(
        source=source, action_type="cash_dividend", symbol=symbol, ex_date=ex_date, cash_amount=amount, currency="USD"
    )


def ingest(store, staging_dir, bars, *, run_id="run-1", as_of=FIRST_SEEN):
    return store.ingest_daily_bars(bars, run_id=run_id, as_of=as_of, staging_dir=staging_dir)


def ingest_actions(store, staging_dir, actions, *, run_id="run-ca", as_of=FIRST_SEEN):
    return store.ingest_corporate_actions(actions, run_id=run_id, as_of=as_of, staging_dir=staging_dir)


def adjusted(store, symbol, session, *, as_of_date, known_at=None) -> AdjustedBar:
    rows = store.adjusted_bars(symbol, as_of_date=as_of_date, known_at=known_at)
    match = [r for r in rows if r.session == session]
    assert len(match) == 1, f"exactly one {symbol} row for {session}, got {match}"
    return match[0]


NVDA_BARS = [
    bar("NVDA", date(2024, 5, 31), 1096.33),
    bar("NVDA", date(2024, 6, 7), 1208.88),
    bar("NVDA", date(2024, 6, 10), 121.79),
]


# --- schema contract --------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    [
        "daily_bars_raw",
        "corporate_actions",
        "ingestion_runs",
        "intraday_quotes",
        "yahoo_legacy_bars",
        "reconciliation_results",
    ],
)
def test_every_table_carries_provenance_columns(store, table):
    cols = set(store.table_columns(table))
    assert {"source", "as_of", "ingested_at", "run_id"} <= cols


def test_intraday_quotes_has_the_briefed_columns(store):
    assert {"symbol", "ts", "price", "feed", "as_of", "delayed", "slot"} <= set(store.table_columns("intraday_quotes"))


# --- append-only, idempotent, atomic ---------------------------------------------------------


def test_ingest_stages_parquet_then_inserts(store, staging_dir):
    assert ingest(store, staging_dir, NVDA_BARS, run_id="run-a") == 3
    assert store.row_count("daily_bars_raw") == 3
    assert list((staging_dir / "run-a").glob("*.parquet")), "the batch was staged as Parquet first"


def test_ingesting_identical_data_twice_is_a_no_op(store, staging_dir):
    ingest(store, staging_dir, NVDA_BARS, run_id="run-a")
    assert ingest(store, staging_dir, NVDA_BARS, run_id="run-b", as_of=utc(2026, 10, 18, 1)) == 0
    assert store.row_count("daily_bars_raw") == 3


def test_vendor_correction_is_a_new_row_with_a_later_as_of(store, staging_dir):
    first, second = utc(2026, 10, 17, 1), utc(2026, 10, 18, 1)
    ingest(store, staging_dir, [bar("AAPL", date(2026, 10, 16), 250.00)], run_id="run-a", as_of=first)
    ingest(store, staging_dir, [bar("AAPL", date(2026, 10, 16), 250.40)], run_id="run-b", as_of=second)

    raw = sorted(store.raw_bars("AAPL"), key=lambda r: r["as_of"])
    assert [r["close"] for r in raw] == pytest.approx([250.00, 250.40]), "the first row is kept, not overwritten"
    assert [r["run_id"] for r in raw] == ["run-a", "run-b"]

    latest = adjusted(store, "AAPL", date(2026, 10, 16), as_of_date=TODAY)
    assert latest.close_raw == pytest.approx(250.40)
    before_correction = adjusted(
        store, "AAPL", date(2026, 10, 16), as_of_date=TODAY, known_at=datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    )
    assert before_correction.close_raw == pytest.approx(250.00)


@pytest.mark.parametrize(
    "bad",
    [
        bar("BAD", date(2026, 10, 16), -1.0, o=1.0, h=1.0, low=-1.0),  # negative price
        bar("BAD", date(2026, 10, 16), 0.0, o=1.0, h=1.0, low=0.0),  # zero price
        bar("BAD", date(2026, 10, 16), 10.0, o=10.0, h=9.0, low=9.5),  # high < low
        bar("BAD", date(2026, 10, 16), 10.0, o=12.0, h=11.0, low=9.0),  # open above high
        bar("BAD", date(2026, 10, 16), 10.0, o=10.0, h=11.0, low=10.5),  # close below low
        bar("BAD", date(2026, 10, 16), 10.0, volume=-5.0),  # negative volume
    ],
    ids=["negative", "zero", "high<low", "open>high", "close<low", "negative-volume"],
)
def test_one_invalid_row_rejects_the_whole_batch(store, staging_dir, bad):
    good = bar("GOOD", date(2026, 10, 16), 10.0)

    with pytest.raises(ValidationError) as excinfo:
        ingest(store, staging_dir, [good, bad])
    assert "BAD" in str(excinfo.value), "the error names the offending row"
    assert store.row_count("daily_bars_raw") == 0, "nothing from a failed batch is written"


def test_duplicate_symbol_session_inside_one_batch_is_rejected(store, staging_dir):
    with pytest.raises(ValidationError):
        ingest(store, staging_dir, [bar("DUP", date(2026, 10, 16), 10.0), bar("DUP", date(2026, 10, 16), 10.5)])
    assert store.row_count("daily_bars_raw") == 0


def test_corporate_actions_are_idempotent_too(store, staging_dir):
    actions = [split("NVDA", date(2024, 6, 10), 1, 10)]
    assert ingest_actions(store, staging_dir, actions) == 1
    assert ingest_actions(store, staging_dir, actions, run_id="run-ca2") == 0
    assert store.row_count("corporate_actions") == 1


# --- point in time ----------------------------------------------------------------------


def test_nvda_split_is_excluded_as_of_2024_06_01_and_included_as_of_today(store, staging_dir):
    ingest(store, staging_dir, NVDA_BARS)
    ingest_actions(store, staging_dir, [split("NVDA", date(2024, 6, 10), 1, 10)])

    then = adjusted(store, "NVDA", date(2024, 5, 31), as_of_date=date(2024, 6, 1))
    assert then.close_split_adjusted == pytest.approx(1096.33), "the 2024-06-10 split had not happened yet"

    now = adjusted(store, "NVDA", date(2024, 5, 31), as_of_date=TODAY)
    assert now.close_split_adjusted == pytest.approx(109.633)
    assert now.volume_split_adjusted == pytest.approx(10_000_000.0)


def test_split_applies_from_its_ex_date_not_a_day_early(store, staging_dir):
    ingest(store, staging_dir, NVDA_BARS)
    ingest_actions(store, staging_dir, [split("NVDA", date(2024, 6, 10), 1, 10)])

    day_before = adjusted(store, "NVDA", date(2024, 6, 7), as_of_date=date(2024, 6, 9))
    on_ex_date = adjusted(store, "NVDA", date(2024, 6, 7), as_of_date=date(2024, 6, 10))
    assert day_before.close_split_adjusted == pytest.approx(1208.88)
    assert on_ex_date.close_split_adjusted == pytest.approx(120.888)


def test_bars_on_or_after_the_ex_date_are_not_adjusted_by_it(store, staging_dir):
    ingest(store, staging_dir, NVDA_BARS)
    ingest_actions(store, staging_dir, [split("NVDA", date(2024, 6, 10), 1, 10)])

    assert adjusted(store, "NVDA", date(2024, 6, 10), as_of_date=TODAY).close_split_adjusted == pytest.approx(121.79)


def test_as_of_query_returns_no_sessions_after_its_date(store, staging_dir):
    ingest(store, staging_dir, NVDA_BARS)

    sessions = [r.session for r in store.adjusted_bars("NVDA", as_of_date=date(2024, 6, 7))]
    assert sessions == [date(2024, 5, 31), date(2024, 6, 7)]


def test_known_at_hides_a_split_we_learned_about_later(store, staging_dir):
    ingest(store, staging_dir, NVDA_BARS, as_of=utc(2024, 6, 11, 1))
    ingest_actions(store, staging_dir, [split("NVDA", date(2024, 6, 10), 1, 10)], as_of=utc(2024, 6, 12, 1))

    unaware = adjusted(store, "NVDA", date(2024, 5, 31), as_of_date=TODAY, known_at=utc(2024, 6, 11, 12))
    aware = adjusted(store, "NVDA", date(2024, 5, 31), as_of_date=TODAY, known_at=utc(2024, 6, 12, 12))
    assert unaware.close_split_adjusted == pytest.approx(1096.33)
    assert aware.close_split_adjusted == pytest.approx(109.633)


def test_raw_rows_are_never_rewritten_by_adjustment(store, staging_dir):
    ingest(store, staging_dir, NVDA_BARS)
    ingest_actions(store, staging_dir, [split("NVDA", date(2024, 6, 10), 1, 10)])
    store.adjusted_bars("NVDA", as_of_date=TODAY)

    closes = sorted(r["close"] for r in store.raw_bars("NVDA"))
    assert closes == pytest.approx(sorted([1096.33, 1208.88, 121.79]))


def test_reverse_split_multiplies_earlier_prices(store, staging_dir):
    ingest(store, staging_dir, [bar("RVRS", date(2024, 6, 4), 0.50), bar("RVRS", date(2024, 6, 5), 10.00)])
    ingest_actions(store, staging_dir, [split("RVRS", date(2024, 6, 5), 20, 1, action_type="reverse_split")])

    assert adjusted(store, "RVRS", date(2024, 6, 4), as_of_date=TODAY).close_split_adjusted == pytest.approx(10.00)


def test_stock_dividend_adjusts_like_a_split(store, staging_dir):
    ingest(store, staging_dir, [bar("STKD", date(2024, 5, 31), 105.0)])
    ingest_actions(store, staging_dir, [split("STKD", date(2024, 6, 3), 100, 105, action_type="stock_dividend")])

    assert adjusted(store, "STKD", date(2024, 5, 31), as_of_date=TODAY).close_split_adjusted == pytest.approx(100.0)


def test_cash_dividend_adjusts_total_return_close_only(store, staging_dir):
    ingest(
        store,
        staging_dir,
        [bar("KO", date(2024, 6, 12), 60.00), bar("KO", date(2024, 6, 13), 60.00), bar("KO", date(2024, 6, 14), 59.60)],
    )
    ingest_actions(store, staging_dir, [cash_dividend("KO", date(2024, 6, 14), 0.485)])

    row = adjusted(store, "KO", date(2024, 6, 13), as_of_date=TODAY)
    assert row.close_split_adjusted == pytest.approx(60.00), "a cash dividend is not a split"
    # Multiplicative factor (1 - dividend / close on the session before the ex-date).
    assert row.close_adjusted == pytest.approx(60.00 * (1 - 0.485 / 60.00))
    assert adjusted(store, "KO", date(2024, 6, 14), as_of_date=TODAY).close_adjusted == pytest.approx(59.60)
    assert adjusted(store, "KO", date(2024, 6, 13), as_of_date=date(2024, 6, 13)).close_adjusted == pytest.approx(60.00)


def test_the_same_split_from_two_vendors_applies_once(store, staging_dir):
    ingest(store, staging_dir, NVDA_BARS)
    ingest_actions(
        store,
        staging_dir,
        [
            split("NVDA", date(2024, 6, 10), 1, 10, source="massive"),
            split("NVDA", date(2024, 6, 10), 1, 10, source="alpaca"),
        ],
    )

    assert adjusted(store, "NVDA", date(2024, 5, 31), as_of_date=TODAY).close_split_adjusted == pytest.approx(109.633)


def test_massive_is_preferred_over_alpaca_for_the_same_session(store, staging_dir):
    ingest(store, staging_dir, [bar("AAPL", date(2024, 6, 10), 193.12, source="alpaca")], run_id="alp")
    ingest(
        store,
        staging_dir,
        [bar("AAPL", date(2024, 6, 10), 193.10, source="massive")],
        run_id="mas",
        as_of=utc(2026, 10, 17, 0),
    )  # earlier as_of, still preferred: source priority comes first
    ingest(store, staging_dir, [bar("MSFT", date(2024, 6, 10), 427.87, source="alpaca")], run_id="alp2")

    aapl = adjusted(store, "AAPL", date(2024, 6, 10), as_of_date=TODAY)
    msft = adjusted(store, "MSFT", date(2024, 6, 10), as_of_date=TODAY)
    assert (aapl.source, aapl.close_raw) == ("massive", pytest.approx(193.10))
    assert (msft.source, msft.close_raw) == ("alpaca", pytest.approx(427.87)), "fallback-only rows are labelled"


# --- helpers the provider and job rely on -----------------------------------------------------


def test_trailing_session_counts_are_prior_sessions_of_one_source_most_recent_first(store, staging_dir):
    for i, d in enumerate([date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5), date(2024, 6, 6)]):
        ingest(store, staging_dir, [bar(f"S{j}", d, 10.0) for j in range(i + 1)], run_id=f"r{i}")
    ingest(store, staging_dir, [bar(f"A{j}", date(2024, 6, 5), 10.0, source="alpaca") for j in range(9)], run_id="alp")

    assert store.trailing_session_counts("massive", before=date(2024, 6, 6), n=20) == [3, 2, 1]
    assert store.trailing_session_counts("massive", before=date(2024, 6, 6), n=2) == [3, 2]


def test_trailing_counts_ignore_superseded_correction_rows(store, staging_dir):
    ingest(store, staging_dir, [bar("X", date(2024, 6, 3), 10.0), bar("Y", date(2024, 6, 3), 10.0)], run_id="a")
    ingest(store, staging_dir, [bar("X", date(2024, 6, 3), 10.5)], run_id="b", as_of=utc(2026, 10, 18, 1))

    assert store.trailing_session_counts("massive", before=date(2024, 6, 4), n=20) == [2]


def test_last_session_symbols_come_from_the_latest_session_of_that_source(store, staging_dir):
    ingest(store, staging_dir, [bar("OLD", date(2024, 6, 3), 1.0)], run_id="a")
    ingest(store, staging_dir, [bar("NVDA", date(2024, 6, 4), 1.0), bar("AAPL", date(2024, 6, 4), 1.0)], run_id="b")

    assert sorted(store.last_session_symbols("massive")) == ["AAPL", "NVDA"]


# --- PR #2265 review: versioned dedupe for daily bars ------------------------------------------


def test_a_vendor_revert_to_an_earlier_close_is_kept(store, staging_dir):
    """A -> B -> A: the revert is a new version, not a duplicate of the first A."""
    session = date(2026, 10, 16)
    ingest(store, staging_dir, [bar("AAPL", session, 250.00)], run_id="run-a", as_of=utc(2026, 10, 17, 1))
    ingest(store, staging_dir, [bar("AAPL", session, 250.40)], run_id="run-b", as_of=utc(2026, 10, 18, 1))
    assert ingest(store, staging_dir, [bar("AAPL", session, 250.00)], run_id="run-c", as_of=utc(2026, 10, 18, 2)) == 1
    assert adjusted(store, "AAPL", session, as_of_date=TODAY).close_raw == pytest.approx(250.00)


def test_an_older_price_file_key_refuses_a_revert(tmp_path, staging_dir):
    """A file whose daily_bars_raw key lacks as_of/run_id must fail loudly, not drop the revert."""
    import duckdb

    from market_data_ingest.prices.store import PriceStore

    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE daily_bars_raw (source VARCHAR NOT NULL, symbol VARCHAR NOT NULL, session DATE NOT NULL, "
        "open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL, "
        "volume DOUBLE NOT NULL, vwap DOUBLE, trades BIGINT, content_hash VARCHAR NOT NULL, "
        "as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL, "
        "PRIMARY KEY (source, symbol, session, content_hash))"
    )
    con.close()
    session = date(2026, 10, 16)
    with PriceStore(path, clock=lambda: utc(2026, 10, 18, 3)) as old:
        ingest(old, staging_dir, [bar("AAPL", session, 250.00)], run_id="run-a", as_of=utc(2026, 10, 17, 1))
        ingest(old, staging_dir, [bar("AAPL", session, 250.40)], run_id="run-b", as_of=utc(2026, 10, 18, 1))
        with pytest.raises(ValidationError, match="collided with the primary key"):
            ingest(old, staging_dir, [bar("AAPL", session, 250.00)], run_id="run-c", as_of=utc(2026, 10, 18, 2))
        assert old.row_count("daily_bars_raw") == 2
