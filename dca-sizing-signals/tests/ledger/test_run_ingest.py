"""Phase 0 ingest driver: run artifacts + broker order rows -> ``fills`` and ``baseline_fills``.

Spec for ``market_data_ingest.ledger.run_ingest`` (docs/adr-outcome-ledger-pit-harness.md,
Decisions 2, 3, 4 and 7). Written BEFORE the implementation; awaiting operator approval.

Why two inputs and a join, not "read the run directory": a run's own ``orders_pages_*.json`` is
the order history it pulled BEFORE placing, so it can miss that run's own orders. On the real
scrubbed fixtures in scripts/tests/fixtures/budget_exhaustion/, the 2026-11-20 09:35 run finds 1
of its 2 submitted orders in its own pages and the 15:30 run finds 6 of 7. So:

* ATTRIBUTION comes from the run directory: which order ids this run submitted
  (``submitted_orders.csv`` and/or ``finalize_orders.json``), the run id, scheduled vs manual,
  slot, trading day, and each ticker's multiplier and base amount from the candidates file.
* EXECUTION comes from broker order rows (the ``get_equity_orders`` shape): average price,
  filled quantity, ``created_at``, state, side. They are pooled from EVERY run directory's
  ``orders_pages_*.json`` in the tree plus any extra rows the caller passes (a later full pull).
* An attributed order with no filled broker row is SKIPPED with a reason -- never a zero fill
  (Decision 7, fail closed). A broker order no run claims is reported as unattributed and never
  ingested: it might be manual, and only a run directory can say it is scheduled.

Assumptions pinned here, each a deliberate choice the operator can overturn:

* ``filled_at`` is the broker row's ``created_at``: the order rows carry no fill timestamp.
  Fractional dollar orders in these slots fill within seconds, and outcomes count SESSIONS, so
  the session (the run's trading day) is what scoring uses.
* ``notional`` = average price x filled quantity (dollars actually filled), not the requested
  amount.
* Baseline (open question 4, "flat 1x at the same slot timestamp and price"): one baseline row
  per ingested fill, ``decision_id = "<order_id>:baseline"``, at the fill's price and timestamp,
  with ``base_notional`` = the candidate's ``selected_base_amount_usd``. Candidates that were not
  bought get no baseline row, so the comparison is graded-vs-flat on the same buys.
* A directory under any ``manual_runs/`` path is ingested as ``manual`` even if its
  classification says scheduled (fail closed toward exclusion), and the conflict is reported.

Every artifact here is SYNTHETIC and mirrors the real shapes field for field (see the fixtures
named above). Tickers are fake (``ZZZA`` ...). The account number is the suite's synthetic
``999000111``, present only to prove it never reaches the ledger.
"""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_data_ingest.ledger import run_ingest
from market_data_ingest.ledger.store import LedgerStore

AS_OF = datetime(2026, 3, 3, 1, tzinfo=timezone.utc)
SYNTHETIC_ACCOUNT = "999000111"


# --- synthetic artifact builders (real shapes) ---------------------------------------------


def broker_row(order_id: str, symbol: str, *, avg: str = "20.000000", qty: str = "0.500000",
               state: str = "filled", side: str = "buy",
               created_at: str = "2026-03-02T14:36:10.000000Z") -> dict:
    # Same keys as a real get_equity_orders row (orders_pages_week_2026-11-16_0935.json).
    return {
        "average_price": avg, "created_at": created_at, "cumulative_quantity": qty,
        "dollar_based_amount": {"amount": "10.000000", "currency_code": "USD"}, "id": order_id,
        "placed_agent": "agentic", "price": avg, "quantity": qty, "side": side, "state": state,
        "symbol": symbol,
    }


def write_pages(run_dir: Path, rows: list[dict], *, name: str = "orders_pages_week_2026-03-02_0935.json") -> None:
    payload = {"account_number": SYNTHETIC_ACCOUNT, "pages": [{"orders": rows}],
               "snapshot_at": "2026-03-02T09:46:36-05:00", "trading_day": "2026-03-02"}
    (run_dir / name).write_text(json.dumps(payload))


