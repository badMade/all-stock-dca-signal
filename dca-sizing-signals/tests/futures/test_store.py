"""FuturesStore: append-only, point-in-time storage for ES/NQ/YM contracts and 1-minute bars.

docs/adr-overnight-futures-tracker.md, D5 (storage) and phase 3. Expected behaviour is the
same contract as market_data_ingest/prices/store.py, applied to futures:

- every data table carries source, as_of, ingested_at, run_id and a content_hash;
- ingesting the same data twice adds nothing; a vendor correction adds a new row with a
  later as_of and never overwrites the first; there is no UPDATE or DELETE;
- a batch with one invalid row writes nothing (one batch = one transaction);
- a known_at read never sees a row we learned about later;
- timestamps go in aware and come back aware UTC; naive ones are refused.

Futures-specific rules:

- a bar must belong to a contract the store already knows (no orphan bars);
- close_at(contract, t) is the close of the 1-minute bar that ENDS at t (starts at t - 1m).
  It never reads a bar starting at or after t (lookahead) and never fills forward from an
  earlier minute: a missing minute is None, so the tracker refuses the night (D9).

Composition with overnight_futures (turning bars into OvernightInputs) is not this module's
job: market_data_ingest may not import overnight_futures (test_architecture_boundaries.py).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from market_data_ingest.futures.models import FuturesBar, FuturesContract
from market_data_ingest.futures.store import FuturesStore
from market_data_ingest.prices.errors import ValidationError

UTC = timezone.utc
FIRST_SEEN = datetime(2026, 11, 12, 1, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 13, 1, 0, tzinfo=UTC)
FIXED_NOW = datetime(2026, 11, 13, 2, 0, tzinfo=UTC)

ESZ26 = FuturesContract(
    contract_id="ESZ26",
    root="ES",
    underlying_index="SPX",
    expiry_utc=datetime(2026, 12, 18, 14, 30, tzinfo=UTC),
    multiplier=50.0,
    source="databento",
)


def minute(h: int, m: int, day: int = 11) -> datetime:
    return datetime(2026, 11, day, h, m, tzinfo=UTC)


def fbar(start: datetime, close: float, *, contract_id: str = "ESZ26", **overrides) -> FuturesBar:
    values = {
        "contract_id": contract_id,
        "bar_start_utc": start,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": 100,
        "source": "databento",
    }
    values.update(overrides)
    return FuturesBar(**values)


# Wed 2026-11-11 (EST): 09:28 ET bar is 14:28 UTC and closes at 09:29 ET, the pre-open anchor.
PRE_OPEN_BARS = [fbar(minute(14, 27), 6028.0), fbar(minute(14, 28), 6030.0)]


@pytest.fixture
def staging_dir(tmp_path: Path) -> Path:
    return tmp_path / "staging"


@pytest.fixture
def store(tmp_path: Path):
    with FuturesStore(tmp_path / "futures.duckdb", clock=lambda: FIXED_NOW) as s:
        s.ingest_contracts([ESZ26], run_id="contracts-1", as_of=FIRST_SEEN, staging_dir=tmp_path / "staging")
        yield s


def ingest(store: FuturesStore, staging_dir: Path, bars, *, run_id: str = "run-1", as_of: datetime = FIRST_SEEN):
    return store.ingest_bars(bars, run_id=run_id, as_of=as_of, staging_dir=staging_dir)


# --- schema contract --------------------------------------------------------------------


@pytest.mark.parametrize("table", ["futures_contract", "futures_bar_1m"])
def test_every_table_carries_provenance_columns(store, table):
    assert {"source", "content_hash", "as_of", "ingested_at", "run_id"} <= set(store.table_columns(table))


def test_module_has_no_update_or_delete():
    source = Path(__file__).resolve().parents[2] / "market_data_ingest" / "futures" / "store.py"
    text = source.read_text().upper()
    assert "UPDATE " not in text.replace("ON CONFLICT DO NOTHING", "")
    assert "DELETE " not in text


# --- append-only, idempotent, atomic ---------------------------------------------------------


def test_ingest_stages_parquet_then_inserts(store, staging_dir):
    assert ingest(store, staging_dir, PRE_OPEN_BARS, run_id="run-a") == 2
    assert store.row_count("futures_bar_1m") == 2
    assert list((staging_dir / "run-a").glob("*.parquet")), "the batch was staged as Parquet first"


def test_ingesting_identical_data_twice_is_a_no_op(store, staging_dir):
    ingest(store, staging_dir, PRE_OPEN_BARS, run_id="run-a")
    assert ingest(store, staging_dir, PRE_OPEN_BARS, run_id="run-b", as_of=LATER) == 0
    assert store.row_count("futures_bar_1m") == 2


def test_contracts_are_idempotent_too(store, tmp_path):
    assert store.ingest_contracts([ESZ26], run_id="contracts-2", as_of=LATER, staging_dir=tmp_path / "s") == 0
    assert store.row_count("futures_contract") == 1


def test_vendor_correction_is_a_new_row_and_known_at_sees_the_old_one(store, staging_dir):
    ingest(store, staging_dir, [fbar(minute(14, 28), 6030.0)], run_id="run-a", as_of=FIRST_SEEN)
    ingest(store, staging_dir, [fbar(minute(14, 28), 6030.25)], run_id="run-b", as_of=LATER)

    assert store.row_count("futures_bar_1m") == 2, "the first row is kept, not overwritten"
    assert store.close_at("ESZ26", minute(14, 29)) == pytest.approx(6030.25)
    before_correction = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    assert store.close_at("ESZ26", minute(14, 29), known_at=before_correction) == pytest.approx(6030.0)


def test_known_at_hides_rows_learned_later(store, staging_dir):
    ingest(store, staging_dir, PRE_OPEN_BARS, as_of=LATER)
    assert store.close_at("ESZ26", minute(14, 29), known_at=FIRST_SEEN) is None


@pytest.mark.parametrize(
    "bad",
    [
        fbar(minute(14, 28), -1.0, open=1.0, high=1.0, low=-1.0),  # negative price
        fbar(minute(14, 28), 0.0, open=1.0, high=1.0, low=0.0),  # zero price
        fbar(minute(14, 28), 10.0, open=10.0, high=9.0, low=9.5),  # high below open/close
        fbar(minute(14, 28), 10.0, open=10.0, high=11.0, low=10.5),  # low above open/close
        fbar(minute(14, 28), math.nan),  # not finite
        fbar(minute(14, 28), 10.0, volume=-1),  # negative volume
        fbar(datetime(2026, 11, 11, 14, 28), 10.0),  # naive timestamp
        fbar(datetime(2026, 11, 11, 14, 28, 30, tzinfo=UTC), 10.0),  # not on a minute boundary
        fbar(minute(14, 28), 10.0, contract_id="ESH27"),  # contract the store does not know
        fbar(minute(14, 28), 10.0, source=""),  # no source
    ],
    ids=[
        "negative", "zero", "high-low", "low-high", "nan", "negative-volume",
        "naive", "off-minute", "unknown-contract", "no-source",
    ],
)
def test_one_bad_row_writes_nothing(store, staging_dir, bad):
    with pytest.raises(ValidationError):
        ingest(store, staging_dir, [*PRE_OPEN_BARS, bad])
    assert store.row_count("futures_bar_1m") == 0


def test_conflicting_duplicates_in_one_batch_write_nothing(store, staging_dir):
    """Two different closes for the same minute in ONE batch is ambiguous, not a correction."""
    with pytest.raises(ValidationError):
        ingest(store, staging_dir, [fbar(minute(14, 28), 6030.0), fbar(minute(14, 28), 6031.0)])
    assert store.row_count("futures_bar_1m") == 0


@pytest.mark.parametrize(
    "bad",
    [
        FuturesContract("ESZ26", "XX", "SPX", datetime(2026, 12, 18, 14, 30, tzinfo=UTC), 50.0, "databento"),
        FuturesContract("ESZ26", "ES", "SPX", datetime(2026, 12, 18, 14, 30), 50.0, "databento"),
        FuturesContract("ESZ26", "ES", "SPX", datetime(2026, 12, 18, 14, 30, tzinfo=UTC), 0.0, "databento"),
        FuturesContract("", "ES", "SPX", datetime(2026, 12, 18, 14, 30, tzinfo=UTC), 50.0, "databento"),
    ],
    ids=["unknown-root", "naive-expiry", "zero-multiplier", "empty-id"],
)
def test_bad_contract_writes_nothing(tmp_path, bad):
    with FuturesStore(tmp_path / "f.duckdb", clock=lambda: FIXED_NOW) as s:
        with pytest.raises(ValidationError):
            s.ingest_contracts([bad], run_id="c", as_of=FIRST_SEEN, staging_dir=tmp_path / "s")
        assert s.row_count("futures_contract") == 0


# --- reads ---------------------------------------------------------------------------------


def test_close_at_reads_the_bar_ending_at_t(store, staging_dir):
    ingest(store, staging_dir, PRE_OPEN_BARS)
    assert store.close_at("ESZ26", minute(14, 29)) == pytest.approx(6030.0)  # 14:28 bar, ends 14:29
    assert store.close_at("ESZ26", minute(14, 28)) == pytest.approx(6028.0)  # 14:27 bar, ends 14:28


def test_close_at_never_reads_a_bar_starting_at_t(store, staging_dir):
    """Lookahead guard: the bar that STARTS at 09:29 ET closes at 09:30, inside the open."""
    ingest(store, staging_dir, [fbar(minute(14, 29), 6040.0)])
    assert store.close_at("ESZ26", minute(14, 29)) is None


def test_close_at_does_not_fill_forward(store, staging_dir):
    """Only the 14:27 bar exists; asking for 14:29 must not quietly return 14:27's close."""
    ingest(store, staging_dir, [fbar(minute(14, 27), 6028.0)])
    assert store.close_at("ESZ26", minute(14, 29)) is None


