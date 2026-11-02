"""Ledger schema contract: idempotent creation, provenance on every row, append-only writes.

Same write rules as the price store (market_data_ingest/prices/store.py), restated for the
Phase 0 tables (proposed ADR docs/adr-outcome-ledger-pit-harness.md, decision 2):

- creating the schema twice on one file is harmless and keeps existing rows;
- every ledger table carries ``source, as_of, ingested_at, run_id`` and a ``content_hash``;
- there is no UPDATE / DELETE / DROP / TRUNCATE anywhere in the ledger package;
- re-ingesting identical rows is a no-op (``content_hash`` key + ON CONFLICT DO NOTHING);
- a correction lands as a new row with a later ``as_of``; readers see the latest version;
- one bad row rejects the whole batch and writes nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .conftest import FIRST_SEEN, LATER, baseline, fill, ingest_calendar, utc

LEDGER_TABLES = (
    "market_sessions",
    "fills",
    "baseline_fills",
    "outcomes",
    "accounts",
    "tax_lots",
    "trials",
    "universe_scores",
    "universe_proposals",
    "universe_membership",
)
LEDGER_MACROS = (
    "fills_known_at",
    "baseline_fills_known_at",
    "outcome_candidates",
    "outcomes_known_at",
    "scheduled_outcomes",
    "trial_counts",
    "universe_members_as_of",
)
PROVENANCE = {"source", "as_of", "ingested_at", "run_id", "content_hash"}
LEDGER_PACKAGE = Path(__file__).resolve().parents[2] / "market_data_ingest" / "ledger"


def _macro_names(con) -> set[str]:
    rows = con.execute("SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'table_macro'")
    return {r[0] for r in rows.fetchall()}


def test_schema_creates_every_table_and_macro(ledger):
    assert set(LEDGER_TABLES) <= set(ledger.tables())
    assert set(LEDGER_MACROS) <= _macro_names(ledger.con)


def test_schema_also_creates_the_price_store_tables_on_the_same_file(ledger):
    # The outcome macros read bars_known_at/actions_known_at, so the price schema must
    # exist in the same database whether or not anyone opened a PriceStore first.
    assert {"daily_bars_raw", "corporate_actions"} <= set(ledger.tables())
    assert {"bars_known_at", "actions_known_at", "adjusted_bars"} <= _macro_names(ledger.con)


def test_reopening_the_same_file_is_idempotent_and_keeps_rows(db_path):
    from market_data_ingest.ledger.store import LedgerStore

    with LedgerStore(db_path) as first:
        assert first.ingest_fills([fill("ord-1")], source="run_artifact", run_id="r1", as_of=FIRST_SEEN) == 1
    with LedgerStore(db_path) as second:  # CREATE ... IF NOT EXISTS / CREATE OR REPLACE MACRO
        assert second.row_count("fills") == 1
        assert set(LEDGER_TABLES) <= set(second.tables())


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_every_ledger_table_carries_provenance_columns(ledger, table):
    assert PROVENANCE <= set(ledger.table_columns(table))


def test_ledger_package_has_no_update_delete_drop_or_truncate():
    # Static check over the package source, SQL strings included. Word-bounded and
    # upper-case so prose like "updated" in a docstring does not trip it.
    banned = re.compile(r"\b(UPDATE|DELETE|DROP|TRUNCATE)\b")
    offenders = []
    for py in sorted(LEDGER_PACKAGE.rglob("*.py")):
        for n, line in enumerate(py.read_text().splitlines(), 1):
            if banned.search(line):
                offenders.append(f"{py.name}:{n}: {line.strip()}")
    assert LEDGER_PACKAGE.exists(), "market_data_ingest/ledger/ not found"
    assert not offenders, "append-only ledger must not mutate rows:\n" + "\n".join(offenders)


def test_store_exposes_no_mutating_methods(ledger):
    names = {n for n in dir(ledger) if not n.startswith("_")}
    assert not {n for n in names if re.match(r"(update|delete|remove|drop|truncate|upsert)", n)}


def test_identical_reingest_is_a_no_op_via_content_hash(ledger):
    rows = [fill("ord-1"), fill("ord-2", price=20.0)]
    assert ledger.ingest_fills(rows, source="run_artifact", run_id="r1", as_of=FIRST_SEEN) == 2
    # Same values, different ingest run and a later as_of: still the same content_hash,
    # so nothing is added.
    assert ledger.ingest_fills(rows, source="run_artifact", run_id="r2", as_of=LATER) == 0
    assert ledger.row_count("fills") == 2

    assert ledger.ingest_baseline_fills([baseline("dec-1")], source="run_artifact", run_id="r1", as_of=FIRST_SEEN) == 1
    assert ledger.ingest_baseline_fills([baseline("dec-1")], source="run_artifact", run_id="r2", as_of=LATER) == 0

    assert ingest_calendar(ledger) == 30
    assert ingest_calendar(ledger, run_id="cal-2", as_of=LATER) == 0


def test_correction_is_a_new_row_and_readers_see_the_latest(ledger):
    ledger.ingest_fills([fill("ord-1", price=10.0)], source="run_artifact", run_id="r1", as_of=FIRST_SEEN)
    # Broker order history later reports a different average price for the same order.
    ledger.ingest_fills([fill("ord-1", price=10.5)], source="broker_order_history", run_id="r2", as_of=LATER)
    assert ledger.row_count("fills") == 2  # nothing overwritten
    latest = ledger.fills(known_at=utc(2026, 6, 1))
    assert [(r["order_id"], r["price"]) for r in latest] == [("ord-1", 10.5)]
    # Point in time: as known between the two ingests, the first version is the answer.
    earlier = ledger.fills(known_at=utc(2026, 4, 30, 12))
    assert [(r["order_id"], r["price"]) for r in earlier] == [("ord-1", 10.0)]


def test_one_bad_row_rejects_the_whole_batch(ledger):
    from market_data_ingest.ledger.errors import LedgerValidationError

    bad = fill("ord-2")
    bad["price"] = -1.0
    with pytest.raises(LedgerValidationError):
        ledger.ingest_fills([fill("ord-1"), bad], source="run_artifact", run_id="r1", as_of=FIRST_SEEN)
    assert ledger.row_count("fills") == 0


def test_unknown_keys_are_rejected_not_silently_dropped(ledger):
    from market_data_ingest.ledger.errors import LedgerValidationError

    row = fill("ord-1")
    row["note"] = "anything"
    with pytest.raises(LedgerValidationError, match="note"):
        ledger.ingest_fills([row], source="run_artifact", run_id="r1", as_of=FIRST_SEEN)


def test_naive_timestamps_are_refused(ledger):
    with pytest.raises(ValueError):
        ledger.ingest_fills([fill("ord-1")], source="run_artifact", run_id="r1", as_of=FIRST_SEEN.replace(tzinfo=None))


def test_fill_source_vocabulary_is_closed(ledger):
    from market_data_ingest.ledger.errors import LedgerValidationError

    with pytest.raises(LedgerValidationError):
        ledger.ingest_fills([fill("ord-1")], source="somewhere_else", run_id="r1", as_of=FIRST_SEEN)
