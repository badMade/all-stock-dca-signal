"""Tests for scripts/purchase_history.py.

Runnable with ``pytest scripts/tests`` or ``python scripts/tests/test_purchase_history.py``.
"""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(1, str(Path(__file__).resolve().parents[2]))

from purchase_history import (  # noqa: E402
    build_ticker_rollup,
    collect_purchase_records,
    main,
    write_purchase_history,
)


def _write_run(
    root: Path,
    run_dir_name: str,
    *,
    trading_day: str,
    run_id: str | None = None,
    stream: str | None = None,
    run_origin: str = "scheduled",
    filled_orders_csv: str,
    broker: object = None,
) -> Path:
    run_dir = root / run_dir_name
    run_dir.mkdir(parents=True, exist_ok=True)
    state = {
        "run_id": run_id,
        "generated_at": f"{trading_day}T15:30:00-04:00",
        "trading_day": trading_day,
        "run_classification": {
            "run_origin": run_origin,
            "manual_run": run_origin == "manual",
            "cron_id": "10ce362c",
            "run_id": run_id,
            "triggered_at_utc": f"{trading_day}T19:30:00Z",
            "state_schema_version": "run-classification-v2-preauthorized",
            "stream": stream,
        },
        # Real run_state.json shapes carry a broker name string (see
        # near_close_dca.py's BrokerName enum), but the default `{}` here
        # (an older stub shape this fixture predates broker-scoping by)
        # is deliberately preserved for every existing caller that doesn't
        # pass `broker=` explicitly, so this fixture change cannot silently
        # alter any pre-existing test's dedup behavior.
        "broker": broker if broker is not None else {},
        "budget": {},
        "counts": {},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(filled_orders_csv, encoding="utf-8")
    return run_dir


def test_collect_purchase_records_reads_confirmed_fills(tmp_path):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "afternoon_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,3.50,filled,109.989900,0.031821\n"
            "GFUZ,10.00,filled,6.249900,1.600025\n"
        ),
    )

    records, warnings = collect_purchase_records(root)

    assert warnings == []
    assert len(records) == 2
    spcx = next(r for r in records if r.ticker == "SPCX")
    assert spcx.avg_fill_price == pytest.approx(109.9899)
    assert spcx.quantity == pytest.approx(0.031821)
    # The executed notional (price * quantity, rounded to 6 decimals -- not
    # the CSV's raw requested $3.50) is intentionally NOT exactly 3.50; see
    # test_collect_purchase_records_prefers_executed_notional_over_requested.
    assert spcx.amount_usd == pytest.approx(3.499989)
    assert spcx.trading_day == "2026-08-05"
    assert spcx.stream == "afternoon"
    assert spcx.run_origin == "scheduled"


def test_collect_purchase_records_prefers_executed_notional_over_requested(tmp_path):
    # near_close_dca._log_execution populates the standard full-report CSV's
    # notional_usd/amount_usd with the REQUESTED amount, not a recomputed
    # executed value; only a PARTIAL row gets price*quantity recomputed
    # inside _orders_csv_fills itself. A fully filled row whose requested
    # amount differs from price*quantity (slippage/rounding) must report the
    # executed product -- this repo's own reporting already treats
    # avg_fill_price*quantity_filled as the true executed notional.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "afternoon_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,amount_usd,state,average_fill_price,quantity\n"
            # Requested $10.00, but price*quantity = 100.00 * 0.09 = $9.00.
            "MSFT,10.00,filled,100.00,0.09\n"
        ),
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].amount_usd == pytest.approx(9.00)


def test_collect_purchase_records_dedupes_by_order_id_across_run_dirs(tmp_path):
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMZN,order-123,4.25,filled,242.1099,0.017554\n"
    )
    _write_run(
        root,
        "original",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
    )
    _write_run(
        root,
        "manual_reruns/recovery_copy",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=csv_body,
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].order_id == "order-123"
    # "manual_reruns/recovery_copy" sorts before "original" lexicographically,
    # so a naive first-seen-wins dedup would misattribute this to the manual
    # copy. The scheduled run that actually placed the order must win.
    assert records[0].run_origin == "scheduled"
    assert records[0].run_id == "scheduled_20260805_1530ET"
    assert any("duplicate order_id" in w for w in warnings)


def test_collect_purchase_records_dedup_prefers_scheduled_regardless_of_scan_order(tmp_path):
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMZN,order-999,4.25,filled,242.1099,0.017554\n"
    )
    # "a_manual_copy" sorts BEFORE "original" too, exercising the same
    # scan-order trap from the other direction.
    _write_run(
        root,
        "a_manual_copy",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=csv_body,
    )
    _write_run(
        root,
        "original",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].run_origin == "scheduled"
    assert records[0].run_id == "scheduled_20260805_1530ET"
    assert any("duplicate order_id" in w for w in warnings)


def test_collect_purchase_records_skips_dry_run(tmp_path):
    root = tmp_path / "cron_tracking"
    run_dir = _write_run(
        root,
        "demo_run",
        trading_day="2026-08-05",
        run_id="demo_20260805",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=(
            "ticker,amount_usd,state,average_fill_price,quantity\nAAPL,5.00,filled,200.00,0.025\n"
        ),
    )
    state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
    state["dry_run"] = True
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert warnings == []


def test_collect_purchase_records_follows_bare_submitted_orders_csv(tmp_path):
    # market-open/intraday streams have no in-repo Python entry point and
    # write only a bare sibling submitted_orders.csv -- no artifacts block,
    # no filled_orders.csv/orders.csv.
    root = tmp_path / "cron_tracking"
    run_dir = root / "market_open_dca" / "20260805_0935ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260805_0935ET",
        "generated_at": "2026-08-05T13:35:00Z",
        "trading_day": "2026-08-05",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "market_open",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nNVDA,7.00,filled,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "NVDA"
    # Executed notional (120.00 * 0.058333, rounded to 6 decimals), not the
    # CSV's raw requested $7.00 -- see amount_usd precision note above.
    assert records[0].amount_usd == pytest.approx(6.99996)


def test_collect_purchase_records_prefers_confirmed_sibling_over_bare_submitted(tmp_path):
    # A run directory can carry BOTH the canonical submitted_orders.csv and a
    # later confirmed filled_orders.csv (submission evidence retained
    # alongside reconciliation). The confirmed file must win -- forcing the
    # bare submitted file bypasses _orders_csv_fills's own preference for the
    # confirmed sibling and can lose confirmed price/quantity or see no fill
    # at all for a row still reading status=submitted.
    root = tmp_path / "cron_tracking"
    run_dir = root / "market_open_dca" / "20260805_0935ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260805_0935ET",
        "generated_at": "2026-08-05T13:35:00Z",
        "trading_day": "2026-08-05",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "market_open",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    # Still-submitted (unconfirmed) row -- must NOT be what the ledger reads.
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,amount_usd,state\nNVDA,7.00,submitted\n",
        encoding="utf-8",
    )
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nNVDA,7.00,filled,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "NVDA"
    assert records[0].avg_fill_price == pytest.approx(120.00)
    assert records[0].quantity == pytest.approx(0.058333)


def test_collect_purchase_records_dedup_prefers_larger_partial_snapshot(tmp_path):
    # Two directories record PROGRESSIVE partially_filled snapshots of the
    # same order (e.g. a recovery copy taken at an earlier point than
    # another). Both are equally "partial" (same completeness rank), so the
    # snapshot with more executed quantity -- the more advanced evidence --
    # must win, not whichever directory happens to sort first.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "a_early_snapshot",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery_early",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=(
            "ticker,order_id,state,average_fill_price,quantity\n"
            "AMD,order-77,partially_filled,100.00,0.01\n"
        ),
    )
    _write_run(
        root,
        "b_later_snapshot",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery_later",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=(
            "ticker,order_id,state,average_fill_price,quantity\n"
            "AMD,order-77,partially_filled,100.00,0.04\n"
        ),
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].quantity == pytest.approx(0.04)
    assert records[0].amount_usd == pytest.approx(4.00)


def test_collect_purchase_records_dedup_prefers_larger_unpriced_partial_over_smaller_priced(tmp_path):
    # Two PARTIAL snapshots: the older/smaller one has a settled price, the
    # later/larger one doesn't yet. Execution progress (quantity) must win
    # over price completeness for partial snapshots -- the opposite priority
    # from two COMPLETE snapshots.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "a_small_priced",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery_early",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=(
            "ticker,order_id,state,average_fill_price,quantity\n"
            "AMD,order-88,partially_filled,100.00,0.01\n"
        ),
    )
    _write_run(
        root,
        "b_large_unpriced",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery_later",
        stream="afternoon",
        run_origin="manual",
        # No average_fill_price column at all.
        filled_orders_csv=("ticker,order_id,amount_usd,quantity\nAMD,order-88,4.00,0.04\n"),
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].quantity == pytest.approx(0.04)


def test_collect_purchase_records_dedup_prefers_scheduled_over_scheduled_recovery(tmp_path):
    # "scheduled" must outrank "scheduled_recovery" (not merely tie with it,
    # which would leave the outcome to scan order).
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NVDA,order-99,6.00,filled,300.00,0.02\n"
    )
    _write_run(
        root,
        "a_recovery_copy",
        trading_day="2026-08-05",
        run_id="scheduled_recovery_20260805",
        stream="afternoon",
        run_origin="scheduled_recovery",
        filled_orders_csv=csv_body,
    )
    _write_run(
        root,
        "b_original_scheduled",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].run_origin == "scheduled"
    assert records[0].run_id == "scheduled_20260805_1530ET"


def test_collect_purchase_records_does_not_merge_attribution_across_directories(tmp_path):
    # Two INDEPENDENT directories both reference the same order_id at equal
    # rank (both "scheduled"), one carrying a stream but no run_id, the
    # other a run_id but no stream. Unlike two run_state*.json variants in
    # ONE directory (which genuinely describe the identical run and are
    # safe to merge field-by-field), these are two SEPARATE directories --
    # merging their fields could label the record with a stream/run_id
    # combination that belonged to NEITHER state (Codex review: "Restrict
    # attribution merging to same-directory states").
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NVDA,order-cross-dir-1,6.00,filled,300.00,0.02\n"
    )
    _write_run(
        root,
        "afternoon_run_a_no_run_id",
        trading_day="2026-08-29",
        run_id=None,
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
    )
    _write_run(
        root,
        "afternoon_run_b_no_stream",
        trading_day="2026-08-29",
        run_id="scheduled_20260829_copy",
        stream=None,
        run_origin="scheduled",
        filled_orders_csv=csv_body,
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    # The record must match ONE directory's own complete attribution, not a
    # Frankenstein combination of stream from one and run_id from the other.
    assert not (records[0].stream == "afternoon" and records[0].run_id == "scheduled_20260829_copy")


def test_collect_purchase_records_normalizes_placeholder_order_id(tmp_path):
    # "-" is a documented placeholder for "no order id" some CSV shapes use.
    # Treating it as a real order_id would give unrelated tickers/runs the
    # SAME dedup key.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "AAA,-,3.00,filled,100.00,0.03\n"
            "BBB,-,4.00,filled,200.00,0.02\n"
        ),
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 2
    tickers = {r.ticker for r in records}
    assert tickers == {"AAA", "BBB"}
    assert not any("duplicate order_id" in w for w in warnings)


def test_collect_purchase_records_dedup_prefers_resolved_quantity_over_amount_only(tmp_path):
    # A fully-priced completed snapshot (quantity=0.05) must win over an
    # amount-only completed snapshot ($5) for the SAME order, even though the
    # raw amount-only number (5) is numerically larger than the quantity
    # (0.05) -- they are not comparable units.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "a_amount_only_copy",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=("ticker,order_id,amount_usd\nAMD,order-90,5.00\n"),
    )
    _write_run(
        root,
        "b_fully_priced",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=(
            "ticker,order_id,state,average_fill_price,quantity\n"
            "AMD,order-90,filled,100.00,0.05\n"
        ),
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].quantity == pytest.approx(0.05)
    assert records[0].avg_fill_price == pytest.approx(100.00)


def test_collect_purchase_records_reads_full_report_legacy_provenance(tmp_path):
    # near_close_dca.write_run_state's full-report shape has no
    # run_classification block and no top-level run_id -- only top-level
    # "scheduled" (bool) and "run_label".
    root = tmp_path / "cron_tracking"
    run_dir = root / "scheduled_runs" / "20260805_1530ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_label": "dca_afternoon_20260805_153000",
        "scheduled": True,
        "trading_day": "2026-08-05",
        "artifacts": {"orders_csv_path": "dca_afternoon_20260805_153000_orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "dca_afternoon_20260805_153000_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nORCL,9.00,filled,150.00,0.06\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].run_origin == "scheduled"
    assert records[0].run_id == "dca_afternoon_20260805_153000"