def test_close_at_refuses_naive_time(store, staging_dir):
    ingest(store, staging_dir, PRE_OPEN_BARS)
    with pytest.raises(ValueError):
        store.close_at("ESZ26", datetime(2026, 11, 11, 14, 29))


def test_close_at_unknown_contract_is_none(store):
    assert store.close_at("NQZ26", minute(14, 29)) is None


def test_bars_round_trip_as_aware_utc(store, staging_dir):
    ingest(store, staging_dir, PRE_OPEN_BARS)
    bars = store.bars("ESZ26", start_utc=minute(14, 0), end_utc=minute(15, 0))
    assert [b.bar_start_utc for b in bars] == [minute(14, 27), minute(14, 28)]
    assert all(b.bar_start_utc.tzinfo is not None and b.bar_start_utc.utcoffset() == timedelta(0) for b in bars)
    assert [b.close for b in bars] == pytest.approx([6028.0, 6030.0])


def test_bars_window_is_half_open(store, staging_dir):
    """[start, end): a bar starting exactly at end is excluded, so windows never overlap."""
    ingest(store, staging_dir, PRE_OPEN_BARS)
    assert [b.bar_start_utc for b in store.bars("ESZ26", start_utc=minute(14, 27), end_utc=minute(14, 28))] == [
        minute(14, 27)
    ]


