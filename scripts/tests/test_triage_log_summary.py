"""Tests for scripts/triage_log_summary.py: the triage routine's redacted STEP 1 summary.

Reproduces 2026-10-30, the day ``verify_schedule`` reported ``ok: true`` while the
9:35 slot aborted at Gate 7 after firing late and the 3:30 slot aborted at Gate 5,
and pins that the summary flags exactly those two slots and leaks no account number.
"""

import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import triage_log_summary as tls  # noqa: E402
from scripts.verify_schedule import SLOT_EVIDENCE_LAYOUT, slot_run_dir_relative  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 30)
NOW = datetime(2026, 10, 30, 18, 15, tzinfo=ET)

# slot -> (run_state.status, order_submit_status, orders, extra run_state fields), as on 2026-10-30.
FRIDAY = {
    "0935": ("ABORT_GATE7_WINDOW_INSUFFICIENT", "submitted", 32,
             {"error_class": "window_insufficient",
              "gate9_report": {"notes": "Fired 36 min late (10:11 ET vs 09:35). placed nothing."}}),
    "1030": ("ORDERS_PLACED", "submitted", 6, {}),
    "1130": ("ORDERS_PLACED_OPERATOR_OVERRIDE", "submitted", 12, {}),
    "1230": ("NO_ORDERS_GATE6_DAILY_CAP_REACHED", "not_submitted_budget_exhausted", 0,
             {"gate9_report": {"notes": ["Gate 6: day_notional_usd 243.69 above cap 180.00"]}}),
    "1330": ("NO_ORDERS_GATE6_DAILY_CAP_EXHAUSTED", "not_submitted_budget_exhausted", 0, {}),
    "1430": ("ABORT_GATE6_DAILY_CAP_EXHAUSTED", "not_submitted_budget_exhausted", 0, {}),
    "1530": ("ABORT_GATE5_ALL_PROVIDER_ERRORS", "not_submitted_error", 0,
             {"error_class": "market_data_unavailable", "error_detail": "91 of 91 tickers NO_DATA"}),
}


def _write_friday(root: Path, account: str = "") -> None:
    for slot_id, (status, submit, count, extra) in FRIDAY.items():
        run_dir = root / slot_run_dir_relative(slot_id, DAY)
        run_dir.mkdir(parents=True)
        state = {"status": status, "order_submit_status": submit, "orders_submitted_count": count,
                 "finalized_at": "2026-10-30T14:12:39Z", "broker": {"account_number": account or "x"},
                 "budget": {"daily_cap_usd": 180}, **extra}
        (run_dir / "run_state.json").write_text(json.dumps(state), encoding="utf-8")


def _fake_verify_day(day, now, *, root=None):
    # Every slot "verified": the real 2026-10-30 verify_schedule verdict, which is the point.
    slots = [{"slot_id": s, "time_et": f"{s[:2]}:{s[2:]}", "stream": SLOT_EVIDENCE_LAYOUT[s][0],
              "status": "verified", "run_dir": str(slot_run_dir_relative(s, day)),
              "order_submit_status": FRIDAY[s][1], "orders_submitted_count": FRIDAY[s][2], "failure_key": None}
             for s in SLOT_EVIDENCE_LAYOUT]
    return {"date": day.isoformat(), "checked_at_et": "2026-10-30T18:15-04:00", "ok": True,
            "failing_slots": [], "slots": slots}


@pytest.fixture
def friday(tmp_path, monkeypatch):
    monkeypatch.setattr(tls, "verify_day", _fake_verify_day)
    _write_friday(tmp_path)
    return tmp_path


def test_friday_flags_the_two_aborts_verify_schedule_called_ok(friday):
    summary = tls.summarize_day(DAY, NOW, root=friday)
    assert summary["verify_schedule_ok"] is True
    assert summary["attention_slots"] == ["0935", "1530"]
    assert summary["ok"] is False
    by_slot = {s["slot_id"]: s for s in summary["slots"]}
    assert by_slot["0935"]["outcome"] == "ABORT_GATE7_WINDOW_INSUFFICIENT"
    assert "36 min late" in by_slot["0935"]["gate9_notes"]
    assert by_slot["1530"]["outcome"] == "ABORT_GATE5_ALL_PROVIDER_ERRORS"
    assert by_slot["1530"]["error_class"] == "market_data_unavailable"
    # Cap-exhausted aborts are the cap working, not faults.
    assert not by_slot["1430"]["attention"]
    assert by_slot["1230"]["gate9_notes"] == ["Gate 6: day_notional_usd 243.69 above cap 180.00"]


def test_window_insufficient_abort_is_flagged_for_attention(friday):
    summary = tls.summarize_day(DAY, NOW, root=friday)
    slot = next(slot for slot in summary["slots"] if slot["slot_id"] == "0935")
    assert slot["error_class"] == "window_insufficient"
    assert slot["outcome"] == "ABORT_GATE7_WINDOW_INSUFFICIENT"
    assert slot["attention"] is True


def test_summary_emits_only_listed_fields_and_relative_paths(friday):
    summary = tls.summarize_day(DAY, NOW, root=friday)
    text = json.dumps(summary)
    assert str(friday) not in text
    for slot in summary["slots"]:
        assert tuple(slot) == tls.SUMMARY_FIELDS
    assert "broker" not in text and "daily_cap_usd" not in text


def test_account_number_is_redacted_everywhere(tmp_path, monkeypatch):
    # conftest assigns the synthetic account; plant it in a note and a list note.
    account = "999000111"
    monkeypatch.setenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", account)
    monkeypatch.setattr(tls, "verify_day", _fake_verify_day)
    _write_friday(tmp_path, account=account)
    state_path = tmp_path / slot_run_dir_relative("1230", DAY) / "run_state.json"
    state = json.loads(state_path.read_text())
    state["gate9_report"]["notes"].append(f"account {account} checked")
    state["error_detail"] = f"acct {account}"
    state_path.write_text(json.dumps(state))
    text = json.dumps(tls.summarize_day(DAY, NOW, root=tmp_path))
    assert account not in text
    assert tls.REDACTED in text


@pytest.mark.parametrize("raw,expected", [
    ("account 123456789 x", "account [redacted] x"),
    ("dir 20261030_0935ET", "dir 20261030_0935ET"),
    ("day 20261030 ok", "day 20261030 ok"),
    ("$243.69 across 50 orders at 13:52", "$243.69 across 50 orders at 13:52"),
])
def test_redact(raw, expected, monkeypatch):
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    assert tls.redact(raw) == expected


def test_latest_trading_day_reads_friday_on_the_weekend():
    assert tls.latest_trading_day(date(2026, 10, 31)) == DAY
    assert tls.latest_trading_day(date(2026, 11, 1)) == DAY
    assert tls.latest_trading_day(DAY) == DAY


def test_cli_unreadable_root_says_logs_unavailable(tmp_path, capsys):
    rc = tls.main(["--date", "2026-10-30", "--root", str(tmp_path / "missing")])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("LOGS UNAVAILABLE")
    assert str(tmp_path) not in err


def test_cli_json_and_text(friday, capsys):
    assert tls.main(["--date", "2026-10-30", "--root", str(friday), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["attention_slots"] == ["0935", "1530"]
    assert tls.main(["--date", "2026-10-30", "--root", str(friday)]) == 1
    out = capsys.readouterr().out
    assert "ATTENTION: 0935, 1530" in out and "ABORT_GATE5_ALL_PROVIDER_ERRORS" in out
