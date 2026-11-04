"""Step 3 of the 2026-10-30 credit plan: the pasted Perplexity prompt reads the slot-status signal first.

About 2,500 credits a trading day went on backup fires that ended as ``stood_down_local_active``: each one ingested a
~12 KB prompt, ran three planner commands, listed devices and ran a Mac sub-agent just to learn that Claude's slot had
succeeded (docs/cost_tracking.md; target under 200). The pasted prompt is now short:

* STEP A reads ``slot_status.json`` once (scripts/slot_status.py, published by the primary) and ends the run, with no
  other call, only on a slot marked ``succeeded`` today or a closed market. Anything else goes to STEP B.
* STEP B checks the build stamp, then fetches ``procedure --part 0``: the hard rules, the calendar, the old step 0
  and the stand-down gate, now served by the stamped planner instead of pasted. Parts 1-3 follow as before.

The hard rules are unchanged word for word; they moved from the paste box into the stamped build. No broker call or
order can happen before part 0 is read, because the short prompt names no broker tool and part 1 needs part 0's token.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import backup_procedure, fill_perplexity_backup_prompts, slot_status, verify_perplexity_pair
from scripts import generate_perplexity_backup_prompts as gen

REPO = Path(__file__).resolve().parents[2]
# The short prompt is what every normal-day fire pays for; keep it small (the old pasted prompt was ~12 KB).
SHORT_PROMPT_BUDGET = 3_500
BROKER_AND_MAC_TOOLS = (
    "get_accounts", "get_equity_orders", "get_equity_quotes", "get_equity_historicals", "get_equity_tradability",
    "place_equity_order", "review_equity_order", "get_portfolio", "list_devices", "run_local_subagent",
)
RUN_VALUE_NAMES = ("PPLX_RUN_STATE_DIR", "DCA_ALERT_EMAIL_ADDRESS", "ROBINHOOD_AGENTIC_ACCOUNT_LAST4")


@pytest.fixture(scope="module")
def instructions():
    return gen.build_instructions()


@pytest.fixture(scope="module")
def full():
    return gen.build_prompts()


def _part0(task_id: str) -> str:
    return backup_procedure.render_part(task_id, 0)


# -- the short pasted prompt -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_the_pasted_prompt_is_short(instructions, backup):
    text = instructions[backup.file]
    assert len(text) <= SHORT_PROMPT_BUDGET, f"{backup.file}: {len(text)} chars"


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_step_a_reads_the_signal_before_any_other_command(instructions, backup):
    text = instructions[backup.file]
    signal = text.index(slot_status.FILENAME)
    assert signal < text.index("--build-info") < text.index(f"procedure --task {backup.task_id} --part 0")
    assert text.index("STEP A") < text.index("STEP B")
    # Nothing runs before the signal: the first command-looking text is the signal read.
    assert "python3" not in text[:signal]


def test_step_a_names_the_published_branch_and_the_raw_url(instructions):
    settings = slot_status.load_settings()
    url = slot_status.raw_url(settings["branch"], settings["repository"])
    for text in instructions.values():
        assert url in text
        assert f'branch "{settings["branch"]}"' in text
        assert settings["repository"] in text
        # The trading repository is private: the signal must never be read from it (2026-10-31).
        assert "raw.githubusercontent.com/badMade/all-stock-dca/" not in text


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_step_a_stands_down_only_on_succeeded_today_or_a_closed_market(instructions, backup):
    step_a = instructions[backup.file].split("STEP A", 1)[1].split("STEP B", 1)[0]
    assert slot_status.SCHEMA in step_a
    assert '"trading_day"' in step_a and "today" in step_a and "America/New_York" in step_a
    assert '"market" is "closed"' in step_a
    assert '"status" is "succeeded"' in step_a
    # A stand-down here is free: no other tool call, no file, no notification.
    for phrase in ("no other tool call", "no file", "no notification"):
        assert phrase in step_a, phrase


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_every_other_signal_goes_to_step_b(instructions, backup):
    step_a = instructions[backup.file].split("STEP A", 1)[1].split("STEP B -", 1)[0]
    for case in ("cannot be read", "not JSON", "another day", "in_progress", "failed", "no entry for this slot"):
        assert case in step_a, case
    assert "go to STEP B" in step_a
    assert "never authorizes a buy" in step_a
    assert "Read it once" in step_a and "do not retry" in step_a


STAND_DOWN_REPLY = "RESULT: stood_down_signal"


def _stand_down_sentence(text: str) -> str:
    # The sentence that ends the fire: from "On either result" to the first full stop after "end the run".
    step_a = text.split("STEP A", 1)[1].split("STEP B -", 1)[0]
    start = step_a.index("On either result")
    return step_a[start:step_a.index("end the run", start)]


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_a_step_a_stand_down_names_its_ledger_status_in_the_final_reply(instructions, backup):
    # 2026-11-04: a STEP A stand-down writes no state record, so the operator could only log it in
    # scripts/backup_cost_ledger.py with no --status, and it vanished into "unrecorded". The run now says which
    # status it was in its last reply line: no tool call, no file, no notification, a few output tokens.
    sentence = _stand_down_sentence(instructions[backup.file])
    assert STAND_DOWN_REPLY in sentence
    assert "final reply" in sentence
    # Still free: the reply line is the only output, not a write.
    for phrase in ("no other tool call", "no file", "no notification"):
        assert phrase in sentence, phrase


def test_the_stand_down_reply_status_is_a_ledger_status_and_not_a_state_record_status():
    # One vocabulary: what the prompt prints is exactly what `backup_cost_ledger add --status` accepts.
    from scripts import backup_cost_ledger

    status = STAND_DOWN_REPLY.split(": ", 1)[1]
    assert status in backup_cost_ledger.STATUSES
    assert status not in backup_procedure.PROCEDURE_TEMPLATE


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_only_the_stand_down_prints_the_signal_status(instructions, backup):
    # A fire that goes on to STEP B never reports stood_down_signal; its outcome is the STEP 8 state record.
    text = instructions[backup.file]
    assert text.count("stood_down_signal") == 1
    assert "stood_down_signal" not in text.split("STEP B -", 1)[1]


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_the_committed_prompt_files_carry_the_stand_down_reply(backup):
    # The pasted files are generated; a stale file would keep the old, silent stand-down after a repaste.
    committed = (gen.DEFAULT_OUT_DIR / backup.file).read_text(encoding="utf-8")
    assert STAND_DOWN_REPLY in committed


def test_the_two_fire_prompt_picks_its_slot_from_the_clock_and_single_fires_name_one_slot(instructions):
    for backup in backup_procedure.BACKUPS:
        step_a = instructions[backup.file].split("STEP A", 1)[1].split("STEP B", 1)[0]
        if len(backup.slots) == 2:
            assert "before 14:30 ET" in step_a and '"1330"' in step_a and '"1430"' in step_a
        else:
            assert f'"{backup.slots[0]}"' in step_a
            assert "14:30" not in step_a


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_the_pasted_prompt_names_no_broker_or_mac_tool(instructions, backup):
    text = instructions[backup.file]
    for tool in BROKER_AND_MAC_TOOLS:
        assert tool not in text, tool
    assert "make no Robinhood call" in text and "place nothing" in text


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_step_b_checks_the_stamp_then_fetches_part_0(instructions, backup):
    step_b = instructions[backup.file].split("STEP B", 1)[1]
    assert step_b.index("--build-info") < step_b.index("content_sha256 must equal")
    assert step_b.index("content_sha256 must equal") < step_b.index(f"procedure --task {backup.task_id} --part 0")
    assert f"PROCEDURE FOR TASK {backup.task_id} - PART 0 OF 3" in step_b
    assert "place NOTHING" in step_b and "failed_unexpected_error" in step_b


def test_the_pair_checker_and_the_filler_still_find_their_header_facts(instructions):
    # verify_perplexity_pair and fill_perplexity_backup_prompts parse these lines out of the pasted prompt.
    for backup in backup_procedure.BACKUPS:
        text = instructions[backup.file]
        assert verify_perplexity_pair._STAMP.search(text)
        assert verify_perplexity_pair._UNIVERSE.search(text)
        assert fill_perplexity_backup_prompts._TASK_HEADER.search(text).group(1) == backup.task_id


def test_the_pasted_prompt_still_defines_every_run_value_part_0_and_the_procedure_name(instructions):
    for backup in backup_procedure.BACKUPS:
        text = instructions[backup.file]
        for name in RUN_VALUE_NAMES:
            assert f"{name} (" in text and f": ${{{name}}}" in text, (backup.file, name)
        named = set(re.findall(r"\$\{([A-Za-z0-9_]+)\}", _part0(backup.task_id)))
        assert named <= set(RUN_VALUE_NAMES), named - set(RUN_VALUE_NAMES)


# -- part 0: the hard rules, served by the stamped planner ------------------------------------------------------------


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_part_0_is_framed_like_the_other_parts_and_needs_no_token(backup):
    text = _part0(backup.task_id)
    assert text.startswith(f"PROCEDURE FOR TASK {backup.task_id} - PART 0 OF 3\n")
    assert f"END OF PART 0 OF 3 FOR TASK {backup.task_id}" in text
    token = backup_procedure.part_token(backup.task_id, 0)
    assert f"procedure --task {backup.task_id} --part 1 --token {token}" in text


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_part_0_carries_the_hard_rules_word_for_word_from_the_full_reference(full, backup):
    rules = full[backup.file].split("HARD RULES.", 1)[1].split("MARKET CALENDAR", 1)[0]
    assert "11. NO UNPRESCRIBED RETRIES OR LOOPS." in rules
    assert "HARD RULES." + rules in _part0(backup.task_id)


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_part_0_carries_the_calendar_universe_check_stand_down_gate_and_pointer(backup):
    text = _part0(backup.task_id)
    order = ["HARD RULES.", "MARKET CALENDAR", "backup_planner.pyz universe", "STAND-DOWN GATE",
             f"gates --task {backup.task_id}", "list_devices", "scripts.local_heartbeat status",
             "PROCEDURE (steps 1-9)"]
    positions = [text.index(needle) for needle in order]
    assert positions == sorted(positions), order
    assert "stood_down_local_active" in text and "send no notification" in text


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_part_0_does_not_hash_its_own_build(backup):
    # Part 0 ships inside the build it would have to name: the stamp check lives in the pasted prompt only.
    from scripts import build_backup_planner

    assert build_backup_planner.content_sha256() not in _part0(backup.task_id)
    assert "@@" not in _part0(backup.task_id)


def test_part_1_now_needs_the_token_part_0_printed():
    task = backup_procedure.BACKUPS[0].task_id
    with pytest.raises(ValueError, match="token"):
        backup_procedure.render_part(task, 1)
    with pytest.raises(ValueError, match="token"):
        backup_procedure.render_part(task, 1, token="0" * 8)
    assert backup_procedure.render_part(task, 1, token=backup_procedure.part_token(task, 0))


def test_part_tokens_are_distinct_per_part_and_task():
    first, second = backup_procedure.BACKUPS[0].task_id, backup_procedure.BACKUPS[1].task_id
    assert backup_procedure.part_token(first, 0) != backup_procedure.part_token(second, 0)
    assert backup_procedure.part_token(first, 0) != backup_procedure.part_token(first, 1)


def _run(args):
    return subprocess.run([sys.executable, "-m", "scripts.backup_planner", *args], cwd=REPO,
                          capture_output=True, text=True, check=False)


def test_the_planner_command_serves_part_0_and_refuses_part_1_without_its_token():
    task = backup_procedure.BACKUPS[4].task_id
    done = _run(["procedure", "--task", task, "--part", "0"])
    assert done.returncode == 0, done.stderr
    assert done.stdout == backup_procedure.render_part(task, 0)
    refused = _run(["procedure", "--task", task, "--part", "1"])
    assert refused.returncode == 2
    assert json.loads(refused.stdout)["status"] == "FAILED"


def test_part_0_ships_inside_the_stamped_pyz(tmp_path):
    from scripts import build_backup_planner

    out = tmp_path / "backup_planner.pyz"
    build_backup_planner.build(out, source_commit="0" * 40)
    task = backup_procedure.BACKUPS[0].task_id
    done = subprocess.run([sys.executable, str(out), "procedure", "--task", task, "--part", "0"],
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == backup_procedure.render_part(task, 0)


# -- everything is still reachable -------------------------------------------------------------------------------------


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_every_step_is_reachable_from_the_short_prompt_and_four_parts(instructions, backup):
    tokens = {part: backup_procedure.part_token(backup.task_id, part - 1) for part in (1, 2, 3)}
    text = instructions[backup.file] + _part0(backup.task_id) + "".join(
        backup_procedure.render_part(backup.task_id, part, token=tokens[part]) for part in (1, 2, 3))
    for step in range(0, 10):
        assert f"STEP {step} - " in text, step
    for needle in ("--plan plan.json", "check-placement", "STEP 8 - STATE RECORD", "STEP 9 - NOTIFY"):
        assert needle in text, needle


@pytest.mark.parametrize("backup", backup_procedure.BACKUPS, ids=lambda b: b.task_id)
def test_the_stand_down_condition_itself_requires_the_schema(instructions, backup):
    # slot_status.is_done refuses any other schema; the sentence that ends the run must say so, not only an example
    # elsewhere in STEP A (review of the Copilot step 3 commit, 2026-10-31).
    step_a = instructions[backup.file].split("\nSTEP A", 1)[1].split("\nSTEP B", 1)[0]
    condition = step_a[step_a.index("Stand down only when"):].split(".", 1)[0]
    assert f'"schema" is "{slot_status.SCHEMA}"' in condition