def test_contract_round_trips(store):
    got = store.contract("ESZ26")
    assert got == ESZ26
    assert store.contract("NQZ26") is None


def test_store_records_ingested_at_from_its_clock(store, staging_dir):
    ingest(store, staging_dir, PRE_OPEN_BARS)
    stamps = {r["ingested_at"] for r in store.raw_rows("futures_bar_1m")}
    assert stamps == {FIXED_NOW}


# --- PR #2264 review regressions ------------------------------------------------------------


def test_vendor_revert_to_an_earlier_value_is_kept(store, staging_dir):
    """A -> B -> A: the revert is a new version, not a duplicate of the first A."""
    ingest(store, staging_dir, [fbar(minute(14, 28), 6030.0)], run_id="run-a", as_of=FIRST_SEEN)
    ingest(store, staging_dir, [fbar(minute(14, 28), 6031.0)], run_id="run-b", as_of=LATER)
    reverted_at = datetime(2026, 11, 13, 1, 30, tzinfo=UTC)
    assert ingest(store, staging_dir, [fbar(minute(14, 28), 6030.0)], run_id="run-c", as_of=reverted_at) == 1
    assert store.close_at("ESZ26", minute(14, 29)) == pytest.approx(6030.0)
    assert store.close_at("ESZ26", minute(14, 29), known_at=LATER) == pytest.approx(6031.0)


def test_reingest_with_an_older_as_of_than_the_correction_adds_nothing(store, staging_dir):
    """Re-sending A with an as_of whose newest known version is already A is still a no-op."""
    ingest(store, staging_dir, [fbar(minute(14, 28), 6030.0)], run_id="run-a", as_of=FIRST_SEEN)
    ingest(store, staging_dir, [fbar(minute(14, 28), 6031.0)], run_id="run-b", as_of=LATER)
    between = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)
    assert ingest(store, staging_dir, [fbar(minute(14, 28), 6030.0)], run_id="run-c", as_of=between) == 0


