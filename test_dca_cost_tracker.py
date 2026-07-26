"""Tests for the monthly automation cost/usage tracker.

Covers month filtering of run_state artifacts, per-component aggregation of the
usage proxies, the estimate/proxy labeling in the rendered markdown, and the
connector-agnostic payload. Runnable with ``pytest`` or ``python
test_dca_cost_tracker.py``.
"""

import json
import tempfile
from pathlib import Path

from dca_cost_tracker import (
    COST_TRACKER_TITLE,
    _month_prefix,
    aggregate_usage,
    build_cost_tracker_markdown,
    build_cost_tracker_payload,
    collect_month_run_states,
    generate_cost_tracker,
)


def _usage(**overrides):
    base = {
        "tickers_scanned": 3,
        "finance_calls_attempted": 3,
        "robinhood_calls_attempted": 3,
        "signal_rows_written": 6,
        "buy_candidates_written": 2,
        "order_reviews_attempted": 2,
        "orders_placed_count": 2,
        "state_file_bytes": 4000,
        "csv_artifact_count": 2,
        "total_artifact_bytes": 5000,
        "notifications_sent": 1,
        "errors_count": 0,
        "manual_recovery_required": False,
        "cost_control_notes": ["daily compact mode"],
    }
    base.update(overrides)
    return base


def _run_state(trading_day, **usage_overrides):
    return {
        "run_id": f"{trading_day}-154500",
        "trading_day": trading_day,
        "broker": "robinhood",
        "usage_proxy": _usage(**usage_overrides),
    }


def _write_runs(root, run_states):
    for i, rs in enumerate(run_states):
        run_dir = Path(root) / f"runs/{rs['trading_day']}-{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_state.json").write_text(json.dumps(rs))


def test_month_prefix():
    assert _month_prefix(2026, 6) == "2026-06"
    assert _month_prefix(2026, 12) == "2026-12"
    assert _month_prefix(99, 1) == "0099-01"


def test_collect_filters_by_month(tmp_path):
    _write_runs(
        tmp_path,
        [
            _run_state("2026-06-29"),
            _run_state("2026-06-30"),
            _run_state("2026-07-01"),  # different month, excluded
        ],
    )
    june = collect_month_run_states(tmp_path, 2026, 6)
    days = sorted(rs["trading_day"] for rs in june)
    assert days == ["2026-06-29", "2026-06-30"]

    july = collect_month_run_states(tmp_path, 2026, 7)
    assert [rs["trading_day"] for rs in july] == ["2026-07-01"]


def test_collect_skips_blocked_stubs(tmp_path):
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    blocked_dir = tmp_path / "runs/2026-06-30-blocked"
    blocked_dir.mkdir(parents=True, exist_ok=True)
    (blocked_dir / "run_state.json").write_text(
        json.dumps({"trading_day": "2026-06-30", "status": "blocked"}), encoding="utf-8"
    )

    states = collect_month_run_states(tmp_path, 2026, 6)
    assert len(states) == 1  # the date-gated stub is not an analyzed run
    agg = aggregate_usage(states, month_label="2026-06")
    assert agg["run_count"] == 1


def test_collect_skips_unparseable_files(tmp_path):
    _write_runs(tmp_path, [_run_state("2026-06-29")])
    bad_dir = tmp_path / "runs/bad"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (bad_dir / "run_state.json").write_text("{not valid json")

    states = collect_month_run_states(tmp_path, 2026, 6)
    assert len(states) == 1  # bad file skipped, good one kept


def test_aggregate_sums_and_component_breakdown():
    run_states = [
        _run_state("2026-06-29", finance_calls_attempted=3, orders_placed_count=2),
        _run_state("2026-06-30", finance_calls_attempted=3, orders_placed_count=1,
                   manual_recovery_required=True, errors_count=2),
    ]
    agg = aggregate_usage(run_states, month_label="2026-06")

    assert agg["run_count"] == 2
    assert agg["manual_recovery_runs"] == 1
    assert agg["totals"]["finance_calls_attempted"] == 6
    assert agg["totals"]["orders_placed_count"] == 3
    assert agg["totals"]["errors_count"] == 2

    by_component = {c["component"]: c for c in agg["components"]}
    # order_execution count = reviews(2+2) + orders(2+1) = 7, weight 2.0 -> 14.0
    assert by_component["order_execution"]["count"] == 7
    assert by_component["order_execution"]["estimated_proxy_units"] == 14.0
    # finance_data count 6, weight 1.0 -> 6.0
    assert by_component["finance_data"]["estimated_proxy_units"] == 6.0
    assert agg["estimated_total_proxy_units"] > 0


def test_aggregate_handles_missing_usage_block():
    run_states = [{"trading_day": "2026-06-29"}]  # no usage_proxy
    agg = aggregate_usage(run_states, month_label="2026-06")
    assert agg["run_count"] == 1
    assert agg["totals"]["finance_calls_attempted"] == 0
    assert agg["estimated_total_proxy_units"] == 0


def test_markdown_labels_estimates_not_credits():
    agg = aggregate_usage([_run_state("2026-06-29")], month_label="2026-06")
    md = build_cost_tracker_markdown(agg)
    assert md.startswith(f"## {COST_TRACKER_TITLE} — 2026-06")
    assert "Estimates only" in md
    assert "not exact credit" in md
    assert "not credits" in md  # component table header
    assert "| Component | Count | Proxy weight | Est. proxy units |" in md
    # a couple of components should be present
    assert "finance_data" in md
    assert "order_execution" in md
    # raw metric table present
    assert "Raw monthly usage proxies" in md


def test_payload_is_connector_agnostic():
    agg = aggregate_usage([_run_state("2026-06-29")], month_label="2026-06")
    payload = build_cost_tracker_payload(agg)
    assert payload["title"] == COST_TRACKER_TITLE
    assert payload["page_id"] is None  # supplied by caller, never embedded
    assert payload["value_type"] == "estimate_proxy"
    assert payload["markdown"] == build_cost_tracker_markdown(agg)

    with_page = build_cost_tracker_payload(agg, page_id="PAGE123")
    assert with_page["page_id"] == "PAGE123"


def test_generate_cost_tracker_writes_files(tmp_path):
    _write_runs(tmp_path, [_run_state("2026-06-29"), _run_state("2026-06-30")])
    out_dir = tmp_path / "cost"
    paths = generate_cost_tracker(tmp_path, 2026, 6, out_dir)

    assert Path(paths["markdown"]).exists()
    assert Path(paths["payload"]).exists()
    payload = json.loads(Path(paths["payload"]).read_text())
    assert payload["month_label"] == "2026-06"
    assert payload["aggregate"]["run_count"] == 2


def _run_all():
    import inspect

    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            if inspect.signature(fn).parameters:
                # tmp_path-style tests get a throwaway directory outside pytest.
                with tempfile.TemporaryDirectory() as tmp:
                    fn(Path(tmp))
            else:
                fn()
            print(f"PASS {name}")
    print("All cost-tracker self-tests passed.")


if __name__ == "__main__":
    _run_all()
