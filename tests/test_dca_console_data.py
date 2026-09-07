"""Daily-console regressions for evidence, isolation, and schedule timing."""

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from dca_console_data import build_daily_snapshot

DAY = date(2026, 9, 8)
ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 9, 8, 12, tzinfo=ET)
CONFIG = Path(__file__).resolve().parents[1] / "config"


def write_state(root, name="example", **updates):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    state = {
        "trading_day": DAY.isoformat(), "generated_at": DAY.isoformat() + "T10:30:00-04:00",
        "status": "completed", "dry_run": False,
        "counts": {"buy_candidates": 1, "submitted": 1},
        "run_classification": {"stream": "morning", "run_origin": "scheduled", "manual_run": False},
    }
    state.update(updates)
    (folder / "run_state.json").write_text(json.dumps(state), encoding="utf-8")
    return folder


def snapshot(root, day=DAY, now=NOW, config_dir=CONFIG):
    return build_daily_snapshot(root, day, now, config_dir)


def test_missing_root_is_unknown_not_zero_spending(tmp_path):
    result = snapshot(tmp_path / "absent")
    assert result["metrics"]["run_count"] == 0
    assert result["metrics"]["confirmed_spend_usd"] is None
    assert "unverified" in result["summary"][0]
    assert any("does not exist" in warning for warning in result["warnings"])


def test_confirmed_fill_uses_purchase_reader_executed_amount(tmp_path):
    folder = write_state(tmp_path)
    (folder / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "ABC,order-example,40,filled,10,2\n", encoding="utf-8",
    )
    result = snapshot(tmp_path)
    assert result["metrics"]["confirmed_spend_usd"] == 20
    assert result["metrics"]["confirmed_fill_count"] == 1
    assert result["runs"][0]["confirmed_spend_usd"] == 20
    assert result["runs"][0]["mode"] == "live"


@pytest.mark.parametrize("mode_fields", [{"dry_run": True}, {"demo": True}, {"mode": "demo"}])
def test_simulated_fills_do_not_become_live_spending(tmp_path, mode_fields):
    folder = write_state(tmp_path, **mode_fields)
    (folder / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "ABC,order-example,40,filled,10,2\n", encoding="utf-8",
    )
    result = snapshot(tmp_path)
    assert result["metrics"]["confirmed_spend_usd"] is None
    assert result["metrics"]["confirmed_fill_count"] == 0
    assert result["runs"][0]["confirmed_spend_usd"] is None
    assert any("simulations" in sentence for sentence in result["summary"])


def test_requested_pending_amount_is_not_a_confirmed_fill(tmp_path):
    folder = write_state(tmp_path, status="submitted")
    (folder / "orders.csv").write_text(
        "ticker,order_id,amount_usd,status\nABC,order-example,40,pending\n", encoding="utf-8",
    )
    result = snapshot(tmp_path)
    assert result["runs"][0]["submitted"] == 1
    assert result["runs"][0]["status"] == "submitted"
    assert result["metrics"]["confirmed_spend_usd"] is None


def test_malformed_files_do_not_hide_healthy_runs_or_leak_contents(tmp_path):
    folder = write_state(tmp_path, account_number="private-account", access_token="private-token",
                         run_id="private-run-secret", error="private-error", status="blocked")
    (folder / "run_state_broken.json").write_text("private-malformed-content", encoding="utf-8")
    (folder / "run_state_array.json").write_text("[]", encoding="utf-8")
    result = snapshot(tmp_path)
    encoded = json.dumps(result, allow_nan=False)
    assert "private-" not in encoded
    assert str(tmp_path) not in encoded
    assert result["metrics"]["blocked_count"] == 1
    assert any("2 unreadable" in warning for warning in result["warnings"])


def test_duplicate_state_variants_are_one_run_and_recent_days_are_sorted(tmp_path):
    folder = write_state(tmp_path)
    (folder / "run_state_partial.json").write_text(json.dumps({
        "trading_day": DAY.isoformat(), "generated_at": DAY.isoformat() + "T10:29:00-04:00", "status": "pending",
    }), encoding="utf-8")
    write_state(tmp_path, "older", trading_day="2026-09-04")
    result = snapshot(tmp_path)
    assert result["metrics"]["run_count"] == 1
    assert result["runs"][0]["status"] == "completed"
    assert result["recent_days"] == ["2026-09-08", "2026-09-04"]


def test_activity_costs_are_finite_proxy_units_not_dollars(tmp_path):
    write_state(tmp_path, usage_proxy={"finance_calls_attempted": 2, "total_artifact_bytes": "Infinity",
                                      "private_token": "private-value"})
    result = snapshot(tmp_path)
    json.dumps(result, allow_nan=False)
    assert result["costs"]["estimated_total_proxy_units"] == 2
    assert "proxy" in result["costs"]["disclaimer"].lower()
    assert "private" not in json.dumps(result)