def make_run(root: Path, rel: str, *, orders: list[tuple[str, str]], run_origin: str = "scheduled",
             slot: str | None = "09:35", run_id: str = "market_open_live_20260302_0935ET",
             trading_day: str = "2026-03-02", candidates: dict | None = None,
             cand_shape: str = "cands", summary: bool = True, finalize_json: bool = False) -> Path:
    """One run directory. ``orders`` = [(ticker, order_id)]. ``candidates`` = {ticker: (multiplier, base)}."""
    run_dir = root / rel
    run_dir.mkdir(parents=True)
    if summary:
        rc = {"run_id": run_id, "run_origin": run_origin, "trading_day": trading_day,
              "manual_run": run_origin == "manual", "account_number_redacted": True,
              "triggered_at_utc": "2026-03-02T14:35:46+00:00"}
        if slot is not None:
            rc["scheduled_for_et"] = slot
        (run_dir / "run_summary.json").write_text(json.dumps(
            {"run_id": run_id, "status": "ORDERS_PLACED", "trading_day": trading_day, "run_classification": rc}))
    if finalize_json:
        (run_dir / "finalize_orders.json").write_text(json.dumps(
            [{"order_id": oid, "status": "filled", "submitted_amount_usd": 10.0, "ticker": t} for t, oid in orders]))
    else:
        with (run_dir / "submitted_orders.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["ticker", "order_id", "submitted_amount_usd", "status"])
            for t, oid in orders:
                w.writerow([t, oid, "10.0", "filled"])
    cands = candidates if candidates is not None else {t: (5.0, 2.0) for t, _ in orders}
    if cand_shape == "cands":  # executor-class shape (market_open / morning / midday / intraday)
        body = {"cands": [{"ticker": t, "multiplier": m, "selected_base_amount_usd": b, "current_price": 20.0}
                          for t, (m, b) in cands.items()], "slot": slot, "trading_day": trading_day}
    else:  # 3:30 PM shape (ce16ba6b candidates_*_1530.json)
        body = {"candidates": [{"ticker": t, "action": "BUY", "buy_multiplier": m,
                                "selected_base_amount_usd": b, "target_price": 20.0}
                               for t, (m, b) in cands.items()]}
    (run_dir / f"candidates_{trading_day}_{(slot or '0000').replace(':', '')}.json").write_text(json.dumps(body))
    return run_dir


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "cron_tracking"
    root.mkdir()
    return root


def tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        h.update(str(p.relative_to(root)).encode())
        if p.is_file():
            h.update(p.read_bytes())
    return h.hexdigest()


def reasons(extract) -> dict[str, str]:
    return {s["order_id"]: s["reason"] for s in extract.skipped}


# --- the join --------------------------------------------------------------------------------


def test_scheduled_fill_joins_attribution_with_broker_row(tree: Path) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")],
                   candidates={"ZZZA": (5.0, 2.0)})
    write_pages(run, [broker_row("ord-1", "ZZZA", avg="20.000000", qty="0.500000")])

    ex = run_ingest.extract_tree(tree)

    assert ex.skipped == []
    assert ex.fills == [{
        "order_id": "ord-1", "dca_run_id": "market_open_live_20260302_0935ET", "run_kind": "scheduled",
        "slot": "09:35", "symbol": "ZZZA", "session": "2026-03-02",
        "filled_at": "2026-03-02T14:36:10+00:00",
        "price": 20.0, "quantity": 0.5, "notional": 10.0,  # 20.00 x 0.5
        "multiplier": 5.0,
    }]


