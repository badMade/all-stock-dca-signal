"""Tests for scripts/check_slot_fills.py: the post-slot refusal and submission-count check.

Written before the implementation (TDD). Reproduces the 2026-10-07 09:35 ET
incident: ``place_equity_order`` was refused by the auto-mode classifier at
Gate 7, 4 of 20 eligible buys filled, 16 were never sent, and nothing flagged
it. Contract under test:

  * ``run_state.json`` carries ``counts.submitted`` (orders sent, including pending and
    unconfirmed ones; not a confirmed-fill count) and a
    ``gate9_report`` with ``eligible_count`` and a ``dropped`` object keyed
    ``price_recovered`` / ``spread_over_1pct`` / ``daily_cap``.
  * The check fails when any report text in the run directory contains
    ``PLATFORM REFUSAL``, or when ``submitted != eligible - sum(dropped)``.
  * It fails closed on a missing or malformed ``run_state.json`` or count
    fields, and is read-only: it never touches a broker.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import check_slot_fills  # noqa: E402
from scripts.verify_schedule import slot_run_dir_relative  # noqa: E402

DAY = date(2026, 10, 7)
SLOT = "0935"


def make_state(eligible=20, filled=20, dropped=None, **overrides):
    state = {
        "trading_day": DAY.isoformat(),
        "counts": {"submitted": filled},
        "gate9_report": {
            "eligible_count": eligible,
            "dropped": {"price_recovered": 0, "spread_over_1pct": 0, "daily_cap": 0, **(dropped or {})},
        },
    }
    state.update(overrides)
    return state


def write_run(root, state=None, *, raw_state=None, report_text=None):
    """Create the slot's dated run directory; return it."""
    run_dir = root / slot_run_dir_relative(SLOT, DAY)
    run_dir.mkdir(parents=True)
    if raw_state is not None:
        (run_dir / "run_state.json").write_text(raw_state)
    elif state is not None:
        (run_dir / "run_state.json").write_text(json.dumps(state))
    if report_text is not None:
        (run_dir / "gate9_report_0935.json").write_text(report_text)
    return run_dir


def run_cli(root, capsys):
    code = check_slot_fills.main(["--slot", SLOT, "--date", DAY.isoformat(), "--root", str(root)])
    return code, capsys.readouterr().out


# --- pure evaluate() -------------------------------------------------------

def test_platform_refusal_in_report_fails():
    failures = check_slot_fills.evaluate(make_state(eligible=20, filled=4),
                                         "Gate 7 aborted: PLATFORM REFUSAL on place_equity_order TSM")
    assert any("PLATFORM REFUSAL" in f for f in failures)


def test_refusal_fails_even_when_counts_balance():
    # A refusal is never acceptable, whatever the arithmetic says.
    failures = check_slot_fills.evaluate(make_state(eligible=20, filled=20), "PLATFORM REFUSAL")
    assert failures


def test_filled_equals_eligible_minus_dropped_passes():
    state = make_state(eligible=20, filled=15, dropped={"price_recovered": 2, "spread_over_1pct": 2, "daily_cap": 1})
    assert check_slot_fills.evaluate(state, "all buys placed") == []


def test_all_filled_with_nothing_dropped_passes():
    assert check_slot_fills.evaluate(make_state(eligible=20, filled=20), "") == []


def test_filled_below_eligible_without_refusal_text_fails():
    # The silent variant: 4 of 20 filled, no drops recorded, no refusal wording.
    failures = check_slot_fills.evaluate(make_state(eligible=20, filled=4), "Gate 7 stopped")
    assert failures
    assert any("submitted" in f and "4" in f and "20" in f for f in failures)


def test_filled_above_expected_fails():
    failures = check_slot_fills.evaluate(make_state(eligible=20, filled=19, dropped={"daily_cap": 2}), "")
    assert failures


@pytest.mark.parametrize("bad", [None, "20", 20.5, True, -1])
def test_malformed_eligible_count_fails_closed(bad):
    state = make_state()
    state["gate9_report"]["eligible_count"] = bad
    assert check_slot_fills.evaluate(state, "")


def test_missing_gate9_report_fails_closed():
    state = make_state()
    del state["gate9_report"]
    assert check_slot_fills.evaluate(state, "")


def test_missing_submitted_count_fails_closed():
    state = make_state()
    del state["counts"]["submitted"]
    assert check_slot_fills.evaluate(state, "")


def test_malformed_dropped_value_fails_closed():
    assert check_slot_fills.evaluate(make_state(dropped={"daily_cap": "two"}), "")


def test_non_object_state_fails_closed():
    assert check_slot_fills.evaluate(["not", "an", "object"], "")


def test_failures_are_deterministic():
    state = make_state(eligible=20, filled=4)
    assert check_slot_fills.evaluate(state, "PLATFORM REFUSAL") == check_slot_fills.evaluate(state, "PLATFORM REFUSAL")


# --- CLI over a real run directory ------------------------------------------

def test_cli_passes_on_balanced_run(tmp_path, capsys):
    run_dir = write_run(tmp_path, make_state(eligible=20, filled=18, dropped={"daily_cap": 2}), report_text="{}")
    before = {path.name: path.read_bytes() for path in run_dir.iterdir()}
    code, out = run_cli(tmp_path, capsys)
    assert code == 0
    assert "FAIL" not in out
    assert {path.name: path.read_bytes() for path in run_dir.iterdir()} == before