def test_collect_purchase_records_scans_additional_roots(tmp_path):
    # A relocated stream artifact root (e.g. DCA_MARKET_OPEN_LIVE_STATE_ROOT)
    # lives outside the main cron-tracking tree entirely.
    main_root = tmp_path / "cron_tracking"
    other_root = tmp_path / "relocated_market_open_root"
    _write_run(
        main_root,
        "afternoon_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,amount_usd,state,average_fill_price,quantity\nAAPL,3.00,filled,150.00,0.02\n"
        ),
    )
    _write_run(
        other_root,
        "market_open_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_0935ET",
        stream="market_open",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,amount_usd,state,average_fill_price,quantity\nMSFT,4.00,filled,300.00,0.013333\n"
        ),
    )

    records, _ = collect_purchase_records(main_root, additional_roots=[other_root])

    tickers = {r.ticker for r in records}
    assert tickers == {"AAPL", "MSFT"}


def test_collect_purchase_records_searches_enclosing_recovery_root(tmp_path):
    # A recovery run can write artifacts/run_state.json beneath a
    # "*_recovery_*" directory while its confirmed filled_orders.csv sits at
    # that enclosing directory's own root, with no artifacts.orders_csv_path
    # recorded to point at it.
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260805"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260805_recovery",
        "generated_at": "2026-08-05T17:30:00Z",
        "trading_day": "2026-08-05",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-321,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "CRM"
    assert records[0].amount_usd == pytest.approx(6.00)


def test_collect_purchase_records_keeps_distinct_recovery_orders_separate(tmp_path):
    # When the nested artifact directory and the enclosing recovery root each
    # have a DISTINCT confirmed order for the same ticker, both must survive
    # as separate ledger rows -- near_close_dca._merge_fill would blend them
    # into one allocation-oriented aggregate with order_id=None, which is
    # wrong for a per-order purchase ledger (it understates buy_count and
    # loses the order_id identity a later duplicate would need to dedup
    # against).
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260806"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260806_recovery",
        "generated_at": "2026-08-06T17:30:00Z",
        "trading_day": "2026-08-06",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (artifacts_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-111,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-222,5.00,filled,250.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 2
    order_ids = {r.order_id for r in records}
    assert order_ids == {"order-111", "order-222"}
    assert sum(r.amount_usd for r in records if r.amount_usd is not None) == pytest.approx(9.00)


def test_collect_purchase_records_keeps_same_ticker_orders_in_one_csv(tmp_path):
    """Retry-created orders for one ticker remain separate ledger purchases."""
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "afternoon_run",
        trading_day="2026-08-06",
        run_id="scheduled_20260806_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,state,average_fill_price,quantity\n"
            "CRM,order-111,filled,200.00,0.02\n"
            "CRM,order-222,filled,250.00,0.02\n"
        ),
    )

    records, _ = collect_purchase_records(root)

    assert {record.order_id for record in records} == {"order-111", "order-222"}
    assert sum(record.amount_usd for record in records if record.amount_usd is not None) == pytest.approx(9.00)
    assert build_ticker_rollup(records)["CRM"]["buy_count"] == 2


def test_collect_purchase_records_scopes_no_id_recovery_fills_to_their_root(tmp_path):
    # The nested artifacts/ directory and the enclosing recovery root can each
    # contribute a DISTINCT no-order-id fill for the same ticker (a real,
    # older shape some manual/recovery filled_orders.csv files predate).
    # Keying both by the outer run_dir alone collided them into one entry.
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260807"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260807_recovery",
        "generated_at": "2026-08-07T17:30:00Z",
        "trading_day": "2026-08-07",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    # No order_id column at all in either CSV.
    (artifacts_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,5.00,filled,250.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 2
    assert sum(r.amount_usd for r in records if r.amount_usd is not None) == pytest.approx(9.00)


def test_collect_purchase_records_does_not_double_read_recorded_recovery_csv(tmp_path):
    # A nested recovery state records artifacts.orders_csv_path pointing at
    # the recovery root's OWN filled_orders.csv (a legitimate in-root
    # relative path, e.g. "../filled_orders.csv"). The recovery-root fallback
    # must not read that SAME physical file a second time, or every
    # no-order-id row in it is double-counted.
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260808"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260808_recovery",
        "generated_at": "2026-08-08T17:30:00Z",
        "trading_day": "2026-08-08",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
        "artifacts": {"orders_csv_path": "../filled_orders.csv"},
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    # No order_id column -- the shape most exposed to double-counting.
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].amount_usd == pytest.approx(4.00)
    assert records[0].quantity == pytest.approx(0.02)


def test_collect_purchase_records_retries_sibling_beside_recorded_recovery_csv(tmp_path):
    # A nested recovery state records artifacts.orders_csv_path pointing at
    # the recovery root's OWN filled_orders.csv ("../filled_orders.csv" from
    # the artifacts/ dir run_state.json lives in) -- run_dir (artifacts/) and
    # the resolved preferred CSV's directory (recovery_dir) are DIFFERENT.
    # When that preferred filled_orders.csv is header-only, the sibling
    # orders.csv retry must look beside the resolved CSV (recovery_dir), not
    # beside run_dir, or it looks in the wrong directory and misses real
    # evidence sitting right next to the preferred file (Codex review:
    # "Resolve fallback beside the preferred CSV").
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260809"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260809_recovery",
        "generated_at": "2026-08-09T17:30:00Z",
        "trading_day": "2026-08-09",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
        "artifacts": {"orders_csv_path": "../filled_orders.csv"},
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (recovery_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-recorded-sib-1,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )
    # A distractor orders.csv beside run_dir (artifacts/) must NOT be picked
    # up instead -- it belongs to a different, unrelated directory.
    (artifacts_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "AAPL,order-wrong-dir-1,9.00,filled,180.00,0.05\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "CRM"
    assert records[0].order_id == "order-recorded-sib-1"
    assert any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_no_id_row_not_split_by_differing_broker_metadata(tmp_path):
    # Two run_state*.json variants in the SAME directory (e.g. run_state.json
    # and run_state_scheduled.json) can read the SAME no-order-id CSV row
    # while resolving DIFFERENT broker values -- one variant's
    # run_classification carries execution_broker, an older/legacy sibling
    # variant doesn't. The no-order-id dedup key must not include broker, or
    # this one physical row is recorded twice with doubled quantity/invested
    # dollars (Codex review: "Deduplicate brokerless copies before applying
    # broker fallback").
    root = tmp_path / "cron_tracking"
    run_dir = root / "midday_run"
    run_dir.mkdir(parents=True)
    base_state = {
        "run_id": "scheduled_20260810_1230ET",
        "generated_at": "2026-08-10T16:30:00Z",
        "trading_day": "2026-08-10",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "midday",
        },
    }
    known_broker_state = dict(base_state)
    known_broker_state["run_classification"] = {
        **base_state["run_classification"],
        "execution_broker": "robinhood",
    }
    (run_dir / "run_state.json").write_text(json.dumps(base_state), encoding="utf-8")
    (run_dir / "run_state_scheduled.json").write_text(json.dumps(known_broker_state), encoding="utf-8")
    # No order_id column -- the shape most exposed to this collision.
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].amount_usd == pytest.approx(4.00)
    assert records[0].quantity == pytest.approx(0.02)
    # The broker the execution_broker fallback recovered from the OTHER
    # variant must survive the merge, not be discarded because the
    # brokerless variant happened to be scanned first (Codex review: "Retain
    # known broker when merging brokerless state variants").
    assert records[0].broker == "robinhood"


def test_collect_purchase_records_no_id_row_merges_stream_and_run_id_across_variants(tmp_path):
    # A legacy run_state.json variant with no run_classification block at all
    # (only top-level "scheduled": true) is missing stream/run_id entirely,
    # while a sibling run_state_scheduled.json variant for the SAME run
    # supplies both. Both describe the identical physical run, so merging is
    # safe -- and the merge must not stop at broker; stream/run_id must
    # survive too rather than being discarded because the incomplete variant
    # happened to be scanned first (Codex review: "Preserve complete
    # provenance when equal-ranked states deduplicate").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    legacy_state = {
        "run_id": None,
        "generated_at": "2026-08-20T19:30:00Z",
        "trading_day": "2026-08-20",
        "scheduled": True,
    }
    full_state = {
        "run_id": "scheduled_20260820_1530ET",
        "generated_at": "2026-08-20T19:30:00Z",
        "trading_day": "2026-08-20",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
            "run_id": "scheduled_20260820_1530ET",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(legacy_state), encoding="utf-8")
    (run_dir / "run_state_scheduled.json").write_text(json.dumps(full_state), encoding="utf-8")
    # No order_id column -- the shape most exposed to this collision.
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].stream == "afternoon"
    assert records[0].run_id == "scheduled_20260820_1530ET"


