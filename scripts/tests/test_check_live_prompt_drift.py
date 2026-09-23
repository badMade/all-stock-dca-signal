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