def test_integer_and_float_prices_are_the_same_content(store, staging_dir):
    ingest(store, staging_dir, [fbar(minute(14, 28), 6031.0)], run_id="run-a")
    as_ints = fbar(minute(14, 28), 6031, open=6031, high=6032, low=6030)
    assert ingest(store, staging_dir, [as_ints], run_id="run-b", as_of=LATER) == 0
    assert store.row_count("futures_bar_1m") == 1


def test_integer_multiplier_is_the_same_contract(store, tmp_path):
    as_int = FuturesContract("ESZ26", "ES", "SPX", datetime(2026, 12, 18, 14, 30, tzinfo=UTC), 50, "databento")
    assert store.ingest_contracts([as_int], run_id="c2", as_of=LATER, staging_dir=tmp_path / "s") == 0


def other_source_contract(store, tmp_path):
    other = FuturesContract("ESZ26", "ES", "SPX", datetime(2026, 12, 18, 14, 30, tzinfo=UTC), 50.0, "other")
    store.ingest_contracts([other], run_id="c-other", as_of=FIRST_SEEN, staging_dir=tmp_path / "s")
    return other


def test_a_second_source_never_replaces_the_first(store, staging_dir, tmp_path):
    other_source_contract(store, tmp_path)
    ingest(store, staging_dir, [fbar(minute(14, 28), 6031.0)], run_id="run-a")
    ingest(store, staging_dir, [fbar(minute(14, 28), 6100.0, source="other")], run_id="run-b", as_of=LATER)
    assert store.close_at("ESZ26", minute(14, 29), source="databento") == pytest.approx(6031.0)
    assert store.close_at("ESZ26", minute(14, 29), source="other") == pytest.approx(6100.0)
    with pytest.raises(ValueError, match="more than one source"):
        store.close_at("ESZ26", minute(14, 29))
    with pytest.raises(ValueError, match="more than one source"):
        store.bars("ESZ26", start_utc=minute(14, 0), end_utc=minute(15, 0))
    assert [b.close for b in store.bars("ESZ26", start_utc=minute(14, 0), end_utc=minute(15, 0), source="other")] == [
        pytest.approx(6100.0)
    ]


def test_two_sources_in_one_batch_are_not_a_conflict(store, staging_dir, tmp_path):
    other_source_contract(store, tmp_path)
    batch = [fbar(minute(14, 28), 6031.0), fbar(minute(14, 28), 6100.0, source="other")]
    assert ingest(store, staging_dir, batch) == 2


def test_contract_from_two_sources_needs_a_source(store, tmp_path):
    other = FuturesContract("ESZ26", "ES", "SPX", datetime(2026, 12, 18, 14, 30, tzinfo=UTC), 50.0, "other")
    store.ingest_contracts([other], run_id="c2", as_of=LATER, staging_dir=tmp_path / "s")
    with pytest.raises(ValueError, match="more than one source"):
        store.contract("ESZ26")
    assert store.contract("ESZ26", source="databento") == ESZ26


def test_conflicting_contract_versions_in_one_batch_write_nothing(tmp_path):
    a = FuturesContract("ESZ26", "ES", "SPX", datetime(2026, 12, 18, 14, 30, tzinfo=UTC), 5.0, "databento")
    b = FuturesContract("ESZ26", "ES", "SPX", datetime(2026, 12, 18, 14, 30, tzinfo=UTC), 500.0, "databento")
    with FuturesStore(tmp_path / "f.duckdb", clock=lambda: FIXED_NOW) as s:
        with pytest.raises(ValidationError):
            s.ingest_contracts([a, b], run_id="c", as_of=FIRST_SEEN, staging_dir=tmp_path / "s")
        assert s.row_count("futures_contract") == 0


# --- PR #2265 review regressions ------------------------------------------------------------