def test_collect_purchase_records_broker_reconciliation_merges_same_run_provenance(tmp_path):
    # Two run_state*.json variants in the SAME directory reference the SAME
    # order_id -- one resolves a broker (via run_classification.execution_broker)
    # but has no stream/run_id, the other has stream/run_id but no broker at
    # all. Because they resolve DIFFERENT brokers ("robinhood" vs
    # "unknown-broker"), they land in different dedup-key namespaces and are
    # merged by the LATER broker-reconciliation pass, not the main
    # equal-rank tie-break -- that reconciliation pass must also merge
    # missing provenance fields for same-directory variants rather than
    # picking the brokered attribution wholesale (Codex review: "Merge
    # same-run provenance during broker reconciliation").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    brokered_state = {
        "run_id": None,
        "generated_at": "2026-09-02T19:30:00Z",
        "trading_day": "2026-09-02",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "execution_broker": "robinhood",
        },
    }
    provenance_state = {
        "run_id": "scheduled_20260902_1530ET",
        "generated_at": "2026-09-02T19:30:00Z",
        "trading_day": "2026-09-02",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
            "run_id": "scheduled_20260902_1530ET",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(brokered_state), encoding="utf-8")
    (run_dir / "run_state_scheduled.json").write_text(json.dumps(provenance_state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-recon-provenance-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].broker == "robinhood"
    assert records[0].stream == "afternoon"
    assert records[0].run_id == "scheduled_20260902_1530ET"


def test_collect_purchase_records_no_id_rows_scoped_to_distinct_shared_report_files(tmp_path):
    # Two DIFFERENT run_states can each reference a distinct custom-named CSV
    # living in the SAME shared full-report directory (the writer's own
    # layout explicitly allows this). Both files' first row for the same
    # ticker independently resolves to row_key "TICKER#0" with no order_id --
    # the no-order-id key must include the actual CSV file's identity, not
    # just its shared parent directory, or the second real purchase is merged
    # into the first (Codex review: "Include source files in brokerless
    # dedup keys").
    root = tmp_path / "cron_tracking"
    run_a = root / "afternoon_run_a"
    run_b = root / "afternoon_run_b"
    shared_reports = root / "shared_reports"
    run_a.mkdir(parents=True)
    run_b.mkdir(parents=True)
    shared_reports.mkdir(parents=True)

    def _state(run_id, orders_csv_name):
        return {
            "run_id": run_id,
            "generated_at": "2026-08-12T19:30:00Z",
            "trading_day": "2026-08-12",
            "run_classification": {
                "run_origin": "scheduled",
                "manual_run": False,
                "stream": "afternoon",
            },
            "artifacts": {"orders_csv_path": f"../shared_reports/{orders_csv_name}"},
        }

    (run_a / "run_state.json").write_text(
        json.dumps(_state("scheduled_20260812_1530ET_a", "dca_robinhood_report_orders.csv")),
        encoding="utf-8",
    )
    (run_b / "run_state.json").write_text(
        json.dumps(_state("scheduled_20260812_1530ET_b", "dca_alpaca_report_orders.csv")),
        encoding="utf-8",
    )
    (shared_reports / "dca_robinhood_report_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )
    (shared_reports / "dca_alpaca_report_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,9.00,filled,300.00,0.03\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 2
    amounts = sorted(r.amount_usd for r in records if r.amount_usd is not None)
    assert amounts == [pytest.approx(4.00), pytest.approx(9.00)]


def test_collect_purchase_records_does_not_resurrect_explicit_rejection_via_sibling(tmp_path):
    # A preferred filled_orders.csv whose rows all resolved to an explicit
    # no-fill outcome (state='rejected', nothing executed) is a confirmed
    # broker answer, not missing evidence. It must not be treated the same
    # as an unreadable/header-only file -- falling back to a sibling
    # orders.csv's stale status='filled' row would resurrect a purchase the
    # broker explicitly rejected (Codex review: "Respect explicit no-fill
    # outcomes before using the sibling CSV").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260813_1530ET",
        "generated_at": "2026-08-13T19:30:00Z",
        "trading_day": "2026-08-13",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-rejected-1,4.00,rejected,,\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-rejected-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_does_not_resurrect_status_rejected(tmp_path):
    # A preferred filled_orders.csv row expresses its final outcome ONLY in
    # `status` (no `state` column at all) -- status='rejected' is just as
    # authoritative as state='rejected' and must block the sibling fallback
    # the same way (Codex review: "Treat terminal status rows as
    # authoritative").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260827_1530ET",
        "generated_at": "2026-08-27T19:30:00Z",
        "trading_day": "2026-08-27",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-status-rejected-1,4.00,rejected,,\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-status-rejected-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_does_not_resurrect_status_rejected_with_blank_state(tmp_path):
    # A preferred filled_orders.csv row has a `state` COLUMN present (used
    # by other rows elsewhere in the file) but BLANK for this specific row,
    # while `status='rejected'` carries the real outcome. Checking only
    # whether the state column exists (not whether it actually has content)
    # missed this blank-cell shape and let a stale sibling status='filled'
    # row resurrect the rejection (Codex review: "Fall back to status when
    # state is blank").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260831_1530ET",
        "generated_at": "2026-08-31T19:30:00Z",
        "trading_day": "2026-08-31",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,status,state,average_fill_price,quantity\n"
        "CRM,order-blank-state-rejected-1,4.00,rejected,,,\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-blank-state-rejected-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_filters_rejected_row_the_parser_misclassified_as_partial(tmp_path):
    # A row has state blank but status='rejected', AND a positive `quantity`
    # column value. The SHARED near_close_dca._orders_csv_fills parser's own
    # state-blank fallback treats that positive quantity as executed
    # (state is present-but-blank, not absent, so its manual-filled-orders
    # heuristic doesn't apply) and returns it as a confirmed PARTIAL FILL --
    # without ever consulting status. That means `fills` is non-empty
    # straight out of the shared parser, bypassing the earlier
    # empty-fills-only rejection check entirely; this file's own
    # status='rejected' must still filter that misclassified row back out
    # (Codex review: "Check terminal status before accepting parsed fills").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260901_1530ET",
        "generated_at": "2026-09-01T19:30:00Z",
        "trading_day": "2026-09-01",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,status,state,average_fill_price,quantity\n"
        "CRM,order-parser-misclassified-1,4.00,rejected,,100.00,0.02\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-parser-misclassified-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_filters_rejected_row_a_sibling_candidate_misclassified_as_partial(
    tmp_path,
):
    # Same parser misclassification as the preferred-file test above --
    # state blank, status='rejected', positive `quantity` reads as a
    # confirmed partial fill -- but this time the misclassified row lives on
    # an INTERMEDIATE fallback candidate (orders.csv), not the preferred
    # file. The preferred filled_orders.csv is header-only, so the fallback
    # loop advances to orders.csv and returns its non-empty `sibling_fills`
    # as soon as it sees any row survive the (empty) rejected-id filter
    # carried over from the preferred file. That filter can't catch a
    # rejection THIS sibling is the first to report, so the misclassified
    # row must be checked against orders.csv's own status column before
    # being accepted (Codex review: "Validate sibling terminal outcomes
    # before accepting fills").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260902_1530ET",
        "generated_at": "2026-09-02T19:30:00Z",
        "trading_day": "2026-09-02",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,status,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,state,average_fill_price,quantity\n"
        "CRM,order-sibling-misclassified-1,4.00,rejected,,100.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_requires_executed_evidence_for_terminal_rows(tmp_path):
    # A 'canceled' row in orders.csv (the SUBMISSION-side artifact, where
    # `quantity` is what was requested, never a broker-confirmed fill)
    # carries no quantity_filled/cumulative_quantity column at all, only the
    # last-resort requested `quantity` alias, positive.
    # near_close_dca._has_executed_quantity falls back to that alias, so the
    # shared parser reports this as a confirmed partial fill. A canceled
    # order's REQUESTED quantity must not be published as executed capital
    # just because no genuine executed column contradicts it (Codex review:
    # "Require executed evidence for terminal rows"). No filled_orders.csv
    # exists, so this IS the file the default resolution reads.
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260903_1530ET",
        "generated_at": "2026-09-03T19:30:00Z",
        "trading_day": "2026-09-03",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-cancel-unconfirmed-1,4.00,canceled,100.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_preserves_recovery_partial_fill_recorded_in_quantity(tmp_path):
    # The recovery-artifact filled_orders.csv shape near_close_dca documents
    # (config/scheduled_tasks.json -> recovery_context_run_outcomes) records
    # a partially executed cancellation with `quantity` AS the confirmed
    # filled amount, carrying no separate quantity_filled/cumulative_quantity
    # column at all. Restricting the new terminal-row exclusion (above) to
    # orders.csv/submitted_orders.csv -- mirroring near_close_dca's own
    # is_manual_filled_orders special-casing of the filled_orders.csv
    # filename as the confirmed-fill record -- must leave this shape's real
    # partial fill in the ledger (Codex review: "Preserve recovery partial
    # fills recorded in quantity").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260904_1530ET",
        "generated_at": "2026-09-04T19:30:00Z",
        "trading_day": "2026-09-04",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-recovery-partial-1,4.00,canceled,100.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "CRM"
    assert records[0].order_id == "order-recovery-partial-1"
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_preserves_partial_fill_in_custom_confirmed_csv(tmp_path):
    # Same recovery shape as the test above -- a terminal `state=canceled`
    # row with `quantity` AS the confirmed filled amount and no separate
    # quantity_filled/cumulative_quantity column -- but recorded under a
    # RENAMED confirmed-fill/recovery CSV via artifacts.orders_csv_path
    # rather than the literal "filled_orders.csv". The prior restriction
    # excluded every filename OTHER than "filled_orders.csv" instead of
    # positively matching the submission-side names, so this custom
    # confirmed-fill filename was wrongly treated as submission-side and
    # its real partial fill was dropped (Codex review: "Preserve partial
    # fills in custom confirmed artifacts").
    root = tmp_path / "cron_tracking"
    run_dir = root / "scheduled_runs" / "20260907_1530ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_label": "dca_afternoon_20260907_153000",
        "scheduled": True,
        "trading_day": "2026-09-07",
        "artifacts": {"orders_csv_path": "dca_afternoon_20260907_153000_filled.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "dca_afternoon_20260907_153000_filled.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-custom-recovery-partial-1,4.00,canceled,100.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "CRM"
    assert records[0].order_id == "order-custom-recovery-partial-1"
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_filters_rejected_row_in_a_custom_named_csv(tmp_path):
    # A full-report run_state's artifacts.orders_csv_path can name a custom
    # CSV outside _SIBLING_CSV_CHAIN (e.g. "dca_<label>_<day>_<stamp>_orders.csv"),
    # which has no defined SIBLING fallback order. But the misclassification
    # this collector already filters for canonical filenames -- a blank
    # `state`, status='rejected' row whose positive `quantity` the shared
    # parser reads as a confirmed partial fill -- applies just as much to a
    # renamed submission artifact, and previously slipped through untouched
    # because the out-of-chain early return happened BEFORE the terminal-
    # outcome analysis ran at all (Codex review: "Analyze custom CSV
    # outcomes before returning").
    root = tmp_path / "cron_tracking"
    run_dir = root / "scheduled_runs" / "20260906_1530ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_label": "dca_afternoon_20260906_153000",
        "scheduled": True,
        "trading_day": "2026-09-06",
        "artifacts": {"orders_csv_path": "dca_afternoon_20260906_153000_orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "dca_afternoon_20260906_153000_orders.csv").write_text(
        "ticker,order_id,amount_usd,status,state,average_fill_price,quantity\n"
        "CRM,order-custom-csv-misclassified-1,4.00,rejected,,100.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_filters_rejected_row_despite_a_truncated_tail_row(tmp_path):
    # Same misclassification as the test above -- state blank,
    # status='rejected', positive `quantity` reads as a confirmed partial
    # fill -- but this file ALSO has an unrelated final row with fewer
    # fields than the header (the documented truncated-mid-write shape
    # _orders_csv_fills detects via skip_sink). An earlier revision gated
    # the terminal-outcome analysis on skip_sink staying unchanged, so the
    # truncated NVDA row disabled filtering for the COMPLETE, trustworthy
    # CRM row before it, letting the misclassified rejection through
    # unfiltered (Codex review: "Filter trusted rows despite a truncated
    # tail"). The analysis must run regardless and drop only the
    # untrustworthy tail row itself, not bail out of the whole file.
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260905_1530ET",
        "generated_at": "2026-09-05T19:30:00Z",
        "trading_day": "2026-09-05",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,status,state,average_fill_price,quantity\n"
        "CRM,order-truncated-tail-rejected-1,4.00,rejected,,100.00,0.02\n"
        "NVDA,filled\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-truncated-tail-rejected-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_requires_executed_evidence_for_in_flight_rows(tmp_path):
    # A market-open/intraday recovery whose ONLY evidence is
    # submitted_orders.csv (no filled_orders.csv/orders.csv exists at all --
    # these streams have no in-repo executor writing buy_candidates). The
    # row carries state='submitted' (still in flight, may fill later) and no
    # quantity_filled/cumulative_quantity column, only a positive requested
    # `quantity`. near_close_dca._has_executed_quantity falls back to that
    # alias exactly as it does for a terminal row, so the shared parser
    # reports this in-flight, unconfirmed order as a confirmed partial fill.
    # The order has only been PLACED, not filled -- publishing the requested
    # shares/notional as a purchase would be wrong even though it isn't yet
    # known to have failed either (Codex review: "Reject in-flight rows
    # during submitted fallback").
    root = tmp_path / "cron_tracking"
    run_dir = root / "market_open_dca" / "20260908_0935ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260908_0935ET",
        "generated_at": "2026-09-08T13:35:00Z",
        "trading_day": "2026-09-08",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "market_open",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NVDA,order-in-flight-1,7.00,submitted,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling" in w for w in warnings)


def test_collect_purchase_records_requires_executed_evidence_for_new_state_rows(tmp_path):
    # Same misclassification as the in-flight "submitted" test above, but
    # for state='new' -- one of dca_live_adapters._LIVE_ORDER_STATES's
    # broker-native live states that near_close_dca._UNRESOLVED_ORDER_
    # ATTEMPT_STATUSES ("submitted"/"pending"/"unconfirmed" only) does not
    # cover. A row with no quantity_filled/cumulative_quantity column but a
    # positive requested `quantity` must not be published as a purchase just
    # because this collector's in-flight check only recognized the canonical
    # submitted_orders.csv writer's own vocabulary and not the broker's
    # wider one (Codex review: "Cover every live broker state before
    # accepting quantity").
    root = tmp_path / "cron_tracking"
    run_dir = root / "market_open_dca" / "20260909_0935ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260909_0935ET",
        "generated_at": "2026-09-09T13:35:00Z",
        "trading_day": "2026-09-09",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "market_open",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NVDA,order-new-state-1,7.00,new,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling" in w for w in warnings)


def test_collect_purchase_records_excludes_confirmation_blocked_attempt_rows(tmp_path):
    # status='not_submitted_confirmation_required' (near_close_dca.
    # OrderConfirmationRequiredError / scripts.run_classification's
    # confirmation-required status) means the order was blocked pending a
    # separate pre-authorization step and NEVER reached the broker at all --
    # blank state, so `row_state` falls back to this status, and a positive
    # requested `quantity` reads as a confirmed partial fill from the shared
    # parser even though no order was ever placed (Codex review: "Exclude
    # confirmation-blocked attempt rows").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260910_1530ET",
        "generated_at": "2026-09-10T19:30:00Z",
        "trading_day": "2026-09-10",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-confirmation-blocked-1,4.00,not_submitted_confirmation_required,100.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling" in w for w in warnings)


def test_collect_purchase_records_filters_live_states_in_renamed_submission_artifacts(tmp_path):
    # Same misclassification as the canonical-filename "state=new" test
    # above, but recorded under a RENAMED submission snapshot via
    # artifacts.orders_csv_path (e.g. a full-report writer's
    # "broker_snapshot.csv") rather than the literal "submitted_orders.csv".
    # The live-state exclusion must not depend on the basename -- a row
    # still carrying an unresolved live state confirms nothing executed
    # regardless of what the file happens to be named (Codex review: "Filter
    # live states in renamed submission artifacts").
    root = tmp_path / "cron_tracking"
    run_dir = root / "scheduled_runs" / "20260911_1530ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_label": "dca_afternoon_20260911_153000",
        "scheduled": True,
        "trading_day": "2026-09-11",
        "artifacts": {"orders_csv_path": "broker_snapshot.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "broker_snapshot.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NVDA,order-renamed-live-1,7.00,new,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling" in w for w in warnings)


def test_collect_purchase_records_preserves_status_bearing_partial_fills(tmp_path):
    # test_near_close_dca.py's
    # test_orders_csv_fills_accepts_submitted_partial_executions establishes
    # status='submitted' + state='partially_filled' + a requested-quantity-
    # only row (no separate quantity_filled/cumulative_quantity column) as a
    # real, intentionally-accepted confirmed partial fill from the SHARED
    # parser -- regardless of filename or the row's ``status`` value. Two
    # earlier rounds in this collector tried excluding this shape (first via
    # a filename check, then via a ``not status`` row-level check); the
    # second one directly contradicted this documented/tested behavior and
    # silently dropped a real confirmed partial fill (Codex review:
    # "Preserve status-bearing partial fills in confirmed CSVs"). Recorded
    # under a renamed artifacts.orders_csv_path CSV specifically to prove
    # the filename plays no part in this decision either.
    root = tmp_path / "cron_tracking"
    run_dir = root / "scheduled_runs" / "20260912_1530ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_label": "dca_afternoon_20260912_153000",
        "scheduled": True,
        "trading_day": "2026-09-12",
        "artifacts": {"orders_csv_path": "broker_snapshot.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "broker_snapshot.csv").write_text(
        "ticker,order_id,amount_usd,status,state,average_fill_price,quantity\n"
        "NVDA,order-status-bearing-partial-1,7.00,submitted,partially_filled,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "NVDA"
    assert records[0].order_id == "order-status-bearing-partial-1"
    assert records[0].quantity == pytest.approx(0.058333)


def test_collect_purchase_records_retains_authoritative_prefix_after_csv_error(tmp_path):
    # csv.Error raised mid-iteration (e.g. a field exceeding
    # csv.field_size_limit()) while reading a LATER row must not discard the
    # COMPLETE, successfully-parsed rows already read before it --
    # near_close_dca._orders_csv_fills itself keeps that prefix rather than
    # failing the whole read. A blanket ``list(reader)`` around the entire
    # read discarded the trustworthy prefix wholesale, so an earlier
    # complete rejected/misclassified row went unanalyzed and unfiltered
    # purely because a later, unrelated row was malformed (Codex review:
    # "Retain authoritative prefix rows after CSV parser errors").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260913_1530ET",
        "generated_at": "2026-09-13T19:30:00Z",
        "trading_day": "2026-09-13",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    oversized_field = "x" * 200_000
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-csv-error-prefix-rejected-1,4.00,rejected,100.00,0.02\n"
        f'AAPL,order-csv-error-trailing,4.00,"unterminated {oversized_field}\n',
        encoding="utf-8",
    )

    original_limit = csv.field_size_limit()
    csv.field_size_limit(1024)
    try:
        records, warnings = collect_purchase_records(root)
    finally:
        csv.field_size_limit(original_limit)

    assert records == []
    assert not any("falling back to sibling" in w for w in warnings)


def test_collect_purchase_records_catches_csv_error_in_the_header_row(tmp_path):
    # csv.Error can be raised by ``reader.fieldnames`` itself -- e.g. a
    # HEADER field exceeding csv.field_size_limit() -- before the row loop
    # is even entered. That must be caught and treated as a soft read
    # failure (skip this file, keep scanning), not left to abort the whole
    # collect_purchase_records() run (Codex review: "Catch CSV errors while
    # reading the header").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260914_1530ET",
        "generated_at": "2026-09-14T19:30:00Z",
        "trading_day": "2026-09-14",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    oversized_header_field = "x" * 200_000
    (run_dir / "orders.csv").write_text(
        f'"{oversized_header_field}",order_id,amount_usd,status,average_fill_price,quantity\n'
        "CRM,order-header-csv-error-1,4.00,filled,100.00,0.02\n",
        encoding="utf-8",
    )

    original_limit = csv.field_size_limit()
    csv.field_size_limit(1024)
    try:
        records, _ = collect_purchase_records(root)
    finally:
        csv.field_size_limit(original_limit)

    assert records == []


def test_collect_purchase_records_treats_dropped_tail_as_non_authoritative(tmp_path):
    # A preferred filled_orders.csv has a complete rejected AAPL row
    # followed by a truncated MSFT tail row (fewer fields than the header).
    # Dropping that untrustworthy tail row must NOT leave the file
    # `all_authoritative` -- the dropped row's real outcome is unknown, so
    # the sibling orders.csv (with a confirmed MSFT fill) must still be
    # consulted rather than being blocked by treating the retained
    # rejection-only prefix as proof the whole file has no fills (Codex
    # review: "Treat a dropped tail as non-authoritative").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260915_1530ET",
        "generated_at": "2026-09-15T19:30:00Z",
        "trading_day": "2026-09-15",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "AAPL,order-dropped-tail-rejected-1,4.00,rejected,100.00,0.02\n"
        "MSFT,filled\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "MSFT,order-dropped-tail-confirmed-1,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "MSFT"
    assert records[0].order_id == "order-dropped-tail-confirmed-1"


def test_collect_purchase_records_does_not_resurrect_cancelled_with_explicit_zero(tmp_path):
    # A preferred filled_orders.csv row with state='cancelled' (British
    # spelling) and an EXPLICIT numeric zero in the executed-quantity column
    # is just as authoritative as 'rejected' -- must block the sibling
    # fallback (Codex review: "Recognize every terminal no-fill state before
    # falling back").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260818_1530ET",
        "generated_at": "2026-08-18T19:30:00Z",
        "trading_day": "2026-08-18",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity_filled\n"
        "CRM,order-cancelled-1,4.00,cancelled,,0\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-cancelled-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_retries_sibling_for_canceled_with_unknown_execution(tmp_path):
    # A preferred filled_orders.csv row with state='canceled' but NO
    # executed-quantity column at all says "unknown", not "confirmed nothing
    # executed" -- a cancelled order can genuinely have partially filled
    # first. Must still consult the sibling orders.csv rather than assuming
    # zero execution (Codex review: "Require explicit zero execution before
    # trusting cancellation").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260819_1530ET",
        "generated_at": "2026-08-19T19:30:00Z",
        "trading_day": "2026-08-19",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state\nCRM,order-canceled-unknown-1,4.00,canceled\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-canceled-unknown-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].order_id == "order-canceled-unknown-1"
    assert any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_retries_sibling_for_malformed_filled_row(tmp_path):
    # A preferred filled_orders.csv row that says state='filled' but carries
    # no usable price, quantity, or amount is NOT an explicit no-fill outcome
    # -- it is a data-quality problem, and the sibling orders.csv (which has
    # the real confirmed data) must still be tried (Codex review:
    # "Distinguish malformed rows from explicit no-fill outcomes").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260814_1530ET",
        "generated_at": "2026-08-14T19:30:00Z",
        "trading_day": "2026-08-14",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-malformed-1,,filled,,\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-malformed-1,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].order_id == "order-malformed-1"
    assert any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_no_id_rows_share_identity_across_explicit_and_implicit_resolution(tmp_path):
    # Two run_state*.json variants in ONE directory can read the SAME
    # physical default CSV while one explicitly names it in
    # artifacts.orders_csv_path and the other omits artifacts entirely,
    # relying on _orders_csv_fills's own implicit default resolution. Both
    # must resolve to the SAME fill_source_dir identity for a no-order-id
    # row, or the row is recorded twice (Codex review: "Normalize implicit
    # CSVs to their resolved file identity").
    root = tmp_path / "cron_tracking"
    run_dir = root / "midday_run"
    run_dir.mkdir(parents=True)
    explicit_state = {
        "run_id": "scheduled_20260815_1230ET",
        "generated_at": "2026-08-15T16:30:00Z",
        "trading_day": "2026-08-15",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "midday",
        },
        "artifacts": {"orders_csv_path": "filled_orders.csv"},
    }
    implicit_state = {
        "run_id": "scheduled_20260815_1230ET",
        "generated_at": "2026-08-15T16:30:00Z",
        "trading_day": "2026-08-15",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "midday",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(explicit_state), encoding="utf-8")
    (run_dir / "run_state_scheduled.json").write_text(json.dumps(implicit_state), encoding="utf-8")
    # No order_id column -- the shape most exposed to this collision.
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].amount_usd == pytest.approx(4.00)
    assert records[0].quantity == pytest.approx(0.02)


def test_collect_purchase_records_recovery_root_not_reread_after_sibling_fallback(tmp_path):
    # A nested recovery state records "../filled_orders.csv", which is
    # header-only; run_dir's own read falls back to the sibling
    # recovery_root/orders.csv and succeeds. The already_covered check that
    # decides whether to separately read recovery_root's fills must
    # recognize orders.csv as an equally valid match, not only
    # filled_orders.csv, or the recovery-root reader re-reads the same
    # sibling and records the same no-order-id row a second time (Codex
    # review: "Skip recovery-root reread after sibling fallback").
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260816"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260816_recovery",
        "generated_at": "2026-08-16T17:30:00Z",
        "trading_day": "2026-08-16",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
        "artifacts": {"orders_csv_path": "../filled_orders.csv"},
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (recovery_dir / "orders.csv").write_text(
        "ticker,amount_usd,status,average_fill_price,quantity\nCRM,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "CRM"
    assert records[0].amount_usd == pytest.approx(6.00)
    assert records[0].quantity == pytest.approx(0.02)


def test_collect_purchase_records_preserves_rejection_in_mixed_preferred_file(tmp_path):
    # filled_orders.csv mixes one authoritative rejected row with one
    # malformed (unparseable) row for a DIFFERENT order. The malformed row
    # legitimately triggers the sibling fallback, but the sibling's stale
    # status='filled' entry for the ALREADY-REJECTED order must not be
    # resurrected alongside the sibling's valid evidence for the malformed
    # one (Codex review: "Preserve explicit no-fill rows during sibling
    # fallback").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260817_1530ET",
        "generated_at": "2026-08-17T19:30:00Z",
        "trading_day": "2026-08-17",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-rejected-1,4.00,rejected,,\n"
        "AAPL,order-malformed-1,,filled,,\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-rejected-1,4.00,filled,200.00,0.02\n"
        "AAPL,order-malformed-1,9.00,filled,180.00,0.05\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    order_ids = {r.order_id for r in records}
    assert order_ids == {"order-malformed-1"}
    assert any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_orphan_sweep_skips_fallback_to_covered_sibling(tmp_path):
    # A run_state explicitly references an external orders.csv (covering
    # it), while an UNREFERENCED, header-only filled_orders.csv sits beside
    # it in the same external directory with no run_state of its own. The
    # orphan sweep discovers that filled_orders.csv (its own directory isn't
    # covered), and its internal sibling fallback resolves right back to the
    # ALREADY-covered orders.csv -- which must be recognized and skipped, not
    # re-read and re-recorded under the orphan's own source identity (Codex
    # review: "Skip orphan fallback targets that are already covered").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    external_dir = root / "external_reports"
    run_dir.mkdir(parents=True)
    external_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260821_1530ET",
        "generated_at": "2026-08-21T19:30:00Z",
        "trading_day": "2026-08-21",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
        "artifacts": {"orders_csv_path": "../external_reports/orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (external_dir / "orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )
    (external_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].amount_usd == pytest.approx(4.00)
    assert not any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_orphan_sweep_skips_implicit_default_already_covered(tmp_path):
    # A run_state explicitly references an external, VALID filled_orders.csv
    # (covering it, no fallback swap involved -- it reads fine on the first
    # try). That same external directory also has an unreferenced orders.csv.
    # The orphan sweep discovers that orders.csv, and since it's passed
    # orders_csv_file=None (relying on _orders_csv_fills's own implicit
    # default), the fallback helper resolves right back to the
    # ALREADY-covered filled_orders.csv -- which must be recognized even
    # though no explicit sibling-swap occurred (Codex review: "Resolve
    # implicit orphan inputs before checking covered files").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    external_dir = root / "external_reports"
    run_dir.mkdir(parents=True)
    external_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260822_1530ET",
        "generated_at": "2026-08-22T19:30:00Z",
        "trading_day": "2026-08-22",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
        "artifacts": {"orders_csv_path": "../external_reports/filled_orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (external_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )
    (external_dir / "orders.csv").write_text(
        "ticker,amount_usd,status,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].amount_usd == pytest.approx(4.00)
    assert not any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_recovery_reads_own_distinct_filled_orders(tmp_path):
    # A nested recovery state explicitly references "../orders.csv" while
    # the recovery root ALSO has its own DISTINCT filled_orders.csv with a
    # different confirmed fill. Both must be counted -- the already-covered
    # check must only skip when the recovery-root reader would land on the
    # SAME file run_dir's own read already consumed, not whenever run_dir
    # happened to touch any canonically-named file in that directory (Codex
    # review: "Read the other recovery CSV before marking the root
    # covered").
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260823"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260823_recovery",
        "generated_at": "2026-08-23T17:30:00Z",
        "trading_day": "2026-08-23",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
        "artifacts": {"orders_csv_path": "../orders.csv"},
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (recovery_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-recovery-orders-1,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AAPL,order-recovery-filled-1,9.00,filled,180.00,0.05\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    order_ids = {r.order_id for r in records}
    assert order_ids == {"order-recovery-orders-1", "order-recovery-filled-1"}


def test_collect_purchase_records_recovery_no_id_fills_keyed_by_resolved_file(tmp_path):
    # Two run_state*.json variants in one recovery directory each explicitly
    # reference a DIFFERENT canonical file ("../orders.csv" and
    # "../filled_orders.csv"), each with a DISTINCT no-order-id row. The
    # first variant's own recovery-block read (of the file the SECOND
    # variant's main read consumes directly) must key that fill by the
    # RESOLVED file, not the bare recovery_root directory, or the two
    # variants disagree on that row's dedup identity and it is recorded
    # twice (Codex review: "Key recovery fills by the resolved CSV file").
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260825"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)

    def _state(run_id, orders_csv_name):
        return {
            "run_id": run_id,
            "generated_at": "2026-08-25T17:30:00Z",
            "trading_day": "2026-08-25",
            "run_classification": {
                "run_origin": "manual",
                "manual_run": True,
                "stream": "midday",
            },
            "artifacts": {"orders_csv_path": f"../{orders_csv_name}"},
        }

    (artifacts_dir / "run_state.json").write_text(
        json.dumps(_state("manual_20260825_recovery_a", "orders.csv")), encoding="utf-8"
    )
    (artifacts_dir / "run_state_scheduled.json").write_text(
        json.dumps(_state("manual_20260825_recovery_b", "filled_orders.csv")), encoding="utf-8"
    )
    # No order_id column on either -- the shape most exposed to this collision.
    (recovery_dir / "orders.csv").write_text(
        "ticker,amount_usd,status,average_fill_price,quantity\nCRM,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nAAPL,9.00,filled,180.00,0.05\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    tickers = sorted(r.ticker for r in records)
    assert tickers == ["AAPL", "CRM"]


def test_collect_purchase_records_preserves_rejection_for_no_id_row_in_mixed_file(tmp_path):
    # filled_orders.csv mixes a no-order-id explicit rejection with a
    # DIFFERENT, malformed no-order-id row that legitimately triggers the
    # sibling fallback. Without a stable order_id to filter by, the rejected
    # row's ticker must still block a same-ticker sibling row from being
    # resurrected (Codex review: "Preserve terminal rows that lack order
    # IDs").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260824_1530ET",
        "generated_at": "2026-08-24T19:30:00Z",
        "trading_day": "2026-08-24",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\n"
        "CRM,4.00,rejected,,\n"
        "AAPL,,filled,,\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,amount_usd,status,average_fill_price,quantity\n"
        "CRM,4.00,filled,200.00,0.02\n"
        "AAPL,9.00,filled,180.00,0.05\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    tickers = {r.ticker for r in records}
    assert tickers == {"AAPL"}
    assert any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_covers_preferred_external_csv_after_fallback(tmp_path):
    # A full-report run_state can record artifacts.orders_csv_path pointing
    # at a CSV in a DIFFERENT directory than run_dir. When that preferred
    # filled_orders.csv is header-only and the sibling-fallback swaps in its
    # sibling orders.csv, the ORIGINAL preferred file must still be marked
    # covered -- otherwise the orphan sweep later rediscovers it under the
    # external directory, reruns the same fallback, and records the same
    # no-order-id row a second time under a different source identity
    # (Codex review: "Mark the preferred external CSV covered after
    # fallback").
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    external_dir = root / "external_reports"
    run_dir.mkdir(parents=True)
    external_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260811_1530ET",
        "generated_at": "2026-08-11T19:30:00Z",
        "trading_day": "2026-08-11",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
        "artifacts": {"orders_csv_path": "../external_reports/filled_orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (external_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (external_dir / "orders.csv").write_text(
        "ticker,amount_usd,status,average_fill_price,quantity\nCRM,4.00,filled,200.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].amount_usd == pytest.approx(4.00)
    assert records[0].quantity == pytest.approx(0.02)
    assert not any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_dedup_prefers_resolved_price(tmp_path):
    # Two completed snapshots for the same order with the SAME resolved
    # quantity/amount, but only one has a settled avg_fill_price (e.g. a
    # later reconciled snapshot). The priced one must win regardless of
    # directory scan order.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "a_price_less_copy",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery",
        stream="afternoon",
        run_origin="manual",
        # amount + quantity known, but no price column at all.
        filled_orders_csv=("ticker,order_id,amount_usd,quantity\nAMD,order-60,5.00,0.05\n"),
    )
    _write_run(
        root,
        "b_priced_copy",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=(
            "ticker,order_id,state,average_fill_price,quantity\n"
            "AMD,order-60,filled,100.00,0.05\n"
        ),
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].avg_fill_price == pytest.approx(100.00)
    assert records[0].quantity == pytest.approx(0.05)


def test_collect_purchase_records_suppresses_same_directory_duplicate_warning(tmp_path):
    # A directory can carry more than one run_state*.json (e.g. run_state.json
    # AND run_state_scheduled.json) that both read the SAME underlying CSV --
    # not a real cross-directory duplicate, just this scan visiting the same
    # evidence twice. That must not produce a spurious warning.
    root = tmp_path / "cron_tracking"
    run_dir = root / "run1"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260805_1530ET",
        "generated_at": "2026-08-05T19:30:00-04:00",
        "trading_day": "2026-08-05",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "run_state_scheduled.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NFLX,order-55,5.00,filled,500.00,0.01\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert not any("duplicate order_id" in w for w in warnings)


def test_build_ticker_rollup_rounds_total_quantity(tmp_path):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-04",
        run_id="scheduled_20260804_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "SPY,order-1,10.00,filled,100.00,0.1\n"
        ),
    )
    _write_run(
        root,
        "run2",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "SPY,order-2,20.00,filled,100.00,0.2\n"
        ),
    )

    records, _ = collect_purchase_records(root)
    rollup = build_ticker_rollup(records)

    # 0.1 + 0.2 == 0.30000000000000004 in raw float arithmetic.
    assert rollup["SPY"]["total_quantity"] == 0.3


def test_collect_purchase_records_accepts_string_paths(tmp_path):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,amount_usd,state,average_fill_price,quantity\nBA,3.00,filled,200.00,0.015\n"
        ),
    )

    records, _ = collect_purchase_records(str(root))

    assert len(records) == 1
    assert records[0].ticker == "BA"


def test_collect_purchase_records_root_not_a_directory_returns_nothing(tmp_path):
    not_a_dir = tmp_path / "not_a_dir.txt"
    not_a_dir.write_text("hello", encoding="utf-8")

    records, warnings = collect_purchase_records(not_a_dir)

    assert records == []
    assert warnings == []


def test_collect_purchase_records_skips_unreadable_run_state(tmp_path):
    root = tmp_path / "cron_tracking"
    bad_dir = root / "corrupt_run"
    bad_dir.mkdir(parents=True)
    (bad_dir / "run_state.json").write_text("{not json", encoding="utf-8")

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert any("unreadable or malformed" in w for w in warnings)


def test_collect_purchase_records_empty_root_returns_nothing(tmp_path):
    records, warnings = collect_purchase_records(tmp_path / "does_not_exist")
    assert records == []
    assert warnings == []


def test_build_ticker_rollup_aggregates_across_records(tmp_path):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-04",
        run_id="scheduled_20260804_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "AMD,order-1,4.00,filled,150.00,0.026667\n"
        ),
    )
    _write_run(
        root,
        "run2",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "AMD,order-2,6.00,filled,160.00,0.0375\n"
        ),
    )

    records, _ = collect_purchase_records(root)
    rollup = build_ticker_rollup(records)

    assert rollup["AMD"]["buy_count"] == 2
    assert rollup["AMD"]["total_invested_usd"] == pytest.approx(10.00)
    assert rollup["AMD"]["total_quantity"] == pytest.approx(0.064167)
    assert rollup["AMD"]["first_buy_date"] == "2026-08-04"
    assert rollup["AMD"]["last_buy_date"] == "2026-08-05"
    assert rollup["AMD"]["average_cost_basis_usd"] == pytest.approx(10.00 / 0.064167, rel=1e-4)


