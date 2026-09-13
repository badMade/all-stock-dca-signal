"""Regression checks for the lightweight Octopus console artifact adapter."""

import json
import math
import os
from pathlib import Path

import pytest

import dca_console_octopus as adapter


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _pending(ticker="AAPL", *, origin="2026-09-04", close=0.01):
    return {
        "ticker": ticker, "model": "octopus", "origin_date": origin, "ref_close": 100,
        "point": {name: close for name in ("open", "high", "low", "close")},
        "quantiles": {name: {"p10": -0.02, "p50": 0.01, "p90": 0.03}
                      for name in ("open", "high", "low", "close")},
    }


def _state(tmp_path, entries=None, *, cursor="2026-09-04"):
    _write_json(tmp_path / "checkpoint/harness_state.json", {
        "last_processed_date": cursor, "outstanding": entries if entries is not None else {"": [_pending()]},
    })


def test_missing_and_not_configured_are_explicit(tmp_path):
    assert adapter.build_octopus_snapshot(None)["status"] == "not_configured"
    assert adapter.build_octopus_snapshot(tmp_path / "absent")["status"] == "missing"
    assert adapter.build_octopus_snapshot(tmp_path)["status"] == "empty"


def test_checkpoint_converts_prices_without_inventing_trading_date(tmp_path):
    _state(tmp_path)
    result = adapter.build_octopus_snapshot(tmp_path)
    forecast = result["forecasts"][0]
    assert forecast["close"] == 100 * math.exp(0.01)
    assert forecast["p10"] == 100 * math.exp(-0.02)
    assert forecast["target_date"] is None
    assert "Next trading row" in forecast["target_label"]
    assert result["as_of"] == "2026-09-04"
    assert result["status"] == "available"


def test_current_universe_and_latest_per_ticker_are_respected(tmp_path):
    _state(tmp_path, {"2026-09-08": [
        _pending(origin="2026-09-03", close=0.02), _pending(), _pending("MSFT"),
    ]})
    _write_json(tmp_path / "run_meta.json", {"universe": {"tickers": ["AAPL"]}})
    result = adapter.build_octopus_snapshot(tmp_path, ticker="aapl")
    assert len(result["forecasts"]) == 1
    assert result["forecasts"][0]["origin_date"] == "2026-09-04"
    assert result["forecasts"][0]["target_date"] is None
    assert result["forecasts"][0]["scheduled_score_date"] == "2026-09-08"
    assert adapter.build_octopus_snapshot(tmp_path, ticker="MSFT")["forecasts"] == []


def test_daily_csv_and_allowlisted_summary(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-09-04_next_day_forecasts.csv").write_text(
        "ticker,model,origin_date,pred_open,pred_high,pred_low,pred_close,close_p10,close_p90\n"
        "AAPL,octopus,2026-09-04,101,105,99,102,98,106\n", encoding="utf-8"
    )
    _write_json(daily / "2026-09-04_summary.json", {
        "date": "2026-09-04", "predicted_tickers": 1, "account": "do-not-display",
        "next_day_forecasts_csv": "/outside/secret.csv", "models": {"octopus": {
            "scored_today": 8, "close_mae_today": 0.01, "in_band_today": 0.8, "secret": "hidden",
        }}, "encoder_health": {"status": "DEGRADED", "secret": "hidden"},
    })
    result = adapter.build_octopus_snapshot(tmp_path)
    assert result["forecasts"][0]["close"] == 102
    assert result["latest_summary"]["predicted_tickers"] == 1
    encoded = json.dumps(result, allow_nan=False)
    assert "do-not-display" not in encoded and "hidden" not in encoded and "outside" not in encoded
    assert "Encoder health: DEGRADED." in result["summary"]


def test_regime_metrics_do_not_relabel_all_period_skill(tmp_path):
    stats = {"n": 10, "mae": 0.02, "mae_price": 2.0, "coverage": 0.8}
    _write_json(tmp_path / "report/report.json", {
        "by_regime": {name: {"octopus": {"close": stats}} for name in ("all", "covid")},
        "skill_vs_persistence": {"octopus": {"close": 0.25}},
    })
    result = adapter.build_octopus_snapshot(tmp_path)
    assert len(result["periods"]) == 2
    assert result["periods"][0]["skill"] == 0.25
    covid = adapter.build_octopus_snapshot(tmp_path, period="covid")["periods"]
    assert len(covid) == 1 and covid[0]["skill"] is None
    assert covid[0]["mae_price"] == 2.0


def test_corrupt_and_nonfinite_data_are_json_safe(tmp_path):
    _state(tmp_path, {"": [_pending(close=float("nan"))]})
    report = tmp_path / "report/report.json"
    report.parent.mkdir()
    report.write_text("{bad json", encoding="utf-8")
    result = adapter.build_octopus_snapshot(tmp_path)
    assert result["forecasts"][0]["close"] is None
    assert any("corrupt" in warning for warning in result["warnings"])
    assert any("nonfinite" in warning for warning in result["warnings"])
    json.dumps(result, allow_nan=False)