def test_own_order_missing_from_own_pages_is_found_in_a_later_runs_pages(tree: Path) -> None:
    # The real-fixture case: the 09:35 run's pages predate its own placement; the 10:30 run's
    # pages (pulled later the same day) contain the 09:35 order.
    early = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    write_pages(early, [])
    later = make_run(tree, "morning_dca/20260302_1030ET", orders=[], slot="10:30",
                     run_id="morning_live_20260302_1030ET")
    write_pages(later, [broker_row("ord-1", "ZZZA")], name="orders_pages_week_2026-03-02_1030.json")

    ex = run_ingest.extract_tree(tree)

    assert [f["order_id"] for f in ex.fills] == ["ord-1"]
    assert ex.fills[0]["dca_run_id"] == "market_open_live_20260302_0935ET"  # attribution, not page owner


def test_extra_broker_rows_from_the_caller_are_used(tree: Path) -> None:
    make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    ex = run_ingest.extract_tree(tree, broker_orders=[broker_row("ord-1", "ZZZA")])
    assert [f["order_id"] for f in ex.fills] == ["ord-1"]


def test_finalize_orders_json_is_an_order_list_too(tree: Path) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")], finalize_json=True)
    write_pages(run, [broker_row("ord-1", "ZZZA")])
    assert [f["order_id"] for f in run_ingest.extract_tree(tree).fills] == ["ord-1"]


def test_1530_candidate_shape_reads_buy_multiplier(tree: Path) -> None:
    run = make_run(tree, "ce16ba6b/scheduled_runs/20260302_1530ET", orders=[("ZZZA", "ord-1")], slot="15:30",
                   run_id="scheduled_20260302_1530ET", cand_shape="candidates", candidates={"ZZZA": (2.9266, 1.0)})
    write_pages(run, [broker_row("ord-1", "ZZZA")], name="orders_pages_week_2026-03-02_1530.json")

    ex = run_ingest.extract_tree(tree)

    assert ex.fills[0]["multiplier"] == 2.9266
    assert ex.fills[0]["slot"] == "15:30"
    assert ex.baselines[0]["base_notional"] == 1.0


# --- the baseline ----------------------------------------------------------------------------


def test_one_baseline_per_fill_at_the_fills_price_and_time(tree: Path) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")],
                   candidates={"ZZZA": (5.0, 2.0), "ZZZB": (3.0, 1.0)})  # ZZZB was not bought
    write_pages(run, [broker_row("ord-1", "ZZZA", avg="20.000000", qty="0.500000")])

    ex = run_ingest.extract_tree(tree)

    assert ex.baselines == [{
        "decision_id": "ord-1:baseline", "dca_run_id": "market_open_live_20260302_0935ET",
        "run_kind": "scheduled", "slot": "09:35", "symbol": "ZZZA", "session": "2026-03-02",
        "decided_at": "2026-03-02T14:36:10+00:00", "price": 20.0, "base_notional": 2.0,
    }]


# --- fail closed -----------------------------------------------------------------------------


def test_attributed_order_with_no_broker_row_is_skipped_never_zero(tree: Path) -> None:
    make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    ex = run_ingest.extract_tree(tree)
    assert ex.fills == [] and ex.baselines == []
    assert reasons(ex) == {"ord-1": "no_broker_row"}


@pytest.mark.parametrize(("row", "reason"), [
    (broker_row("ord-1", "ZZZA", state="cancelled"), "not_filled"),
    (broker_row("ord-1", "ZZZA", state="partially_filled"), "not_filled"),
    (broker_row("ord-1", "ZZZA", side="sell"), "not_a_buy"),
    (broker_row("ord-1", "ZZZB"), "symbol_mismatch"),
    (broker_row("ord-1", "ZZZA", qty="0"), "bad_broker_values"),
    (broker_row("ord-1", "ZZZA", avg="nan"), "bad_broker_values"),
])
def test_unusable_broker_rows_are_skipped_with_a_reason(tree: Path, row: dict, reason: str) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    write_pages(run, [row])
    ex = run_ingest.extract_tree(tree)
    assert ex.fills == []
    assert reasons(ex) == {"ord-1": reason}