def test_build_ticker_rollup_tolerates_amount_only_fill(tmp_path):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        # No price/quantity columns at all -- amount-only confirmed fill.
        filled_orders_csv=("ticker,amount_usd\nAMZN,4.25\n"),
    )

    records, _ = collect_purchase_records(root)
    assert len(records) == 1
    assert records[0].avg_fill_price is None
    assert records[0].quantity is None
    assert records[0].amount_usd == pytest.approx(4.25)

    rollup = build_ticker_rollup(records)
    assert rollup["AMZN"]["total_quantity"] == 0.0
    assert rollup["AMZN"]["total_invested_usd"] == pytest.approx(4.25)
    assert rollup["AMZN"]["average_cost_basis_usd"] is None


def test_collect_purchase_records_scans_archived_run_state_filenames(tmp_path):
    # run_state_scheduled.json / run_state_partial.json are documented
    # sibling shapes (run_state*.json is the glob every other scanner in the
    # repo uses); an exact "run_state.json" name misses them entirely.
    root = tmp_path / "cron_tracking"
    run_dir = root / "archived_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260805_1530ET",
        "generated_at": "2026-08-05T19:30:00Z",
        "trading_day": "2026-08-05",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state_scheduled.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nTSLA,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "TSLA"


def test_collect_purchase_records_falls_back_when_recorded_path_is_stale(tmp_path):
    # artifacts.orders_csv_path can go stale (artifacts relocated/archived
    # after the state was written); the ledger must still find the real
    # sibling CSV rather than silently reporting a zero-fill run.
    root = tmp_path / "cron_tracking"
    run_dir = root / "full_report_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260805_1530ET",
        "generated_at": "2026-08-05T19:30:00Z",
        "trading_day": "2026-08-05",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
        "artifacts": {"orders_csv_path": "no_longer_here_orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nGOOG,8.00,filled,140.00,0.057143\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "GOOG"
    assert any("no longer exists" in w for w in warnings)


def test_collect_purchase_records_prefers_classification_run_id(tmp_path):
    root = tmp_path / "cron_tracking"
    run_dir = root / "run1"
    run_dir.mkdir(parents=True)
    state = {
        # The top-level run_id is the artifact BUILD id; run_classification.run_id
        # is the origin-scoped identifier reporting treats as authoritative.
        "run_id": "artifact-build-id-xyz",
        "generated_at": "2026-08-05T19:30:00Z",
        "trading_day": "2026-08-05",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "run_id": "scheduled_20260805_1530ET",
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nMETA,3.00,filled,300.00,0.01\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].run_id == "scheduled_20260805_1530ET"


def test_collect_purchase_records_prefers_completed_fill_data_over_stale_partial(tmp_path):
    # Same order_id: a stale PARTIAL snapshot beside the scheduled run that
    # placed it, and a later COMPLETED snapshot in a manual/recovery copy.
    # Attribution should credit the scheduled run; the fill numbers must come
    # from the completed snapshot, not the stale partial.
    root = tmp_path / "cron_tracking"
    # Sorts BEFORE "scheduled_run" so scan order feeds the completed copy first,
    # to prove the outcome doesn't depend on which one is seen first.
    _write_run(
        root,
        "a_manual_completed_copy",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=(
            "ticker,order_id,state,average_fill_price,quantity\n"
            "AMD,order-50,filled,100.00,0.05\n"
        ),
    )
    _write_run(
        root,
        "scheduled_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,state,average_fill_price,quantity\n"
            "AMD,order-50,partially_filled,100.00,0.01\n"
        ),
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    record = records[0]
    assert record.run_origin == "scheduled"
    assert record.run_id == "scheduled_20260805_1530ET"
    assert record.quantity == pytest.approx(0.05)
    assert record.amount_usd == pytest.approx(5.00)
    assert any("duplicate order_id" in w for w in warnings)


def test_build_ticker_rollup_excludes_amount_only_fill_from_average_cost_basis(tmp_path):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-04",
        run_id="scheduled_20260804_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "AMZN,order-1,4.00,filled,200.00,0.02\n"
        ),
    )
    _write_run(
        root,
        "run2",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        # Amount-only confirmed fill for the SAME ticker: no price/quantity.
        filled_orders_csv=("ticker,order_id,amount_usd\nAMZN,order-2,100.00\n"),
    )

    records, _ = collect_purchase_records(root)
    rollup = build_ticker_rollup(records)

    # The $100 amount-only fill must not inflate the average cost basis: it
    # adds to total_invested_usd (a true reporting figure) but must be
    # excluded from the average-cost-basis numerator since no matching share
    # count exists for it.
    assert rollup["AMZN"]["total_invested_usd"] == pytest.approx(104.00)
    assert rollup["AMZN"]["total_quantity"] == pytest.approx(0.02)
    assert rollup["AMZN"]["average_cost_basis_usd"] == pytest.approx(200.00)