def task(result, task_id, clock=None):
    return next(row for row in result["schedule"] if row["id"] == task_id and (clock is None or row["time"] == clock))


def test_manifest_includes_all_tasks_and_all_intraday_slots(tmp_path):
    result = snapshot(tmp_path)
    configured = json.loads((CONFIG / "scheduled_tasks.json").read_text())["tasks"]
    assert {row["id"] for row in result["schedule"]} == {row["id"] for row in configured}
    assert [row["time"] for row in result["schedule"] if row["id"] == "7be82b97"] == ["11:30", "13:30", "14:30"]
    assert task(result, "claude-tickers-daily-update")["status"] == "not_registered"
    assert task(result, "d1f7a2c0")["status"] == "disabled"
    assert task(result, "f7411889")["status"] == "elapsed_one_time"
    assert task(result, "onetime-1630-credit-usage-proxy-2026-07-27")["next_at"] is None


def test_holiday_and_early_close_use_executor_calendar(tmp_path):
    holiday = snapshot(tmp_path, date(2026, 9, 7), datetime(2026, 9, 7, 8, tzinfo=ET))
    assert task(holiday, "2ba19526")["status"] == "market_closed"
    assert task(holiday, "2ba19526")["next_at"] == "2026-09-08T09:35:00-04:00"
    early = snapshot(tmp_path, date(2026, 11, 27), datetime(2026, 11, 27, 8, tzinfo=ET))
    assert task(early, "a6239dae")["status"] == "planned"
    assert task(early, "7be82b97", "13:30")["status"] == "market_closed"
    assert task(early, "7be82b97", "13:30")["next_at"] == "2026-11-30T13:30:00-05:00"


def test_monthly_reports_respect_first_weekday_and_last_market_day(tmp_path):
    first = snapshot(tmp_path, date(2026, 9, 1), datetime(2026, 9, 1, 8, tzinfo=ET))
    assert task(first, "0656657a")["status"] == "planned"
    second = snapshot(tmp_path, date(2026, 9, 2), datetime(2026, 9, 2, 8, tzinfo=ET))
    assert task(second, "0656657a")["status"] == "not_scheduled_today"
    end = snapshot(tmp_path, date(2026, 9, 30), datetime(2026, 9, 30, 8, tzinfo=ET))
    assert task(end, "c9fb1fb8")["status"] == "planned"


def test_unknown_calendar_coverage_does_not_predict_market_open(tmp_path):
    result = snapshot(tmp_path, date(2029, 1, 2), datetime(2029, 1, 2, 8, tzinfo=ET))
    assert task(result, "2ba19526")["status"] == "timing_unverified"
    assert task(result, "2ba19526")["next_at"] is None
    assert any("does not cover" in warning for warning in result["warnings"])


def test_missing_configuration_still_returns_a_daily_summary(tmp_path):
    write_state(tmp_path)
    result = snapshot(tmp_path, config_dir=tmp_path / "missing-config")
    assert result["metrics"]["run_count"] == 1
    assert result["schedule"] == []
    assert any("manifest" in warning for warning in result["warnings"])


def test_malformed_optional_run_fields_remain_unknown_without_breaking_view(tmp_path):
    write_state(tmp_path, counts=[], status={}, dry_run="false", buy_candidates={},
                broker_outcomes={"ABC": {"status": ["invalid"]}})
    result = snapshot(tmp_path)
    assert result["runs"][0]["submitted"] is None
    assert result["runs"][0]["candidates"] is None
    assert result["runs"][0]["mode"] == "unknown"
    json.dumps(result, allow_nan=False)


def test_excluded_demo_tree_does_not_contribute_runs_dates_costs_or_purchases(tmp_path):
    write_state(tmp_path, "live", counts={"submitted": 0})
    excluded = tmp_path / "console/demo"
    folder = write_state(excluded, usage_proxy={"finance_calls_attempted": 10})
    (folder / "filled_orders.csv").write_text(
        "ticker,order_id,amount_usd,state,average_fill_price,quantity\n"
        "ABC,order-example,40,filled,10,2\n", encoding="utf-8",
    )
    write_state(excluded, "older-demo", trading_day="2026-09-04")
    result = build_daily_snapshot(tmp_path, DAY, NOW, CONFIG, excluded_roots=(excluded,))
    assert result["metrics"]["run_count"] == 1
    assert result["metrics"]["confirmed_spend_usd"] is None
    assert result["metrics"]["confirmed_fill_count"] == 0
    assert result["costs"] is None
    assert result["recent_days"] == [DAY.isoformat()]


def test_run_state_symlink_cannot_extend_the_observed_root(tmp_path):
    external = write_state(tmp_path / "external")
    root = tmp_path / "observed"
    root.mkdir()
    (root / "run_state.json").symlink_to(external / "run_state.json")
    result = snapshot(root)
    assert result["metrics"]["run_count"] == 0
    assert result["recent_days"] == []