def test_cli_reproduces_2026_10_07_incident(tmp_path, capsys):
    write_run(tmp_path, make_state(eligible=20, filled=4),
              report_text=json.dumps({"stop": "PLATFORM REFUSAL: place_equity_order TSM blocked by classifier"}))
    code, out = run_cli(tmp_path, capsys)
    assert code == 1
    assert "PLATFORM REFUSAL" in out


def test_cli_reads_refusal_from_a_text_alert_file(tmp_path, capsys):
    run_dir = write_run(tmp_path, make_state(eligible=20, filled=20))
    (run_dir / "alert.txt").write_text("Gate 7 PLATFORM REFUSAL")
    code, _ = run_cli(tmp_path, capsys)
    assert code == 1


def test_cli_fails_closed_when_run_state_missing(tmp_path, capsys):
    write_run(tmp_path)  # directory exists, run_state.json never written
    code, out = run_cli(tmp_path, capsys)
    assert code == 1
    assert "run_state.json" in out


def test_cli_fails_closed_when_run_directory_missing(tmp_path, capsys):
    code, _ = run_cli(tmp_path, capsys)
    assert code == 1


@pytest.mark.parametrize("raw", ["{not json", "[]", '"text"', '{"a": 1, "a": 2}', ""])
def test_cli_fails_closed_on_malformed_run_state(tmp_path, capsys, raw):
    write_run(tmp_path, raw_state=raw)
    code, _ = run_cli(tmp_path, capsys)
    assert code == 1


def test_cli_json_output_is_sorted_and_stable(tmp_path, capsys):
    write_run(tmp_path, make_state(eligible=20, filled=4), report_text="PLATFORM REFUSAL")
    argv = ["--slot", SLOT, "--date", DAY.isoformat(), "--root", str(tmp_path), "--json"]
    assert check_slot_fills.main(argv) == 1
    first = capsys.readouterr().out
    assert check_slot_fills.main(argv) == 1
    assert first == capsys.readouterr().out
    payload = json.loads(first)
    assert payload["ok"] is False
    assert payload["failures"] == sorted(payload["failures"])


def test_cli_uses_env_root_when_no_root_flag(tmp_path, capsys, monkeypatch):
    write_run(tmp_path, make_state(eligible=20, filled=20))
    monkeypatch.setenv("DCA_CRON_TRACKING_ROOT", str(tmp_path))
    assert check_slot_fills.main(["--slot", SLOT, "--date", DAY.isoformat()]) == 0


def test_cli_rejects_unknown_slot(tmp_path):
    with pytest.raises(SystemExit):
        check_slot_fills.main(["--slot", "9999", "--date", DAY.isoformat(), "--root", str(tmp_path)])


@pytest.mark.parametrize("bad", [None, "20", 20.5, True, -1, float("nan"), float("inf")])
def test_malformed_submitted_count_fails_closed(bad):
    assert check_slot_fills.evaluate(make_state(filled=bad), "")


@pytest.mark.parametrize("reason", check_slot_fills.DROP_REASONS)
def test_missing_drop_count_fails_closed(reason):
    state = make_state()
    del state["gate9_report"]["dropped"][reason]
    assert check_slot_fills.evaluate(state, "")


def test_unknown_drop_reason_cannot_balance_counts():
    assert check_slot_fills.evaluate(make_state(filled=19, dropped={"other": 1}), "")


def test_cli_rejects_duplicate_count_keys(tmp_path, capsys):
    raw = json.dumps(make_state()).replace('"submitted": 20', '"submitted": 4, "submitted": 20')
    write_run(tmp_path, raw_state=raw)
    assert run_cli(tmp_path, capsys)[0] == 1


def test_cli_rejects_wrong_trading_day(tmp_path, capsys):
    write_run(tmp_path, make_state(trading_day="2026-10-06"))
    assert run_cli(tmp_path, capsys)[0] == 1


def test_cli_fails_closed_on_unreadable_report(tmp_path, capsys):
    run_dir = write_run(tmp_path, make_state())
    (run_dir / "alert.txt").write_bytes(b"\xff")
    assert run_cli(tmp_path, capsys)[0] == 1


def test_cli_fails_closed_on_external_report_symlink(tmp_path, capsys):
    run_dir = write_run(tmp_path, make_state())
    external = tmp_path.parent / f"{tmp_path.name}-external.txt"
    external.write_text("PLATFORM REFUSAL")
    (run_dir / "alert.txt").symlink_to(external)
    assert run_cli(tmp_path, capsys)[0] == 1


def test_cli_is_read_only(tmp_path, capsys):
    # Failing and passing runs alike must leave the run directory byte-identical.
    for passing in (True, False):
        root = tmp_path / ("pass" if passing else "fail")
        root.mkdir()
        run_dir = write_run(root, make_state(eligible=20, filled=20 if passing else 4),
                            report_text="{}" if passing else "PLATFORM REFUSAL")
        before = {p.name: p.read_bytes() for p in sorted(run_dir.iterdir())}
        run_cli(root, capsys)
        assert {p.name: p.read_bytes() for p in sorted(run_dir.iterdir())} == before