def test_write_purchase_history_writes_csv_and_json(tmp_path):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "MSFT,order-9,5.00,filled,420.00,0.011905\n"
        ),
    )
    records, _ = collect_purchase_records(root)
    rollup = build_ticker_rollup(records)

    out_dir = tmp_path / "out"
    csv_path, json_path = write_purchase_history(records, rollup, output_dir=out_dir)

    assert csv_path.exists()
    assert json_path.exists()
    csv_text = csv_path.read_text(encoding="utf-8")
    assert "MSFT" in csv_text
    assert "order-9" in csv_text
    rollup_data = json.loads(json_path.read_text(encoding="utf-8"))
    assert rollup_data["MSFT"]["buy_count"] == 1


def test_main_end_to_end(tmp_path, capsys):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "IBM,order-7,2.00,filled,250.00,0.008\n"
        ),
    )
    out_dir = tmp_path / "out"

    exit_code = main(["--cron-tracking-root", str(root), "--output-dir", str(out_dir)])

    assert exit_code == 0
    assert (out_dir / "purchase_history.csv").exists()
    assert (out_dir / "purchase_history_by_ticker.json").exists()
    captured = capsys.readouterr()
    assert "Wrote 1 purchase record(s)" in captured.out


def test_main_refuses_to_publish_when_root_missing(tmp_path, capsys):
    missing_root = tmp_path / "does_not_exist"
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    # Simulate a previously-published good ledger that must survive untouched.
    existing_csv = out_dir / "purchase_history.csv"
    existing_csv.write_text("previous good ledger\n", encoding="utf-8")

    exit_code = main(["--cron-tracking-root", str(missing_root), "--output-dir", str(out_dir)])

    assert exit_code != 0
    assert existing_csv.read_text(encoding="utf-8") == "previous good ledger\n"
    captured = capsys.readouterr()
    assert "does not exist" in captured.err