def test_symlink_escape_is_not_read(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _write_json(outside / "report.json", {"by_regime": {"all": {}}})
    run = tmp_path / "run"
    run.mkdir()
    (run / "report").symlink_to(outside, target_is_directory=True)
    result = adapter.build_octopus_snapshot(run)
    assert result["status"] == "empty"
    assert any("outside" in warning for warning in result["warnings"])


def test_byte_and_row_limits_are_explicit(tmp_path, monkeypatch):
    _state(tmp_path, {"": [_pending("AAPL"), _pending("MSFT")]})
    result = adapter.build_octopus_snapshot(tmp_path, limit=1)
    assert result["forecast_count"] == 2 and result["truncated"]
    assert len(result["forecasts"]) == 1
    monkeypatch.setattr(adapter, "_MAX_BYTES", 16)
    result = adapter.build_octopus_snapshot(tmp_path)
    assert result["forecasts"] == []
    assert any("read limit" in warning for warning in result["warnings"])


def test_stale_csv_and_summary_do_not_hide_newer_checkpoint(tmp_path):
    _state(tmp_path)
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "2026-09-03_next_day_forecasts.csv").write_text(
        "ticker,model,origin_date,pred_close\nAAPL,octopus,2026-09-03,1\n", encoding="utf-8"
    )
    _write_json(daily / "2026-09-03_summary.json", {"date": "2026-09-03"})
    result = adapter.build_octopus_snapshot(tmp_path)
    assert result["forecasts"][0]["close"] > 100
    assert any("summary is older" in warning for warning in result["warnings"])
    assert any("CSV is older" in warning for warning in result["warnings"])


def test_checkpoint_swap_fallback(tmp_path):
    _write_json(tmp_path / "checkpoint.prev/harness_state.json", {"outstanding": {"": [_pending()]}})
    assert len(adapter.build_octopus_snapshot(tmp_path)["forecasts"]) == 1


def test_report_freshness_warns_when_checkpoint_has_advanced(tmp_path):
    _state(tmp_path)
    report = tmp_path / "report/report.json"
    checkpoint = tmp_path / "checkpoint/harness_state.json"
    _write_json(report, {"by_regime": {"all": {"octopus": {"close": {"n": 3, "mae": 0.02}}}}})
    os.utime(report, (10, 10))
    os.utime(checkpoint, (20, 20))
    result = adapter.build_octopus_snapshot(tmp_path)
    assert result["report_may_be_stale"] is True
    assert any("period report predates" in warning for warning in result["warnings"])
    assert result["periods"][0]["n"] == 3
    assert result["report_modified_at"].endswith("+00:00")
    os.utime(report, (30, 30))
    refreshed = adapter.build_octopus_snapshot(tmp_path)
    assert refreshed["report_may_be_stale"] is False
    assert not any("period report predates" in warning for warning in refreshed["warnings"])


@pytest.mark.parametrize("current_ticker", ["MSFT", None])
def test_same_day_recovery_uses_checkpoint_even_when_outstanding_is_empty(tmp_path, current_ticker):
    _state(tmp_path, {"": [_pending(current_ticker)]} if current_ticker else {})
    daily = tmp_path / "daily"
    daily.mkdir()
    csv_path = daily / "2026-09-04_next_day_forecasts.csv"
    csv_path.write_text("ticker,model,origin_date,pred_close\nAAPL,octopus,2026-09-04,1\n", encoding="utf-8")
    os.utime(csv_path, (10, 10))
    os.utime(tmp_path / "checkpoint/harness_state.json", (20, 20))
    result = adapter.build_octopus_snapshot(tmp_path)
    assert [row["ticker"] for row in result["forecasts"]] == ([current_ticker] if current_ticker else [])
    assert result["forecast_source"] == "checkpoint outstanding forecasts"
    assert result["status"] == "available"
    assert any("same-day checkpoint" in warning for warning in result["warnings"])


def test_newer_same_day_csv_is_still_accepted(tmp_path):
    _state(tmp_path)
    daily = tmp_path / "daily"
    daily.mkdir()
    csv_path = daily / "2026-09-04_next_day_forecasts.csv"
    csv_path.write_text("ticker,model,origin_date,pred_close\nAAPL,octopus,2026-09-04,105\n", encoding="utf-8")
    os.utime(csv_path, (20, 20))
    os.utime(tmp_path / "checkpoint/harness_state.json", (10, 10))
    result = adapter.build_octopus_snapshot(tmp_path)
    assert result["forecasts"][0]["close"] == 105
    assert result["forecast_source"].endswith("_next_day_forecasts.csv")
    assert not any("same-day checkpoint" in warning for warning in result["warnings"])