def test_conflicting_broker_rows_for_one_order_are_skipped(tree: Path) -> None:
    a = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    write_pages(a, [broker_row("ord-1", "ZZZA", avg="20.000000")])
    b = make_run(tree, "morning_dca/20260302_1030ET", orders=[], slot="10:30", run_id="morning_live_20260302_1030ET")
    write_pages(b, [broker_row("ord-1", "ZZZA", avg="21.000000")], name="orders_pages_week_2026-03-02_1030.json")

    ex = run_ingest.extract_tree(tree)

    assert ex.fills == []
    assert reasons(ex) == {"ord-1": "conflicting_broker_rows"}


def test_identical_broker_rows_in_many_pages_are_one_row(tree: Path) -> None:
    a = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    write_pages(a, [broker_row("ord-1", "ZZZA")])
    b = make_run(tree, "morning_dca/20260302_1030ET", orders=[], slot="10:30", run_id="morning_live_20260302_1030ET")
    write_pages(b, [broker_row("ord-1", "ZZZA")], name="orders_pages_week_2026-03-02_1030.json")
    assert len(run_ingest.extract_tree(tree).fills) == 1


def test_order_claimed_by_two_runs_is_skipped(tree: Path) -> None:
    a = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    write_pages(a, [broker_row("ord-1", "ZZZA")])
    make_run(tree, "morning_dca/20260302_1030ET", orders=[("ZZZA", "ord-1")], slot="10:30",
             run_id="morning_live_20260302_1030ET")
    ex = run_ingest.extract_tree(tree)
    assert ex.fills == []
    assert reasons(ex) == {"ord-1": "claimed_by_multiple_runs"}


def test_missing_candidate_means_no_fill(tree: Path) -> None:
    # Without the candidate the multiplier is unknown; a guessed 1x would bias the comparison.
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")], candidates={})
    write_pages(run, [broker_row("ord-1", "ZZZA")])
    ex = run_ingest.extract_tree(tree)
    assert ex.fills == []
    assert reasons(ex) == {"ord-1": "no_candidate"}


def test_run_dir_without_summary_is_skipped_whole(tree: Path) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")], summary=False)
    write_pages(run, [broker_row("ord-1", "ZZZA")])
    ex = run_ingest.extract_tree(tree)
    assert ex.fills == []
    assert ex.skipped_runs == [{"run_dir": "market_open_dca/20260302_0935ET", "reason": "no_run_summary"}]
    # Its broker rows are still pooled, but nothing claims ord-1, so it is unattributed.
    assert ex.unattributed_order_ids == ["ord-1"]


def test_broker_order_no_run_claims_is_never_a_fill(tree: Path) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[])
    write_pages(run, [broker_row("ord-x", "ZZZA")])
    ex = run_ingest.extract_tree(tree)
    assert ex.fills == []
    assert ex.unattributed_order_ids == ["ord-x"]


# --- run classification ------------------------------------------------------------------------


def test_manual_run_is_ingested_as_manual(tree: Path) -> None:
    run = make_run(tree, "market_open_dca/manual_runs/20260302T150000Z", orders=[("ZZZA", "ord-m")],
                   run_origin="manual", slot=None, run_id="manual_20260302T150000Z")
    write_pages(run, [broker_row("ord-m", "ZZZA")])

    ex = run_ingest.extract_tree(tree)

    assert ex.fills[0]["run_kind"] == "manual"
    # No scheduled slot: the slot is the ET clock time the run was triggered (14:35:46Z = 09:35 EST).
    assert ex.fills[0]["slot"] == "09:35"


def test_manual_path_overrides_a_scheduled_classification(tree: Path) -> None:
    run = make_run(tree, "midday_dca/manual_runs/20260302T150000Z", orders=[("ZZZA", "ord-m")],
                   run_origin="scheduled")
    write_pages(run, [broker_row("ord-m", "ZZZA")])

    ex = run_ingest.extract_tree(tree)

    assert ex.fills[0]["run_kind"] == "manual"
    assert {"run_dir": "midday_dca/manual_runs/20260302T150000Z",
            "warning": "manual_path_overrides_scheduled_origin"} in ex.warnings