def test_main_refuses_to_publish_when_additional_root_missing(tmp_path, capsys):
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,amount_usd,state,average_fill_price,quantity\nIBM,2.00,filled,250.00,0.008\n"
        ),
    )
    missing_additional = tmp_path / "relocated_root_missing"
    out_dir = tmp_path / "out"

    exit_code = main(
        [
            "--cron-tracking-root",
            str(root),
            "--output-dir",
            str(out_dir),
            "--additional-root",
            str(missing_additional),
        ]
    )

    assert exit_code != 0
    assert not (out_dir / "purchase_history.csv").exists()


def test_collect_purchase_records_reports_broker_and_scopes_dedup_to_it(tmp_path):
    # A broker's order_id is only guaranteed unique WITHIN that broker; two
    # different brokers (near_close_dca.AlpacaBroker vs RobinhoodBroker) could
    # each independently generate the same order_id string. A dedup key that
    # ignores broker would collapse two genuinely distinct fills into one row.
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-shared,3.50,filled,109.99,0.031821\n"
    )
    _write_run(
        root,
        "robinhood_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET_rh",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
        broker="robinhood",
    )
    _write_run(
        root,
        "alpaca_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET_alpaca",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
        broker="alpaca",
    )

    records, warnings = collect_purchase_records(root)

    assert warnings == []
    assert len(records) == 2
    brokers = {r.broker for r in records}
    assert brokers == {"robinhood", "alpaca"}


def test_collect_purchase_records_dedupes_same_broker_across_run_dirs(tmp_path):
    # Sanity check for the other direction: the SAME broker on both sides
    # must still dedup exactly as before broker was added to the key.
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-same-broker,3.50,filled,109.99,0.031821\n"
    )
    _write_run(
        root,
        "original",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
        broker="robinhood",
    )
    _write_run(
        root,
        "manual_reruns/recovery_copy",
        trading_day="2026-08-05",
        run_id="manual_20260805_recovery",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=csv_body,
        broker="robinhood",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].broker == "robinhood"
    assert any("duplicate order_id" in w for w in warnings)


def test_collect_purchase_records_broker_falls_back_to_none_for_legacy_shapes(tmp_path):
    # The default _write_run fixture writes "broker": {} (an older stub
    # shape, not a string) -- a non-string/missing broker must resolve to
    # None, not raise or fabricate a broker name.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,order-1,3.50,filled,109.99,0.031821\n"
        ),
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].broker is None


def test_collect_purchase_records_broker_falls_back_to_run_classification(tmp_path):
    # MorningDCAExecutor/midday run states store the definitive broker as
    # run_classification.execution_broker and add no top-level "broker" field
    # at all -- without a fallback to it, a confirmed fill from one of these
    # streams is recorded as broker=None, landing its order_id in the
    # "unknown" namespace despite the classification already knowing it's
    # Robinhood.
    root = tmp_path / "cron_tracking"
    run_dir = root / "midday_dca" / "20260806_1230ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260806_1230ET",
        "generated_at": "2026-08-06T16:30:00Z",
        "trading_day": "2026-08-06",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "midday",
            "execution_broker": "robinhood",
        },
        # No top-level "broker" field -- the normal MorningDCAExecutor/midday
        # run-state shape.
        "budget": {},
        "counts": {},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-mid-1,3.50,filled,109.99,0.031821\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].broker == "robinhood"


def test_collect_purchase_records_top_level_broker_wins_over_classification(tmp_path):
    # A real top-level "broker" string (near_close_dca's own state shape) must
    # still win over run_classification.execution_broker -- the classification
    # field is only a fallback for a state that never records one.
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260806_1530ET",
        "generated_at": "2026-08-06T19:30:00Z",
        "trading_day": "2026-08-06",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
            "execution_broker": "alpaca",
        },
        "broker": "robinhood",
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-top-1,3.50,filled,109.99,0.031821\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].broker == "robinhood"


def test_collect_purchase_records_retries_sibling_orders_csv_when_filled_orders_empty(tmp_path):
    # filled_orders.csv exists (so the default resolution prefers it) but is
    # header-only -- no confirmed rows -- while a sibling orders.csv carries a
    # real confirmed status=filled row. A single default-parser call never
    # looks at orders.csv once filled_orders.csv is chosen; this must retry.
    root = tmp_path / "cron_tracking"
    run_dir = root / "afternoon_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260806_1530ET",
        "generated_at": "2026-08-06T19:30:00Z",
        "trading_day": "2026-08-06",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "afternoon",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "SPCX,order-sib-1,3.50,filled,109.99,0.031821\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "SPCX"
    assert records[0].order_id == "order-sib-1"
    assert any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_falls_back_to_submitted_orders_when_filled_unusable(tmp_path):
    # A market-open/intraday-style directory: filled_orders.csv exists but
    # is header-only/unusable, there's no orders.csv sibling at all, but a
    # submitted_orders.csv carries a confirmed broker-native state=filled
    # row. filled_orders.csv merely EXISTING (even though useless) must not
    # block trying submitted_orders.csv the way it previously blocked the
    # bare_submitted default-resolution fallback too (Codex review: "Fall
    # back to confirmed submitted-order evidence").
    root = tmp_path / "cron_tracking"
    run_dir = root / "market_open_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260826_0935ET",
        "generated_at": "2026-08-26T13:35:00Z",
        "trading_day": "2026-08-26",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "market_open",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-submitted-1,3.50,filled,109.99,0.031821\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "SPCX"
    assert records[0].order_id == "order-submitted-1"
    assert any("falling back to sibling submitted_orders.csv" in w for w in warnings)


def test_collect_purchase_records_falls_back_to_submitted_orders_when_orders_csv_unusable(tmp_path):
    # No filled_orders.csv exists at all, so the default resolution's
    # preferred file IS orders.csv -- which is header-only/unusable -- while
    # submitted_orders.csv carries a confirmed broker-native state=filled
    # row. The sibling search previously only ran when the preferred name
    # was EXACTLY "filled_orders.csv", so this case (preferred=orders.csv)
    # never got a chance to try submitted_orders.csv at all (Codex review:
    # "Retry submitted evidence after an unusable orders CSV").
    root = tmp_path / "cron_tracking"
    run_dir = root / "market_open_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260828_0935ET",
        "generated_at": "2026-08-28T13:35:00Z",
        "trading_day": "2026-08-28",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "market_open",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-submitted-2,3.50,filled,109.99,0.031821\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "SPCX"
    assert records[0].order_id == "order-submitted-2"
    assert any("falling back to sibling submitted_orders.csv" in w for w in warnings)


