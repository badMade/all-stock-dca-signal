"""The Perplexity backups keep their full seven-day order fetch; they never reuse a saved baseline (yet).

2026-11-13: the 9:35 Claude slot did not fire, so the 10:30 Claude session had no same-day week envelope and fell
back to a 135,748-character full-week ``get_equity_orders`` fetch it could not save (ABORT_GATE6_HISTORY_INCOMPLETE /
output_cap_exceeded, 0 orders). Commit 67d9d617 taught ``scripts.find_order_baseline --kind week`` to accept
earlier-day week envelopes and wired that into the Claude live-mirror prompts (WEEK BASELINE REUSE). The Perplexity
backups were left out, and on 2026-11-14 the operator chose to pin their current behavior first (option "B") and
build the Mac-side merge as its own follow-up (the "hybrid"), rather than port the Claude wording, because the
backup path is shaped differently:

* Step 2 fetches from 00:00 ET SEVEN days back, not from Monday: the trailing-seven-day kill-switch figure
  (``--deployed-last-7d-usd auto``) needs days a Monday-start week envelope does not hold on Tuesday-Friday.
* The planner binds every saved envelope to ``trading_day`` == today (``same_day_top_up_gate._bind_saved_pages``),
  so a week envelope or a ``merge_order_pages`` output (both dated Monday) is refused by classify/compute/finalize.
* The saved envelopes live in the Mac checkout's cron root; the backup runs in the Perplexity sandbox and can reach
  the Mac only while it is online -- usually not the case the backup exists for.

These tests make that a deliberate, visible property: a procedure edit that half-adopts the Claude rule (say, a
``find_order_baseline`` call with no planner support for a Monday-dated envelope) fails here instead of in a live
backup run. When the hybrid lands, update these tests together with the procedure and the planner.

Every assertion reads the rendered procedure (what ``backup_planner.pyz procedure`` serves, under the pinned
``content_sha256``) or the pasted prompts; nothing here changes either, so no repaste is implied.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from scripts import backup_planner, backup_procedure

REPO = Path(__file__).resolve().parents[2]
PROMPTS = REPO / "claude/dca-executor-prompts/perplexity"
TASKS = [backup.task_id for backup in backup_procedure.BACKUPS]
# Anything that would mean the Claude WEEK BASELINE REUSE rule (or a piece of it) leaked into the backup path.
BASELINE_NEEDLES = (
    "WEEK BASELINE REUSE",
    "find_order_baseline",
    "merge_order_pages",
    "prior_baseline_merge",
    "baseline_merge",
    "orders_pages_week_",
    "_0935.json",
)


def _procedure(task_id: str) -> str:
    backup = backup_procedure.find_backup(task_id)
    assert backup is not None
    return backup_procedure.render_procedure(backup, backup_procedure.common_values())


def _step(text: str, heading: str, next_heading: str) -> str:
    start = text.index(heading)
    return text[start : text.index(next_heading, start)]


@pytest.mark.parametrize("task_id", TASKS)
def test_poll_1_fetches_seven_days_in_full_pinned_to_today(task_id):
    step2 = _step(_procedure(task_id), "STEP 2 - EVIDENCE POLL 1", "STEP 3 - ")
    # Seven days back, not from Monday: the kill-switch figure is a trailing seven-day sum.
    assert "created_at_gte set to 00:00 ET seven days before today" in step2
    assert "follow EVERY pagination cursor to completion" in step2
    # The only narrowing is that window; a re-query with another state, date or window is forbidden.
    assert "NEVER re-query with a different state, date, or window" in step2
    # Raw pages, one envelope dated today: the planner refuses any other trading_day.
    assert '"trading_day": "<today YYYY-MM-DD>"' in step2
    assert "trading_day stays today" in step2
    assert "never retype or condense rows by hand" in step2


@pytest.mark.parametrize("task_id", TASKS)
def test_poll_2_repeats_the_full_fetch_from_scratch(task_id):
    step6 = _step(_procedure(task_id), "STEP 6 - EVIDENCE POLL 2", "STEP 7 - ")
    assert "repeat the full poll of step 2 from scratch into orders_pages_poll2.json" in step6


@pytest.mark.parametrize("task_id", TASKS)
def test_procedure_never_reuses_a_saved_order_baseline(task_id):
    procedure = _procedure(task_id)
    for needle in BASELINE_NEEDLES:
        assert needle not in procedure, f"{task_id}: {needle!r} needs planner support first (see module docstring)"


@pytest.mark.parametrize("task_id", TASKS)
def test_every_served_part_is_free_of_the_baseline_rule(task_id):
    # Part 0 is a separate template (hard rules); parts 1-3 are cut from the procedure. Check what is served.
    token = None
    for part in (0, 1, 2, 3):
        text = backup_procedure.render_part(task_id, part, token=token)
        for needle in BASELINE_NEEDLES:
            assert needle not in text, f"{task_id} part {part}: {needle!r}"
        if part < 3:
            token = backup_procedure.part_token(task_id, part)


@pytest.mark.parametrize("path", sorted(PROMPTS.glob("*-backup.txt")), ids=lambda p: p.name)
def test_pasted_prompts_carry_no_order_history_rule(path):
    # The pasted prompt is only a signal check and preflight; the fetch rule lives in the stamped procedure.
    text = path.read_text(encoding="utf-8")
    for needle in BASELINE_NEEDLES:
        assert needle not in text, f"{path.name}: {needle!r}"


def test_planner_refuses_an_envelope_dated_monday(tmp_path):
    # Why a Claude-style week envelope cannot simply be handed to the backup planner: it is dated the ISO week's
    # Monday, and the planner pins every envelope to the requested trading day.
    envelope = tmp_path / "orders_pages_week.json"
    envelope.write_text(
        '{"account_number": "999000111", "trading_day": "2026-11-09", "pages": [{"results": [], "next": null}]}',
        encoding="utf-8",
    )
    with pytest.raises(backup_planner._Refused) as refused:
        backup_planner._load_rows(envelope, date(2026, 11, 13))
    assert refused.value.errors[0].code == "orders_pages_trading_day_mismatch"