def test_unknown_run_origin_skips_the_run(tree: Path) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")], run_origin="backfill")
    write_pages(run, [broker_row("ord-1", "ZZZA")])
    ex = run_ingest.extract_tree(tree)
    assert ex.fills == []
    assert ex.skipped_runs == [{"run_dir": "market_open_dca/20260302_0935ET", "reason": "unknown_run_origin"}]


# --- into the store ----------------------------------------------------------------------------


def test_ingest_tree_writes_fills_and_baselines_and_is_idempotent(tree: Path, ledger: LedgerStore) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1"), ("ZZZB", "ord-2")])
    write_pages(run, [broker_row("ord-1", "ZZZA"), broker_row("ord-2", "ZZZB")])

    first = run_ingest.ingest_tree(ledger, tree, run_id="ingest-1", as_of=AS_OF)
    again = run_ingest.ingest_tree(ledger, tree, run_id="ingest-2", as_of=AS_OF)

    assert (first.fills_inserted, first.baselines_inserted) == (2, 2)
    assert (again.fills_inserted, again.baselines_inserted) == (0, 0)
    stored = ledger.fills()
    assert [r["order_id"] for r in stored] == ["ord-1", "ord-2"]
    assert {r["source"] for r in stored} == {"run_artifact"}
    assert [r["decision_id"] for r in ledger.baseline_fills()] == ["ord-1:baseline", "ord-2:baseline"]


def test_account_number_in_pages_never_reaches_the_store(tree: Path, ledger: LedgerStore) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    write_pages(run, [broker_row("ord-1", "ZZZA")])  # payload carries account_number 999000111

    run_ingest.ingest_tree(ledger, tree, run_id="ingest-1", as_of=AS_OF)

    dump = json.dumps(ledger.fills() + ledger.baseline_fills(), default=str)
    assert SYNTHETIC_ACCOUNT not in dump


def test_extraction_never_writes_to_the_artifact_tree(tree: Path, ledger: LedgerStore) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    write_pages(run, [broker_row("ord-1", "ZZZA")])
    before = tree_digest(tree)
    run_ingest.ingest_tree(ledger, tree, run_id="ingest-1", as_of=AS_OF)
    assert tree_digest(tree) == before


def test_malformed_json_skips_that_run_only(tree: Path) -> None:
    good = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1")])
    write_pages(good, [broker_row("ord-1", "ZZZA")])
    bad = make_run(tree, "morning_dca/20260302_1030ET", orders=[("ZZZB", "ord-2")], slot="10:30",
                   run_id="morning_live_20260302_1030ET")
    (bad / "run_summary.json").write_text("{not json")

    ex = run_ingest.extract_tree(tree)

    assert [f["order_id"] for f in ex.fills] == ["ord-1"]
    assert ex.skipped_runs == [{"run_dir": "morning_dca/20260302_1030ET", "reason": "unreadable_run_summary"}]


# --- CLI ---------------------------------------------------------------------------------------


def test_cli_ingests_and_prints_a_json_report(tree: Path, db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = make_run(tree, "market_open_dca/20260302_0935ET", orders=[("ZZZA", "ord-1"), ("ZZZB", "ord-2")])
    write_pages(run, [broker_row("ord-1", "ZZZA")])

    code = run_ingest.main(["--root", str(tree), "--db", str(db_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["fills_inserted"] == 1
    assert report["skipped"] == [{"run_dir": "market_open_dca/20260302_0935ET", "order_id": "ord-2",
                                  "reason": "no_broker_row"}]


def test_cli_missing_root_fails(tmp_path: Path, db_path: Path) -> None:
    assert run_ingest.main(["--root", str(tmp_path / "nope"), "--db", str(db_path)]) == 2