def test_collect_purchase_records_stops_chain_at_authoritative_intermediate_rejection(tmp_path):
    # filled_orders.csv is unusable (header-only), orders.csv records an
    # EXPLICIT terminal rejection for the order, and submitted_orders.csv
    # still has an older stale filled snapshot for that same order. The
    # chain must stop at orders.csv's authoritative rejection rather than
    # treating its empty result the same as "no evidence" and continuing on
    # to resurrect the stale submitted_orders.csv row (Codex review: "Stop
    # fallback at an authoritative intermediate outcome").
    root = tmp_path / "cron_tracking"
    run_dir = root / "market_open_run"
    run_dir.mkdir(parents=True)
    state = {
        "run_id": "scheduled_20260830_0935ET",
        "generated_at": "2026-08-30T13:35:00Z",
        "trading_day": "2026-08-30",
        "run_classification": {
            "run_origin": "scheduled",
            "manual_run": False,
            "stream": "market_open",
        },
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (run_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "SPCX,order-chain-rejected-1,3.50,rejected,,\n",
        encoding="utf-8",
    )
    (run_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,submitted_amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-chain-rejected-1,3.50,filled,109.99,0.031821\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("falling back to sibling submitted_orders.csv" in w for w in warnings)


def test_collect_purchase_records_does_not_retry_sibling_when_filled_orders_has_fills(tmp_path):
    # The sibling-retry fallback must only fire when the preferred file
    # yields NO usable evidence -- a filled_orders.csv that legitimately has
    # confirmed fills must never also pull in orders.csv's (possibly stale,
    # merely-submitted) rows.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "afternoon_run",
        trading_day="2026-08-06",
        run_id="scheduled_20260806_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,order-real-1,3.50,filled,109.99,0.031821\n"
        ),
    )
    (root / "afternoon_run" / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "SPCX,order-stale-1,7.00,submitted,,\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].order_id == "order-real-1"
    assert not any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_retries_sibling_orders_csv_in_recovery_root(tmp_path):
    # The sibling-orders.csv retry must also apply to the enclosing recovery
    # root, not only the run_dir the run_state.json lives in -- a recovery
    # root's own filled_orders.csv can be header-only while its sibling
    # orders.csv carries the real confirmed rows (Codex review: "Apply the
    # sibling fallback to every fill reader").
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260807"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260807_recovery",
        "generated_at": "2026-08-07T17:30:00Z",
        "trading_day": "2026-08-07",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (recovery_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (recovery_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "CRM,order-recovery-sib-1,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "CRM"
    assert records[0].order_id == "order-recovery-sib-1"
    assert any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_retries_sibling_orders_csv_for_orphan_artifact(tmp_path):
    # The sibling-orders.csv retry must also apply to the orphan sweep (a
    # directory with no run_state*.json at all) -- a header-only
    # filled_orders.csv there must not shadow a valid sibling orders.csv
    # (Codex review: "Apply the sibling fallback to every fill reader").
    root = tmp_path / "cron_tracking"
    orphan_dir = root / "final_manual_review" / "20260808_manual_fix"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n",
        encoding="utf-8",
    )
    (orphan_dir / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status,average_fill_price,quantity\n"
        "AMD,order-orphan-sib-1,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "AMD"
    assert records[0].order_id == "order-orphan-sib-1"
    assert records[0].run_origin is None
    assert any("falling back to sibling orders.csv" in w for w in warnings)


def test_collect_purchase_records_includes_orphan_csv_with_no_run_state(tmp_path):
    # A repair/reconciliation pass (or a run interrupted before its own
    # run_state.json was written) can leave a confirmed filled_orders.csv
    # with no run_state*.json anywhere in its directory. Without the orphan
    # sweep this evidence is invisible to every run_state-keyed branch above.
    root = tmp_path / "cron_tracking"
    orphan_dir = root / "final_manual_review" / "20260805_manual_fix"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-orphan-1,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "AMD"
    assert records[0].order_id == "order-orphan-1"
    assert records[0].run_origin is None
    assert records[0].stream is None
    assert records[0].run_id is None
    assert records[0].broker is None
    # The trading day is recovered from the dated directory name.
    assert records[0].trading_day == "2026-08-05"
    assert any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_orphan_sweep_skips_covered_directories(tmp_path):
    # A directory that DOES have a successfully-parsed run_state*.json must
    # never be re-processed by the orphan sweep -- that would double-count
    # every fill the main scan already recorded.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "afternoon_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,order-1,3.50,filled,109.99,0.031821\n"
        ),
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].run_origin == "scheduled"
    assert not any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_orphan_sweep_skips_malformed_run_state_covered_by_main_dedup(tmp_path):
    # A directory whose run_state*.json exists but fails to parse is reported
    # as a warning by the main scan and is STILL eligible for the orphan
    # sweep (mirrors generate_credit_usage_proxy_report's own "present AND
    # parseable" skip semantics) -- a persisted CSV must not be dropped just
    # because its sibling state file is corrupt.
    root = tmp_path / "cron_tracking"
    run_dir = root / "final_manual_review" / "20260805_corrupt"
    run_dir.mkdir(parents=True)
    (run_dir / "run_state.json").write_text("{not valid json", encoding="utf-8")
    (run_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-orphan-2,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert any("unreadable or malformed" in w for w in warnings)
    assert len(records) == 1
    assert records[0].ticker == "AMD"
    assert records[0].order_id == "order-orphan-2"
    assert any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_orphan_sweep_follows_bare_submitted_orders_csv(tmp_path):
    # An orphan directory with ONLY submitted_orders.csv (no filled_orders.csv
    # / orders.csv) must still resolve fills -- _orders_csv_fills's own
    # default sibling search never looks at submitted_orders.csv, so the
    # orphan sweep must apply the same bare_submitted fallback the main scan
    # already applies.
    root = tmp_path / "cron_tracking"
    orphan_dir = root / "morning_dca" / "manual_recovery" / "20260805_fix"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NVDA,order-orphan-3,7.00,filled,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "NVDA"
    assert records[0].order_id == "order-orphan-3"
    assert any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_orphan_sweep_dedups_across_directories(tmp_path):
    # Two orphan directories reporting the SAME order_id (a duplicate copy of
    # the same repair artifact) must still dedup against each other, exactly
    # like two run_state-keyed directories would.
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-orphan-dup,4.00,filled,100.00,0.04\n"
    )
    (root / "final_manual_review" / "20260805_copy_a").mkdir(parents=True)
    (root / "final_manual_review" / "20260805_copy_a" / "filled_orders.csv").write_text(
        csv_body, encoding="utf-8"
    )
    (root / "final_manual_review" / "20260805_copy_b").mkdir(parents=True)
    (root / "final_manual_review" / "20260805_copy_b" / "filled_orders.csv").write_text(
        csv_body, encoding="utf-8"
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].order_id == "order-orphan-dup"
    assert any("duplicate order_id" in w for w in warnings)


def test_collect_purchase_records_orphan_sweep_skips_directories_with_no_fills(tmp_path):
    # An orphan CSV that resolves to zero confirmed fills (e.g. every row is
    # still status=submitted, never confirmed) must not add a spurious
    # warning or an empty-provenance no-op.
    root = tmp_path / "cron_tracking"
    orphan_dir = root / "final_manual_review" / "20260805_unconfirmed"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-unconfirmed,4.00,submitted,,\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert records == []
    assert not any("orphan order artifact" in w for w in warnings)


def test_infer_trading_day_from_path_handles_dashed_and_compact_and_absent():
    from purchase_history import _infer_trading_day_from_path

    assert _infer_trading_day_from_path(Path("/root/2026-08-05/run")) == "2026-08-05"
    assert _infer_trading_day_from_path(Path("/root/20260805_1530ET/run")) == "2026-08-05"
    assert _infer_trading_day_from_path(Path("/root/manual_runs/run")) is None
    # Digits that don't form a real calendar date must not be fabricated.
    assert _infer_trading_day_from_path(Path("/root/2026-13-99/run")) is None


def test_infer_trading_day_from_path_prefers_leaf_over_outer_backup_date():
    from purchase_history import _infer_trading_day_from_path

    # The run's own directory name (2026-07-01, compact) is the closest,
    # most specific evidence of when it actually ran -- the outer backup
    # date (2026-08-10) must not win just because it appears earlier in the
    # path string.
    path = Path("/backups/2026-08-10/cron_tracking/manual/20260701_fix")
    assert _infer_trading_day_from_path(path) == "2026-07-01"


def test_collect_purchase_records_reconciles_orphan_with_unique_known_broker(tmp_path):
    # An orphan copy (no run_state, so broker=None) of an order ALSO seen
    # from a normal, broker-attributed run must reconcile into ONE row, not
    # survive as a second "unknown-broker" row that double-counts quantity/
    # invested dollars/buy_count for the same real order.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "robinhood_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "SPCX,order-shared-2,3.50,filled,109.99,0.031821\n"
        ),
        broker="robinhood",
    )
    orphan_dir = root / "final_manual_review" / "20260805_repair_copy"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-shared-2,3.50,filled,109.99,0.031821\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    matching = [r for r in records if r.order_id == "order-shared-2"]
    assert len(matching) == 1
    assert matching[0].broker == "robinhood"
    assert matching[0].run_origin == "scheduled"


def test_collect_purchase_records_keeps_ambiguous_multi_broker_orphan_separate(tmp_path):
    # If the SAME order_id string legitimately exists under two DIFFERENT
    # known brokers, an orphan copy of it cannot be unambiguously reconciled
    # into either -- it must be kept as its own row, with a warning calling
    # out the ambiguity, rather than guessing.
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-ambiguous,3.50,filled,109.99,0.031821\n"
    )
    _write_run(
        root,
        "robinhood_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET_rh",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
        broker="robinhood",
    )
    _write_run(
        root,
        "alpaca_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET_alpaca",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
        broker="alpaca",
    )
    orphan_dir = root / "final_manual_review" / "20260805_ambiguous_copy"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "filled_orders.csv").write_text(csv_body, encoding="utf-8")

    records, warnings = collect_purchase_records(root)

    matching = [r for r in records if r.order_id == "order-ambiguous"]
    assert len(matching) == 3
    assert {r.broker for r in matching} == {"robinhood", "alpaca", None}
    assert any("matches known broker orders under more than one broker" in w for w in warnings)


