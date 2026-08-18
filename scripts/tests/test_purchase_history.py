"""Tests for scripts/purchase_history.py.

Runnable with ``pytest scripts/tests`` or ``python scripts/tests/test_purchase_history.py``.
"""

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
    run_id: str,
    stream: str,
    run_origin: str,
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
    state = json.loads((run_dir / "run_state.json").read_bytes())
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
    assert sum(r.amount_usd or 0.0 for r in records) == pytest.approx(9.00)


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
    assert sum(r.amount_usd or 0.0 for r in records) == pytest.approx(9.00)


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
    rollup_data = json.loads(json_path.read_bytes())
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
    quantities = sorted(r.quantity or 0.0 for r in amd_records)
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
