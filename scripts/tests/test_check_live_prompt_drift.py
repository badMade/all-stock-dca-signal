"""Tests for scripts/check_live_prompt_drift.py.

Motivating incident (2026-09-22): the 2:30 PM run found its LIVE task text
telling it to save order history as a bare JSON array, which
scripts/same_day_top_up_gate.py rejects, and quoting a trading-window end date
that no longer matched config/trading_window.json. The repo's live-mirror
copies were already correct for both; the live Cowork task bodies had drifted
from them. The same day the 10:30 slot placed 11 orders repeating tickers 9:35
had just bought, as a slot running without its same-day top-up guard would.
These tests pin the checker that makes that drift visible before a slot runs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Imported before any test sets $DCA_CONFIG_DIR: dca_config freezes
# DEFAULT_CONFIG_DIR at import time, so a first import under the monkeypatched
# value below would leak a temp config dir into every later test module.
import dca_config  # noqa: F401
from scripts import check_live_prompt_drift as drift

MIRROR_DIR = Path(__file__).resolve().parents[2] / "claude" / "dca-executor-prompts" / "live-mirror"

GOOD_BODY = (
    "GATE 3 - TRADING WINDOW\n"
    "Read config/trading_window.json. Do not hardcode either date.\n"
    "GATE 6.5 - SAME-DAY TOP-UP GUARD\n"
    'Save {"account_number": "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", "trading_day": "<YYYY-MM-DD>", '
    '"pages": [...]} then run python3 -m scripts.same_day_top_up_gate --orders-pages ...\n'
    "GATE 5 - BUY LIST\n"
    "NO HAND-REIMPLEMENTED SIGNALS: Never recompute signals, sizes or candidates by hand.\n"
    "GATE 9 - REPORT\n"
    "Run python3 -m scripts.finalize_run_evidence --slot 1030 --started-at ... on every outcome.\n"
)


def _mirror(tmp_path: Path, task: str, body: str = GOOD_BODY) -> Path:
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir(exist_ok=True)
    path = mirror_dir / f"{task}.txt"
    path.write_text(
        f"# MIRROR of the LIVE scheduled task `{task}` (test slot)\n# Read-only copy.\n\n{body}",
        encoding="utf-8",
    )
    return mirror_dir


def _live(tmp_path: Path, task: str, body: str) -> Path:
    scheduled = tmp_path / "Scheduled"
    (scheduled / task).mkdir(parents=True, exist_ok=True)
    (scheduled / task / "SKILL.md").write_text(body, encoding="utf-8")
    return scheduled


def _full_mirror_dir(tmp_path: Path) -> Path:
    """One mirror per roster slot, named the way the real directory is."""
    mirror_dir = tmp_path / "full-mirror"
    mirror_dir.mkdir()
    for slot in drift.EXPECTED_SLOTS:
        (mirror_dir / f"{slot}-task-{slot.lower()}.txt").write_text(
            f"# MIRROR of the LIVE scheduled task `task-{slot.lower()}` ({slot} slot)\n\n{GOOD_BODY}",
            encoding="utf-8",
        )
    return mirror_dir


def _check(tmp_path, mirror_dir, scheduled, end_date="2027-03-12", account=None):
    return drift.check(mirror_dir=mirror_dir, scheduled_dir=scheduled, window_end_date=end_date, account_number=account)


def _findings(report):
    return [f["code"] for slot in report["slots"] for f in slot["findings"]]


def test_a_live_task_matching_its_mirror_is_clean(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    scheduled = _live(tmp_path, "1430-test", GOOD_BODY)
    report = _check(tmp_path, mirror_dir, scheduled)
    assert report["drifted_slots"] == 0
    assert report["slots"][0]["status"] == "match"


def test_the_live_skill_frontmatter_is_not_reported_as_drift(tmp_path):
    # Every real ~/Claude/Scheduled/<task>/SKILL.md opens with name/description
    # frontmatter the mirror never carries; before this was stripped a faithful
    # repaste still read as "differs" (2026-09-22).
    mirror_dir = _mirror(tmp_path, "1430-test")
    live = "---\nname: 1430-test\ndescription: 2:30pm ET DCA buy\n---\n\n" + GOOD_BODY
    scheduled = _live(tmp_path, "1430-test", live)
    report = _check(tmp_path, mirror_dir, scheduled)
    assert report["slots"][0]["status"] == "match"
    assert report["drifted_slots"] == 0


def test_an_unclosed_frontmatter_opener_is_still_compared(tmp_path):
    # A lone leading "---" with no closer is not frontmatter; stripping to EOF
    # would hide the whole body, so it must stay in the comparison.
    mirror_dir = _mirror(tmp_path, "1430-test")
    scheduled = _live(tmp_path, "1430-test", "---\n" + GOOD_BODY)
    report = _check(tmp_path, mirror_dir, scheduled)
    assert report["slots"][0]["status"] == "differs"


def test_the_account_literal_in_live_text_is_not_reported_as_drift(tmp_path):
    # The README's one permitted divergence: live text carries the literal
    # account where the mirror carries the placeholder.
    mirror_dir = _mirror(tmp_path, "1430-test")
    scheduled = _live(tmp_path, "1430-test", GOOD_BODY.replace("${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", "999000111"))
    report = _check(tmp_path, mirror_dir, scheduled, account="999000111")
    assert report["slots"][0]["status"] == "match"


def test_bare_array_order_pages_instruction_is_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    stale = (
        "GATE 6.5 - SAME-DAY TOP-UP GUARD\n"
        "Save the raw get_equity_orders pages as a JSON array to orders_today.json, then run "
        "python3 -m scripts.same_day_top_up_gate --orders-pages orders_today.json\n"
    )
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", stale))
    assert "bare_array_order_pages" in _findings(report)
    assert report["drifted_slots"] == 1


def test_a_live_task_calling_get_equity_orders_without_the_page_helper_is_flagged(tmp_path):
    # 2026-09-24 audit: all seven live bodies called get_equity_orders but had
    # drifted from the mirror's OVERSIZED ORDER PAGES paragraph, so an oversized
    # page would again be hand-transcribed -- the exact 2026-09-09 incident.
    mirror_dir = _mirror(tmp_path, "1430-test")
    stale = "GATE 4 - SOLE-EXECUTOR INTERLOCK\nCall get_equity_orders for today's window.\n" + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", stale))
    assert "missing_oversized_order_pages_helper" in _findings(report)


def test_a_live_task_not_calling_get_equity_orders_at_all_is_not_flagged_for_the_page_helper(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", GOOD_BODY))
    assert "missing_oversized_order_pages_helper" not in _findings(report)


def test_a_live_task_with_both_get_equity_orders_and_the_page_helper_is_not_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    body = (
        "GATE 4 - SOLE-EXECUTOR INTERLOCK\nCall get_equity_orders for today's window.\n"
        "OVERSIZED ORDER PAGES: run python3 -m scripts.assemble_order_pages --account ... --out ...\n"
    ) + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", body))
    assert "missing_oversized_order_pages_helper" not in _findings(report)


def test_a_live_task_without_the_top_up_gate_is_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1030-test")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1030-test", "GATE 7 - PLACE THE ORDERS\nPlace them.\n"))
    assert "missing_top_up_gate" in _findings(report)


def test_a_live_task_without_the_evidence_finalizer_is_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    stale = GOOD_BODY.split("GATE 9 - REPORT")[0] + "GATE 9 - REPORT\nWrite run_state.json and refresh the pointer.\n"
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", stale))
    assert "missing_finalize_evidence" in _findings(report)


def test_every_committed_mirror_runs_the_evidence_finalizer_for_its_own_slot():
    from scripts.check_live_prompt_drift import DEFAULT_MIRROR_DIR

    mirrors = sorted(DEFAULT_MIRROR_DIR.glob("[0-9]*ET-*.txt"))
    assert len(mirrors) == 7
    for mirror in mirrors:
        text = mirror.read_text(encoding="utf-8")
        assert f"python3 -m scripts.finalize_run_evidence --slot {mirror.name[:4]} " in text, mirror.name


def test_a_hardcoded_window_end_date_that_disagrees_with_config_is_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1330-test")
    stale = GOOD_BODY + "Trading window end_date is 2026-10-30; abort after it.\n"
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1330-test", stale))
    finding = next(f for s in report["slots"] for f in s["findings"] if f["code"] == "stale_window_end_date")
    assert "2026-10-30" in finding["detail"]
    assert "2027-03-12" in finding["detail"]


def test_a_hardcoded_window_date_matching_config_is_not_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1330-test")
    body = GOOD_BODY + "Trading window end_date is 2027-03-12.\n"
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1330-test", body))
    assert "stale_window_end_date" not in _findings(report)


def test_a_live_task_missing_the_shell_unavailable_fallback_is_flagged(tmp_path):
    # 2026-09-08 incident (claude/dca-executor-prompts/README.md): a dead
    # sandboxed shell made Gate 2's repo-mount probes look identical to an
    # unreachable repo, so three consecutive slots reported "repo access" with
    # the wrong reconnect-the-folder remedy -- one of them (9:35) burning
    # roughly a third of its window on retries first. The fix adds a bounded
    # liveness check and this distinct abort reason; a live body still missing
    # it can reproduce the same misdiagnosis on the next dead shell.
    mirror_dir = _mirror(tmp_path, "1430-test")
    stale = (
        "GATE 2 - REPO REACHABLE\n"
        "Run `ls -d /sessions/*/mnt/all-stock-dca` and the other repo probes.\n"
    ) + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", stale))
    assert "missing_shell_unavailable_fallback" in _findings(report)


def test_a_live_task_with_the_shell_unavailable_fallback_is_not_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    body = (
        "GATE 2 - REPO REACHABLE\n"
        "Run `ls -d /sessions/*/mnt/all-stock-dca` and the other repo probes.\n"
        'If the shell will not start, abort with "executor shell tool unavailable, '
        'repo probes could not run" instead of a repo-access reason.\n'
    ) + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", body))
    assert "missing_shell_unavailable_fallback" not in _findings(report)


def test_a_live_task_that_never_runs_gate_2_probes_is_not_flagged_for_the_shell_fallback(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", GOOD_BODY))
    assert "missing_shell_unavailable_fallback" not in _findings(report)


def test_a_live_task_missing_the_bridge_only_abort_is_flagged(tmp_path):
    # 2026-09-29 2:30 PM: shell alive, all four probes empty, repo readable only
    # through the device file bridge. A live body without this distinct reason
    # reports the generic repo-access abort and its reconnect-the-folder remedy
    # for a repo that exists, so the operator fixes the wrong thing.
    mirror_dir = _mirror(tmp_path, "1430-test")
    stale = (
        "GATE 2 - REPO REACHABLE\n"
        "Run `ls -d /sessions/*/mnt/all-stock-dca` and the other repo probes.\n"
        'If the shell will not start, abort with "executor shell tool unavailable, '
        'repo probes could not run".\n'
    ) + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", stale))
    assert "missing_bridge_only_abort" in _findings(report)


def test_a_live_task_with_the_bridge_only_abort_is_not_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    body = (
        "GATE 2 - REPO REACHABLE\n"
        "Run `ls -d /sessions/*/mnt/all-stock-dca` and the other repo probes.\n"
        'If the probes are empty but a bridge read works, abort with "repo readable via device bridge, '
        'not mounted in executor shell".\n'
    ) + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", body))
    assert "missing_bridge_only_abort" not in _findings(report)


def test_a_live_task_that_never_runs_gate_2_probes_is_not_flagged_for_the_bridge_only_abort(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", GOOD_BODY))
    assert "missing_bridge_only_abort" not in _findings(report)


def test_a_live_task_still_telling_the_operator_to_draft_the_alert_email_is_flagged(tmp_path):
    # 2026-08-26 fix (README): the alert contract read "EMAIL DRAFT" as its
    # third Gate 9 channel until docs/alert_digest.md counted ~50 undelivered
    # drafts sitting in Gmail. A live body still requiring only a draft
    # repeats that silent-alert failure.
    mirror_dir = _mirror(tmp_path, "1430-test")
    stale = (
        "GATE 9 - REPORT\n"
        "Alert on: (1) IN-APP MESSAGE CENTER; (2) REPO ALERT FILE; "
        "(3) EMAIL DRAFT to the configured operator address.\n"
    ) + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", stale))
    assert "email_draft_not_send" in _findings(report)


def test_a_live_task_requiring_the_alert_email_to_be_sent_is_not_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    body = (
        "GATE 9 - REPORT\n"
        "Alert on: (1) IN-APP MESSAGE CENTER; (2) REPO ALERT FILE; "
        "(3) EMAIL SENT (not drafted) to the configured operator address.\n"
    ) + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", body))
    assert "email_draft_not_send" not in _findings(report)


def test_a_live_task_with_no_email_alerting_at_all_is_not_flagged_for_the_draft_check(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", GOOD_BODY))
    assert "email_draft_not_send" not in _findings(report)


def test_a_missing_live_task_file_is_reported_not_skipped(tmp_path):
    mirror_dir = _mirror(tmp_path, "0935-test")
    scheduled = tmp_path / "Scheduled"
    scheduled.mkdir()
    report = _check(tmp_path, mirror_dir, scheduled)
    assert report["slots"][0]["status"] == "live_missing"
    assert report["drifted_slots"] == 1


def test_output_never_carries_an_account_shaped_number(tmp_path, capsys):
    mirror_dir = _full_mirror_dir(tmp_path)
    live_body = "Place orders for account 123456789 now.\n"
    scheduled = _live(tmp_path, "task-1430et", live_body)
    code = drift.main([
        "--mirror-dir", str(mirror_dir), "--scheduled-dir", str(scheduled),
        "--window-end-date", "2027-03-12", "--diff", "--json",
    ])
    out = capsys.readouterr().out
    assert code == 1
    assert "123456789" not in out
    json.loads(out)


def test_a_comment_line_inside_the_prompt_body_is_still_compared(tmp_path):
    # Only the leading preamble is mirror metadata. The 1230 mirror carries
    # "#" lines inside its code snippets; a live body missing one must differ.
    body = GOOD_BODY + "```python\n# window_start covers the prior six trading days\nx = 1\n```\n"
    mirror_dir = _mirror(tmp_path, "1230-test", body)
    live = body.replace("# window_start covers the prior six trading days\n", "")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1230-test", live))
    assert report["slots"][0]["status"] == "differs"


def test_an_envelope_without_account_number_is_still_a_bare_array_finding(tmp_path):
    # same_day_top_up_gate requires all three keys; two of three is rejected.
    mirror_dir = _mirror(tmp_path, "1430-test")
    live = GOOD_BODY.replace('"account_number": "${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}", ', "")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", live))
    assert "bare_array_order_pages" in _findings(report)


@pytest.mark.parametrize("missing", ["dir", "slot", "duplicate"])
def test_an_incomplete_mirror_set_exits_2_rather_than_reporting_clean(tmp_path, missing):
    mirror_dir = _full_mirror_dir(tmp_path)
    if missing == "dir":
        mirror_dir = tmp_path / "no-such-mirrors"
    elif missing == "slot":
        next(mirror_dir.glob("1030ET-*.txt")).unlink()
    else:
        (mirror_dir / "1030ET-another-task.txt").write_text(GOOD_BODY, encoding="utf-8")
    scheduled = tmp_path / "Scheduled"
    scheduled.mkdir()
    code = drift.main([
        "--mirror-dir", str(mirror_dir), "--scheduled-dir", str(scheduled), "--window-end-date", "2027-03-12",
    ])
    assert code == 2


def test_an_unresolvable_window_end_date_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr(drift, "_window_end_date_from_config", lambda: None)
    scheduled = tmp_path / "Scheduled"
    scheduled.mkdir()
    code = drift.main(["--mirror-dir", str(_full_mirror_dir(tmp_path)), "--scheduled-dir", str(scheduled)])
    assert code == 2


def test_the_committed_mirror_directory_holds_every_slot():
    assert drift.mirror_set_problem(MIRROR_DIR) is None


def test_a_missing_scheduled_dir_exits_2(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    code = drift.main([
        "--mirror-dir", str(mirror_dir), "--scheduled-dir", str(tmp_path / "nope"),
        "--window-end-date", "2027-03-12",
    ])
    assert code == 2


@pytest.mark.parametrize(
    "mirror",
    sorted(p for p in MIRROR_DIR.glob("*.txt") if not p.name.startswith(".")),
    ids=lambda p: p.name,
)
def test_every_committed_mirror_is_clean_against_itself(tmp_path, mirror):
    # The canonical mirrors must pass their own checks, or the checker would
    # flag a correctly repasted live task as drifted.
    text = mirror.read_text(encoding="utf-8")
    task = drift.task_id_for(mirror, text)
    assert task, f"{mirror.name}: header does not name its live task"
    live = "\n".join(drift._mirror_body(text))
    scheduled = _live(tmp_path, task, live)
    report = drift.check(
        mirror_dir=mirror.parent, scheduled_dir=scheduled, window_end_date="2027-03-12",
        account_number=None, only=[mirror.name],
    )
    assert _findings(report) == []
    assert report["slots"][0]["status"] == "match"


def test_the_window_end_date_defaults_to_config(monkeypatch, tmp_path):
    (tmp_path / "trading_window.json").write_text('{"end_date": "2027-03-12"}', encoding="utf-8")
    monkeypatch.setenv("DCA_CONFIG_DIR", str(tmp_path))
    assert drift._window_end_date_from_config() == "2027-03-12"


# The conftest synthetic account, and a second one derived at runtime so no new
# account-shaped literal lands in the tree (test_config_invariants greps for them).
SYNTHETIC_ACCOUNT = "999000111"
OTHER_ACCOUNT = SYNTHETIC_ACCOUNT.replace("111", "222")

def _live_from_full_mirror(tmp_path: Path, mirror_dir: Path, accounts: dict) -> Path:
    """A live SKILL.md per full-mirror slot, the placeholder expanded per slot."""
    scheduled = tmp_path / "Scheduled"
    for mirror in sorted(mirror_dir.glob("*.txt")):
        task = drift.task_id_for(mirror)
        body = "\n".join(drift._mirror_body(mirror.read_text(encoding="utf-8")))
        account = accounts.get(task, SYNTHETIC_ACCOUNT)
        _live(tmp_path, task, f"---\nname: {task}\n---\n\n" + body.replace(drift.ACCOUNT_PLACEHOLDER, account))
    return scheduled


def test_without_the_env_var_the_account_shared_by_every_live_slot_is_used(tmp_path, monkeypatch, capsys):
    # 2026-09-22: right after a faithful repaste the plain command (no env var)
    # still reported 7 of 7 "differs" -- 13 false lines each, all the account.
    monkeypatch.delenv("ROBINHOOD_AGENTIC_ACCOUNT_NUMBER", raising=False)
    mirror_dir = _full_mirror_dir(tmp_path)
    scheduled = _live_from_full_mirror(tmp_path, mirror_dir, {})
    code = drift.main([
        "--mirror-dir", str(mirror_dir), "--scheduled-dir", str(scheduled), "--window-end-date", "2027-03-12",
    ])
    assert code == 0
    assert SYNTHETIC_ACCOUNT not in capsys.readouterr().out


def test_a_slot_on_a_different_account_is_not_auto_normalized(tmp_path):
    # Auto-detection must never bless a live task pointed at another account:
    # with no single shared value the comparison stays strict.
    mirror_dir = _full_mirror_dir(tmp_path)
    scheduled = _live_from_full_mirror(tmp_path, mirror_dir, {"task-1430et": OTHER_ACCOUNT})
    assert drift.shared_live_account(mirror_dir, scheduled) is None
    report = _check(tmp_path, mirror_dir, scheduled, account=drift.shared_live_account(mirror_dir, scheduled))
    assert report["drifted_slots"] == 7


@pytest.mark.parametrize(
    "stale_line",
    [
        "PREFETCHED YFINANCE FALLBACK (2026-09-22): merge it only if Robinhood failed as a whole.\n",
        "PREFETCH-FIRST (2026-10-12): the prefetched file is this slot's primary data path.\n",
    ],
)
@pytest.mark.parametrize("slot", ["0935", "1030"])
def test_a_live_task_still_carrying_the_retired_yfinance_paragraph_is_flagged(tmp_path, slot, stale_line):
    # 2026-10-12 owner direction retired the yfinance fallback from every scheduled run, and
    # the generators now reject a yahoo_finance_yfinance source outright (tests/test_yfinance_retired.py).
    # A live body that still merges the prefetched file, or still makes it 9:35's primary path
    # (the short-lived PREFETCH-FIRST rule), would abort Gate 5 with a ValueError instead of scoring.
    mirror_dir = _mirror(tmp_path, f"{slot}-test")
    stale = (
        "GATE 5 - BUY LIST\n"
        f"Save the input to $ROOT/market_data_<YYYY-MM-DD>_{slot}.json.\n"
        + stale_line
    ) + GOOD_BODY
    report = _check(tmp_path, mirror_dir, _live(tmp_path, f"{slot}-test", stale))
    assert "retired_yfinance_fallback" in _findings(report)


def test_a_live_task_without_the_yfinance_paragraph_is_not_flagged_as_retired(tmp_path):
    mirror_dir = _mirror(tmp_path, "0935-test")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "0935-test", GOOD_BODY))
    assert "retired_yfinance_fallback" not in _findings(report)


def test_a_live_task_without_the_no_hand_signals_rule_is_flagged(tmp_path):
    # hand_reimplemented: when the data path failed, a session hand-wrote signal logic instead of
    # calling the repo generator. The live body must carry the never-recompute rule in Gate 5.
    mirror_dir = _mirror(tmp_path, "1430-test")
    stale = GOOD_BODY.replace("Never recompute signals, sizes or candidates by hand.", "")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", stale))
    assert "missing_no_hand_signals_rule" in _findings(report)


def test_a_live_task_with_the_no_hand_signals_rule_is_not_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1430-test")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1430-test", GOOD_BODY))
    assert "missing_no_hand_signals_rule" not in _findings(report)


def test_every_committed_mirror_carries_the_rule_and_names_its_generator():
    mirrors = sorted(MIRROR_DIR.glob("[0-9]*ET-*.txt"))
    assert len(mirrors) == 7
    for mirror in mirrors:
        text = mirror.read_text(encoding="utf-8")
        assert drift.NO_HAND_SIGNALS_RULE in text, mirror.name
        # Either spelling names the producer: 0935 runs `python3 -m scripts.generate_market_open_candidates`,
        # and its only path-form mention was in a '#' reviewer-note block removed 2026-10-29.
        assert "scripts/generate_" in text or "scripts.generate_" in text, mirror.name


def test_a_live_task_naming_the_text_key_as_its_ref_id_is_flagged(tmp_path):
    # 2026-10-22 11:30: the placement sentence still read "DETERMINISTIC ref_id with every order
    # (sent as the UUID ...): dca-runner-1130-<YYYY-MM-DD>-<TICKER>", the session sent the text key,
    # place_equity_order rejected it (UUID only) and the slot aborted ref_id_format_rejected.
    mirror_dir = _mirror(tmp_path, "1130-test")
    stale = GOOD_BODY + (
        "DETERMINISTIC ref_id with every order (sent as the UUID the NATIVE ORDER-ID RULE derives from "
        "this key): dca-runner-1130-<YYYY-MM-DD>-<TICKER>, the same key dca_runner.execute_plan() derives.\n"
    )
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1130-test", stale))
    assert "ref_id_names_text_key" in _findings(report)


def test_a_live_task_without_the_old_ref_id_wording_is_not_flagged(tmp_path):
    mirror_dir = _mirror(tmp_path, "1130-test")
    report = _check(tmp_path, mirror_dir, _live(tmp_path, "1130-test", GOOD_BODY))
    assert "ref_id_names_text_key" not in _findings(report)


def test_no_committed_mirror_names_the_text_key_as_its_ref_id():
    for mirror in sorted(MIRROR_DIR.glob("[0-9]*ET-*.txt")):
        text = mirror.read_text(encoding="utf-8")
        assert drift.TEXT_KEY_REF_ID_WORDING not in text, mirror.name
        if "dca-runner-" in text and "Send a UUID ref_id with every order" not in text:
            # Only the 3:30 mirror (run_slot path, no per-order dca-runner key sentence) may omit it.
            assert mirror.name.startswith("1530ET"), mirror.name


# --- 2026-11-04: a mirror that resolves the account itself has a live body with no account literal. ---------------
RESOLVE_RULE = "RESOLVED ACCOUNT FOR NATIVE CALLS (operator, 2026-10-28): the account number is written nowhere.\n"


def _resolving_full_mirror_dir(tmp_path: Path, slot: str = "0935ET") -> Path:
    mirror_dir = _full_mirror_dir(tmp_path)
    mirror = next(mirror_dir.glob(f"{slot}-*.txt"))
    mirror.write_text(mirror.read_text(encoding="utf-8") + RESOLVE_RULE, encoding="utf-8")
    return mirror_dir


def _live_with_placeholder_only_slot(tmp_path: Path, mirror_dir: Path, slot: str = "0935ET") -> Path:
    scheduled = _live_from_full_mirror(tmp_path, mirror_dir, {})
    task = f"task-{slot.lower()}"
    path = scheduled / task / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8").replace(SYNTHETIC_ACCOUNT, drift.ACCOUNT_PLACEHOLDER),
                    encoding="utf-8")
    return scheduled


def test_a_placeholder_only_live_slot_is_skipped_when_its_mirror_resolves_the_account(tmp_path):
    mirror_dir = _resolving_full_mirror_dir(tmp_path)
    scheduled = _live_with_placeholder_only_slot(tmp_path, mirror_dir)

    assert drift.shared_live_account(mirror_dir, scheduled) == SYNTHETIC_ACCOUNT
    report = _check(tmp_path, mirror_dir, scheduled, account=SYNTHETIC_ACCOUNT)
    assert [s["status"] for s in report["slots"]] == ["match"] * 7


def test_a_placeholder_only_live_slot_still_blocks_inference_when_its_mirror_has_no_resolve_rule(tmp_path):
    mirror_dir = _full_mirror_dir(tmp_path)
    scheduled = _live_with_placeholder_only_slot(tmp_path, mirror_dir)

    assert drift.shared_live_account(mirror_dir, scheduled) is None
