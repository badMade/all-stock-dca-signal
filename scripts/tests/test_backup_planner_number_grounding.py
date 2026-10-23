"""``backup_planner plan|finalize --market-data``: the unsupported-number check is REPORT-ONLY by default.

Pins the rollout stance: findings carry reason code ``UNSUPPORTED_NUMBER`` under ``number_grounding`` but never
change the status or exit code unless ``--enforce-number-grounding`` is passed. Without ``--market-data`` the
output has no ``number_grounding`` key at all (existing callers see byte-identical output).
"""

from __future__ import annotations

import json
from datetime import date, datetime

import dca_config
import dca_order_plan as dop
from scripts import backup_planner, reason_codes

DAY = "2026-10-07"
SLOT = "1130"
NOW = "2026-10-07T11:49:30-04:00"


def _plan_file(tmp_path, decision_price):
    config = dca_config.load_strategy_config(dca_config.get_config_dir())
    survivor = {"ticker": "MSFT", "buy_usd": 2.0, "multiplier": 2.0, "drop_pct": 1.0, "current_price": decision_price,
                "prior_close": 101.0, "prior_low": 100.5}
    result = dop.plan_from_survivors(
        [survivor], config=config, trading_day=date.fromisoformat(DAY), slot=SLOT,
        generated_at_et=datetime.fromisoformat(NOW), ladder_version=dop.current_ladder_version())
    assert result.errors == ()
    path = tmp_path / "plan.json"
    dop.write_plan(path, result.plan)
    return path


def _market(tmp_path, price):
    path = tmp_path / "market.json"
    path.write_text(json.dumps({"sources": {"robinhood": {"MSFT": {
        "quote": {"price": price}, "previous_close_bar": {"close": 101.0, "low": 100.5}}}}}))
    return str(path)


def _plan_cmd(capsys, plan_path, *extra):
    code = backup_planner.main(["plan", "--source", "mac_plan", "--trading-day", DAY, "--slot", SLOT,
                                "--plan-file", str(plan_path), *extra])
    return code, json.loads(capsys.readouterr().out)


def test_reason_code_is_canonical_and_fail_closed():
    assert reason_codes.is_valid_reason("UNSUPPORTED_NUMBER")
    assert "UNSUPPORTED_NUMBER" in reason_codes.FAIL_CLOSED_REASONS
    # Report-only: it is not a benign no-buy outcome.
    assert "UNSUPPORTED_NUMBER" not in reason_codes.NO_BUY_REASONS


def test_no_market_data_means_no_grounding_key(tmp_path, capsys):
    code, report = _plan_cmd(capsys, _plan_file(tmp_path, 100.0))
    assert (code, report["status"]) == (0, "VALID_PLAN")
    assert "number_grounding" not in report


def test_grounded_plan_reports_empty_findings(tmp_path, capsys):
    code, report = _plan_cmd(capsys, _plan_file(tmp_path, 100.0), "--market-data", _market(tmp_path, 100.0))
    assert code == 0
    assert report["number_grounding"] == {"reason_code": "UNSUPPORTED_NUMBER", "enforced": False, "findings": []}


def test_unsupported_number_is_reported_but_does_not_block_by_default(tmp_path, capsys):
    code, report = _plan_cmd(capsys, _plan_file(tmp_path, 100.0), "--market-data", _market(tmp_path, 80.0))
    assert (code, report["status"]) == (0, "VALID_PLAN")
    findings = report["number_grounding"]["findings"]
    assert any(f["detail"].startswith("MSFT.decision_price") for f in findings)
    assert report["number_grounding"]["enforced"] is False


def test_enforce_flag_blocks_on_the_same_finding(tmp_path, capsys):
    code, report = _plan_cmd(capsys, _plan_file(tmp_path, 100.0), "--market-data", _market(tmp_path, 80.0),
                             "--enforce-number-grounding")
    assert (code, report["status"], report["plan"]) == (2, "NO_VALID_PLAN", None)
    assert report["errors"][0]["code"] == "unsupported_number"


def test_unreadable_market_data_is_reported_not_raised(tmp_path, capsys):
    code, report = _plan_cmd(capsys, _plan_file(tmp_path, 100.0), "--market-data", str(tmp_path / "missing.json"))
    assert code == 0
    assert report["number_grounding"]["findings"][0]["code"] == "market_data_missing"