def test_collect_purchase_records_discovers_orphan_full_report_csv(tmp_path):
    # run_and_report's full-report writer names its confirmed-fill CSV
    # dca_<label>_<day>_<stamp>_orders.csv, via generate_slippage_report,
    # which runs strictly BEFORE write_run_state persists run_state.json --
    # an interruption between the two leaves exactly this file with no
    # run_state.json at all, and the fixed filled/orders/submitted name
    # whitelist alone never discovers it.
    root = tmp_path / "cron_tracking"
    orphan_dir = root / "scheduled_runs" / "20260805_1530ET"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "dca_daily_2026-08-05_20260805-153000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-full-report,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "AMD"
    assert records[0].order_id == "order-full-report"
    assert any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_reports_orphan_csv_parse_failures(tmp_path):
    # An unreadable/malformed orphan CSV must surface a warning (via
    # skip_sink), not fail soft to an empty mapping with no signal at all.
    root = tmp_path / "cron_tracking"
    orphan_dir = root / "final_manual_review" / "20260805_truncated"
    orphan_dir.mkdir(parents=True)
    # A final row with fewer fields than the header is the documented
    # truncated-mid-write shape _orders_csv_fills detects via skip_sink.
    (orphan_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-trunc-1,4.00,filled,100.00,0.04\n"
        "NVDA,filled\n",
        encoding="utf-8",
    )

    _, warnings = collect_purchase_records(root)

    assert any("failed to parse orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_reads_submitted_only_recovery_root(tmp_path):
    # A supported market-open/intraday recovery can store confirmed evidence
    # at the enclosing recovery root ONLY as submitted_orders.csv (broker
    # state=filled) -- run_dir's own read already falls back to this bare
    # sibling shape, and the recovery-root read must apply the identical
    # fallback rather than silently returning no fills.
    root = tmp_path / "cron_tracking"
    recovery_dir = root / "midday_recovery_20260809"
    artifacts_dir = recovery_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    state = {
        "run_id": "manual_20260809_recovery",
        "generated_at": "2026-08-09T17:30:00Z",
        "trading_day": "2026-08-09",
        "run_classification": {
            "run_origin": "manual",
            "manual_run": True,
            "stream": "midday",
        },
    }
    (artifacts_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (recovery_dir / "submitted_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "CRM,order-submitted-recovery,6.00,filled,300.00,0.02\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "CRM"
    assert records[0].order_id == "order-submitted-recovery"


def test_purchase_history_runs_as_direct_script_without_module_flag():
    # Direct invocation (python scripts/purchase_history.py, or via its
    # shebang) puts only scripts/ on sys.path rather than the repo root --
    # without the bootstrap, `import dca_fileio` raises ModuleNotFoundError
    # before argparse even runs.
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo_root / "scripts" / "purchase_history.py"), "--help"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert result.returncode == 0
    assert "ModuleNotFoundError" not in result.stderr


def test_collect_purchase_records_reconciles_broker_regardless_of_scan_order(tmp_path):
    # near_close_dca.write_run_state's full-report shape omits `broker`
    # entirely -- so the brokerless occurrence of a real order isn't only
    # ever an orphan, and it can be scanned BEFORE its broker-attributed
    # twin in plain directory-sort order. "a_legacy_run" sorts before
    # "b_recovery_run" lexicographically, exercising exactly that ordering.
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-brokerless-first,3.50,filled,109.99,0.031821\n"
    )
    _write_run(
        root,
        "a_legacy_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET_legacy",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
    )
    _write_run(
        root,
        "b_recovery_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET_recovery",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
        broker="robinhood",
    )

    records, _ = collect_purchase_records(root)

    matching = [r for r in records if r.order_id == "order-brokerless-first"]
    assert len(matching) == 1
    assert matching[0].broker == "robinhood"


def test_collect_purchase_records_processes_every_orphan_report_in_shared_directory(tmp_path):
    # generate_slippage_report gives each run a unique timestamped filename
    # while accepting a shared output_dir -- two interrupted full-report
    # runs can leave two DISTINCT dca_*_orders.csv files in the SAME
    # directory, each representing a different run.
    root = tmp_path / "cron_tracking"
    shared_dir = root / "manual_reports"
    shared_dir.mkdir(parents=True)
    (shared_dir / "dca_daily_2026-08-05_20260805-153000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-report-1,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )
    (shared_dir / "dca_daily_2026-08-05_20260805-160000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NVDA,order-report-2,7.00,filled,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    order_ids = {r.order_id for r in records}
    assert order_ids == {"order-report-1", "order-report-2"}


def test_collect_purchase_records_infers_orphan_full_report_date_from_filename(tmp_path):
    # The definitive date lives in the full-report CSV's own FILENAME, not
    # necessarily its enclosing directory -- "manual_reports" itself carries
    # no date stamp at all.
    root = tmp_path / "cron_tracking"
    undated_dir = root / "manual_reports"
    undated_dir.mkdir(parents=True)
    (undated_dir / "dca_daily_2026-08-05_20260805-153000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-filename-date,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].trading_day == "2026-08-05"


def test_collect_purchase_records_keeps_manual_attribution_over_orphan_copy(tmp_path):
    # An orphan's honestly-unknown attribution (run_origin=None) must never
    # outrank a directory whose run_state actually parsed -- not even
    # "manual", the previously-worst real classification.
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-manual-vs-orphan,3.50,filled,109.99,0.031821\n"
    )
    _write_run(
        root,
        "manual_run",
        trading_day="2026-08-05",
        run_id="manual_20260805",
        stream="afternoon",
        run_origin="manual",
        filled_orders_csv=csv_body,
        broker="robinhood",
    )
    orphan_dir = root / "final_manual_review" / "20260805_orphan_copy"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "filled_orders.csv").write_text(csv_body, encoding="utf-8")

    records, _ = collect_purchase_records(root)

    matching = [r for r in records if r.order_id == "order-manual-vs-orphan"]
    assert len(matching) == 1
    assert matching[0].run_origin == "manual"
    assert matching[0].run_id == "manual_20260805"
    assert matching[0].stream == "afternoon"
    assert matching[0].broker == "robinhood"


def test_dedup_rank_ranks_orphan_below_manual():
    from purchase_history import _dedup_rank

    assert _dedup_rank("scheduled") < _dedup_rank("scheduled_recovery")
    assert _dedup_rank("scheduled_recovery") < _dedup_rank("manual")
    assert _dedup_rank("manual") < _dedup_rank(None)


def test_collect_purchase_records_marks_externally_recorded_csv_covered(tmp_path):
    # A non-recovery full-report run_state can record artifacts.orders_csv_path
    # pointing at a CSV OUTSIDE run_dir. Using a no-order-id row (the shape
    # whose dedup key is scoped per source directory) makes this test fail
    # loudly if the external directory isn't marked covered: the orphan
    # sweep would read the same physical file a second time under a
    # different directory label and it would NOT dedup against the first.
    root = tmp_path / "cron_tracking"
    run_dir = root / "scheduled_runs" / "20260805_1530ET"
    run_dir.mkdir(parents=True)
    external_dir = root / "external_reports"
    external_dir.mkdir(parents=True)
    state = {
        "run_label": "dca_afternoon_20260805_153000",
        "scheduled": True,
        "trading_day": "2026-08-05",
        "artifacts": {"orders_csv_path": "../../external_reports/dca_afternoon_20260805_153000_orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (external_dir / "dca_afternoon_20260805_153000_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nORCL,9.00,filled,150.00,0.06\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "ORCL"
    assert not any("orphan order artifact" in w for w in warnings)


def test_collect_purchase_records_orphan_sweep_finds_sibling_in_covered_shared_directory(tmp_path):
    # Marking a whole shared full-report output directory covered (because
    # ONE state references a CSV inside it) must not suppress a DIFFERENT,
    # genuinely orphaned dca_*_orders.csv sitting in that SAME directory --
    # only the specific referenced file should be considered consumed.
    root = tmp_path / "cron_tracking"
    run_dir = root / "scheduled_runs" / "20260805_1530ET"
    run_dir.mkdir(parents=True)
    shared_dir = root / "shared_reports"
    shared_dir.mkdir(parents=True)
    state = {
        "run_label": "dca_afternoon_20260805_153000",
        "scheduled": True,
        "trading_day": "2026-08-05",
        "artifacts": {"orders_csv_path": "../../shared_reports/dca_afternoon_20260805_153000_orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (shared_dir / "dca_afternoon_20260805_153000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "ORCL,order-covered,9.00,filled,150.00,0.06\n",
        encoding="utf-8",
    )
    (shared_dir / "dca_afternoon_20260805_160000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-orphan-sibling,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    order_ids = {r.order_id for r in records}
    assert order_ids == {"order-covered", "order-orphan-sibling"}


def test_collect_purchase_records_preserves_known_broker_when_brokerless_wins_attribution(tmp_path):
    # The "scheduled" occurrence (brokerless, near_close_dca.write_run_state's
    # full-report shape) outranks "scheduled_recovery" for ATTRIBUTION, but
    # the recovery occurrence is the ONLY one that proves the broker -- the
    # merge must not silently drop broker=null just because the brokerless
    # side won the attribution comparison.
    root = tmp_path / "cron_tracking"
    csv_body = (
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "SPCX,order-broker-preserve,3.50,filled,109.99,0.031821\n"
    )
    _write_run(
        root,
        "a_scheduled_brokerless",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=csv_body,
    )
    _write_run(
        root,
        "b_scheduled_recovery_brokered",
        trading_day="2026-08-05",
        run_id="scheduled_recovery_20260805",
        stream="afternoon",
        run_origin="scheduled_recovery",
        filled_orders_csv=csv_body,
        broker="robinhood",
    )

    records, _ = collect_purchase_records(root)

    matching = [r for r in records if r.order_id == "order-broker-preserve"]
    assert len(matching) == 1
    assert matching[0].run_origin == "scheduled"
    assert matching[0].broker == "robinhood"


def test_collect_purchase_records_orphan_sweep_excludes_dry_run_fills(tmp_path):
    # near_close_dca.DryRunBroker stamps every simulated fill's order_id
    # with a "DRY-RUN" prefix. A full-report dry run interrupted after its
    # order CSV was written but before dry_run=true was persisted to
    # run_state.json must not have its simulated rows counted as real
    # purchases by the orphan sweep, which has no run_state left to check.
    root = tmp_path / "cron_tracking"
    orphan_dir = root / "scheduled_runs" / "20260805_dry_run_interrupted"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "dca_daily_2026-08-05_20260805-153000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,DRY-RUN-AMD-4.00-abc123,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert records == []


def test_collect_purchase_records_falls_back_to_stale_recorded_basename(tmp_path):
    # An archived full-report run can keep its custom-named CSV sitting
    # right beside its (also archived) run_state.json, while the recorded
    # artifacts.orders_csv_path -- written before the archive move -- still
    # names the OLD, now-nonexistent absolute location.
    root = tmp_path / "cron_tracking"
    run_dir = root / "archive" / "20260805_1530ET"
    run_dir.mkdir(parents=True)
    state = {
        "run_label": "dca_afternoon_20260805_153000",
        "scheduled": True,
        "trading_day": "2026-08-05",
        "artifacts": {"orders_csv_path": "/old/moved/location/dca_afternoon_20260805_153000_orders.csv"},
    }
    (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    (run_dir / "dca_afternoon_20260805_153000_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nORCL,9.00,filled,150.00,0.06\n",
        encoding="utf-8",
    )

    records, warnings = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].ticker == "ORCL"
    assert any("no longer exists" in w for w in warnings)


def test_collect_purchase_records_dedupes_aliased_scan_roots(tmp_path):
    # The same physical root reachable through two different path spellings
    # (here, a symlink alias) must be scanned once, not twice -- Path
    # equality is purely lexical, so only resolve-then-dedup catches this.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "run1",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=("ticker,amount_usd,state,average_fill_price,quantity\nAMD,4.00,filled,100.00,0.04\n"),
    )
    alias_root = tmp_path / "cron_tracking_alias"
    alias_root.symlink_to(root, target_is_directory=True)

    records, _ = collect_purchase_records(root, additional_roots=(alias_root,))

    assert len(records) == 1


def test_collect_purchase_records_orphan_sweep_finds_full_report_beside_covered_run_state(tmp_path):
    # run_and_report accepts output_dir and state_path independently, so a
    # directory can hold a covered run's own run_state.json/filled_orders.csv
    # AND a totally unrelated interrupted run's full-report CSV. Marking the
    # whole directory covered just because ONE run_state parsed there must
    # not suppress that second, genuinely orphaned report.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "scheduled_runs/shared_output",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "ORCL,order-covered-run,9.00,filled,150.00,0.06\n"
        ),
    )
    (root / "scheduled_runs" / "shared_output" / "dca_daily_2026-08-05_20260805-160000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-orphan-in-covered-dir,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    order_ids = {r.order_id for r in records}
    assert order_ids == {"order-covered-run", "order-orphan-in-covered-dir"}


def test_collect_purchase_records_nulls_amount_for_unpriced_partial_execution(tmp_path):
    # near_close_dca._orders_csv_fills only recomputes price*quantity for a
    # partial execution when BOTH are valid; an unpriced partial row's
    # amount_usd is still the raw REQUESTED notional, never executed cash.
    # Publishing it as-is would overstate invested dollars.
    root = tmp_path / "cron_tracking"
    _write_run(
        root,
        "afternoon_run",
        trading_day="2026-08-05",
        run_id="scheduled_20260805_1530ET",
        stream="afternoon",
        run_origin="scheduled",
        filled_orders_csv=(
            "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
            "AMD,order-partial-unpriced,10.00,partially_filled,,0.02\n"
        ),
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].quantity == pytest.approx(0.02)
    assert records[0].avg_fill_price is None
    assert records[0].amount_usd is None


def test_infer_trading_day_from_path_full_report_filename_ignores_label_date():
    from purchase_history import _infer_trading_day_from_path

    # run_label is unrestricted free text and can embed its own dashed date
    # (e.g. --run-label "replay-2026-08-01") that sorts BEFORE the real
    # trading-day segment in the generated filename.
    path = Path("/root/manual_reports/dca_replay-2026-08-01_2026-08-05_20260805-153000_orders.csv")
    assert _infer_trading_day_from_path(path) == "2026-08-05"


def test_collect_purchase_records_full_report_date_ignores_label_embedded_date(tmp_path):
    root = tmp_path / "cron_tracking"
    orphan_dir = root / "manual_reports"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "dca_replay-2026-08-01_2026-08-05_20260805-153000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-label-date,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    assert len(records) == 1
    assert records[0].trading_day == "2026-08-05"


def test_collect_purchase_records_scopes_full_report_no_id_fills_to_their_file(tmp_path):
    # Two DISTINCT legacy (no order_id) full-report reports can share one
    # directory and each confirm a fill for the SAME ticker -- keying the
    # no-ID dedup identity by directory alone collided them into one row,
    # discarding a real purchase.
    root = tmp_path / "cron_tracking"
    shared_dir = root / "manual_reports"
    shared_dir.mkdir(parents=True)
    (shared_dir / "dca_daily_2026-08-05_20260805-153000_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nAMD,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )
    (shared_dir / "dca_daily_2026-08-05_20260805-160000_orders.csv").write_text(
        "ticker,amount_usd,state,average_fill_price,quantity\nAMD,3.00,filled,100.00,0.03\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    amd_records = [r for r in records if r.ticker == "AMD"]
    assert len(amd_records) == 2
    quantities = sorted(r.quantity for r in amd_records if r.quantity is not None)
    assert quantities == pytest.approx([0.03, 0.04])


def test_collect_purchase_records_orphan_sweep_finds_full_report_beside_canonical_orphan(tmp_path):
    # A shared directory can hold an orphan canonical artifact (no run_state)
    # AND an unrelated interrupted full-report CSV (also no run_state) --
    # the canonical pass marking the directory as "seen" must not suppress
    # the full-report pass's own, entirely different, discovery.
    root = tmp_path / "cron_tracking"
    shared_dir = root / "final_manual_review" / "20260805_shared"
    shared_dir.mkdir(parents=True)
    (shared_dir / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "AMD,order-canonical-orphan,4.00,filled,100.00,0.04\n",
        encoding="utf-8",
    )
    (shared_dir / "dca_daily_2026-08-05_20260805-160000_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "NVDA,order-full-report-beside-canonical,7.00,filled,120.00,0.058333\n",
        encoding="utf-8",
    )

    records, _ = collect_purchase_records(root)

    order_ids = {r.order_id for r in records}
    assert order_ids == {"order-canonical-orphan", "order-full-report-beside-canonical"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