class TickingClock:
    """Each call is one second later, so ingested_at orders runs the way a real clock does."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def test_a_revert_with_the_same_as_of_is_kept(tmp_path, staging_dir):
    """A@T, B@T, A@T: with as_of but not run_id in the key, the third insert collided with the first."""
    with FuturesStore(tmp_path / "f.duckdb", clock=TickingClock(FIXED_NOW)) as s:
        s.ingest_contracts([ESZ26], run_id="c", as_of=FIRST_SEEN, staging_dir=staging_dir)
        for run_id, close in (("run-a", 6030.0), ("run-b", 6031.0), ("run-c", 6030.0)):
            assert ingest(s, staging_dir, [fbar(minute(14, 28), close)], run_id=run_id, as_of=LATER) == 1
        assert s.close_at("ESZ26", minute(14, 29), known_at=LATER) == pytest.approx(6030.0)


def test_an_older_file_key_refuses_a_revert_instead_of_dropping_it(tmp_path, staging_dir):
    """A file created before the versioned key keeps PRIMARY KEY (..., content_hash): fail closed."""
    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE futures_bar_1m (contract_id VARCHAR NOT NULL, bar_start_utc TIMESTAMP NOT NULL, "
        "open DOUBLE NOT NULL, high DOUBLE NOT NULL, low DOUBLE NOT NULL, close DOUBLE NOT NULL, "
        "volume BIGINT NOT NULL, source VARCHAR NOT NULL, content_hash VARCHAR NOT NULL, "
        "as_of TIMESTAMP NOT NULL, ingested_at TIMESTAMP NOT NULL, run_id VARCHAR NOT NULL, "
        "PRIMARY KEY (source, contract_id, bar_start_utc, content_hash))"
    )
    con.close()
    with FuturesStore(path, clock=lambda: FIXED_NOW) as s:
        s.ingest_contracts([ESZ26], run_id="c", as_of=FIRST_SEEN, staging_dir=staging_dir)
        ingest(s, staging_dir, [fbar(minute(14, 28), 6030.0)], run_id="run-a", as_of=FIRST_SEEN)
        ingest(s, staging_dir, [fbar(minute(14, 28), 6031.0)], run_id="run-b", as_of=LATER)
        with pytest.raises(ValidationError, match="collided with the primary key"):
            ingest(s, staging_dir, [fbar(minute(14, 28), 6030.0)], run_id="run-c", as_of=FIXED_NOW)
        assert s.row_count("futures_bar_1m") == 2


def test_bars_from_two_sources_in_separate_minutes_still_need_a_source(store, staging_dir, tmp_path):
    """No shared minute, but one window would still stitch two vendors' feeds together."""
    other_source_contract(store, tmp_path)
    ingest(store, staging_dir, [fbar(minute(14, 27), 6028.0)], run_id="run-a")
    ingest(store, staging_dir, [fbar(minute(14, 28), 6100.0, source="other")], run_id="run-b")
    with pytest.raises(ValueError, match="more than one source"):
        store.bars("ESZ26", start_utc=minute(14, 0), end_utc=minute(15, 0))
    primary = store.bars("ESZ26", start_utc=minute(14, 0), end_utc=minute(15, 0), source="databento")
    assert [b.close for b in primary] == [pytest.approx(6028.0)]


def test_a_bar_needs_a_contract_from_its_own_source(store, staging_dir):
    """Only databento stored ESZ26, so an 'other' bar would have no contract under its source."""
    with pytest.raises(ValidationError, match="unknown contract for its source"):
        ingest(store, staging_dir, [fbar(minute(14, 28), 6100.0, source="other")])
    assert store.row_count("futures_bar_1m") == 0


def test_the_dedupe_lookup_is_bounded_to_the_batch_minutes(store, staging_dir):
    """drop_unchanged filters on each key column, so a re-ingest of one minute matches one row."""
    from market_data_ingest.duckdb_batch import drop_unchanged

    ingest(store, staging_dir, PRE_OPEN_BARS)
    rows = [{"source": "databento", "contract_id": "ESZ26", "bar_start_utc": datetime(2026, 11, 11, 14, 28),
             "content_hash": "not-the-stored-hash"}]
    keys = ["source", "contract_id", "bar_start_utc"]
    assert drop_unchanged(store.con, "futures_bar_1m", keys, rows, as_of=datetime(2026, 11, 13)) == rows
    stored = store.raw_rows("futures_bar_1m")[1]
    same = [{**rows[0], "bar_start_utc": stored["bar_start_utc"].replace(tzinfo=None),
             "content_hash": stored["content_hash"]}]
    assert drop_unchanged(store.con, "futures_bar_1m", keys, same, as_of=datetime(2026, 11, 13)) == []
