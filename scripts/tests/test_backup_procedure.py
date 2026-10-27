"""The Perplexity backup procedure is served by the stamped planner, not pasted (2026-10-18).

Perplexity's Instructions box keeps only the first 20,000 characters. The full prompts were ~27,300, so each
was cut inside step 6 and the agent never saw the finalize arguments, order placement, the state record or the
notifications. The pasteable prompt is now the hard rules + step 0 + a pointer; steps 1-9 come from
``backup_planner.pyz procedure --task <id> --part <1|2|3>``, inside the same content-hash stamp.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import backup_planner, backup_procedure, build_backup_planner
from scripts import generate_perplexity_backup_prompts as gen

REPO = Path(__file__).resolve().parents[2]
PERPLEXITY_INSTRUCTIONS_LIMIT = 20_000
# Headroom so a modest edit to a hard rule cannot silently push a prompt back over the limit.
INSTRUCTIONS_BUDGET = 16_000


@pytest.fixture(scope="module")
def instructions():
    return gen.build_instructions()


@pytest.fixture(scope="module")
def full():
    return gen.build_prompts()


def test_every_pasteable_prompt_fits_the_perplexity_limit_with_headroom(instructions):
    assert len(instructions) == 6
    for name, text in instructions.items():
        assert len(text) <= PERPLEXITY_INSTRUCTIONS_LIMIT, f"{name}: {len(text)} chars is over Perplexity's limit"
        assert len(text) <= INSTRUCTIONS_BUDGET, f"{name}: {len(text)} chars leaves too little headroom"


def test_full_reference_prompt_is_the_instructions_head_plus_the_whole_procedure(instructions, full):
    for backup in backup_procedure.BACKUPS:
        head = instructions[backup.file].split("\nPROCEDURE (steps 1-9)")[0].rstrip("\n")
        assert full[backup.file].startswith(head)
        procedure = backup_procedure.render_procedure(backup, backup_procedure.common_values())
        assert full[backup.file].rstrip("\n").endswith(procedure.rstrip("\n"))


def test_the_three_parts_concatenate_back_to_the_whole_procedure():
    common = backup_procedure.common_values()
    for backup in backup_procedure.BACKUPS:
        procedure = backup_procedure.render_procedure(backup, common)
        parts = backup_procedure.split_parts(procedure)
        assert sorted(parts) == [1, 2, 3]
        assert "".join(parts[i] for i in (1, 2, 3)) == procedure
        assert parts[1].startswith("STEP 1 - ") and parts[2].startswith("STEP 4 - ") and parts[3].startswith("STEP 7 - ")


def test_every_step_is_reachable_from_instructions_plus_parts(instructions):
    for backup in backup_procedure.BACKUPS:
        text = instructions[backup.file] + "".join(
            backup_procedure.render_part(
                backup.task_id, part,
                token=None if part == 1 else backup_procedure.part_token(backup.task_id, part - 1),
            )
            for part in backup_procedure.PARTS
        )
        for step in range(0, 10):
            assert f"STEP {step} - " in text, (backup.file, step)
        # The exact things the 2026-10-18 truncation lost.
        for needle in ("--plan plan.json", "check-placement", "STEP 8 - STATE RECORD", "STEP 9 - NOTIFY"):
            assert needle in text, (backup.file, needle)


def test_instructions_point_at_their_own_task_and_keep_the_stamp_check(instructions):
    for backup in backup_procedure.BACKUPS:
        text = instructions[backup.file]
        for part in (1, 2, 3):
            assert f"procedure --task {backup.task_id} --part {part}" in text
        assert "STEP 0 - SETUP" in text and "content_sha256 must equal" in text
        assert "HARD RULES." in text
        assert "place NOTHING" in text.split("PROCEDURE (steps 1-9)")[1]


def _run(args):
    return subprocess.run([sys.executable, "-m", "scripts.backup_planner", *args], cwd=REPO,
                          capture_output=True, text=True, check=False)


def test_procedure_command_prints_exactly_the_requested_part():
    backup = backup_procedure.BACKUPS[4]  # the two-fire task
    for part in (1, 2, 3):
        token = [] if part == 1 else ["--token", backup_procedure.part_token(backup.task_id, part - 1)]
        done = _run(["procedure", "--task", backup.task_id, "--part", str(part), *token])
        assert done.returncode == 0, done.stderr
        assert done.stdout == backup_procedure.render_part(backup.task_id, part, token=token[1] if token else None)


def test_procedure_command_fails_closed_on_an_unknown_task_or_part():
    unknown = _run(["procedure", "--task", "deadbeef", "--part", "1"])
    assert unknown.returncode == 2
    assert json.loads(unknown.stdout)["status"] == "FAILED"
    bad_part = _run(["procedure", "--task", backup_procedure.BACKUPS[0].task_id, "--part", "4"])
    assert bad_part.returncode == 2


def test_procedure_ships_inside_the_stamped_pyz_and_serves_the_same_text(tmp_path):
    out = tmp_path / "backup_planner.pyz"
    info = build_backup_planner.build(out, source_commit="0" * 40)
    assert "scripts/backup_procedure.py" in info["files"]
    backup = backup_procedure.BACKUPS[0]
    token = backup_procedure.part_token(backup.task_id, 1)
    done = subprocess.run([sys.executable, str(out), "procedure", "--task", backup.task_id, "--part", "2",
                           "--token", token], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout == backup_procedure.render_part(backup.task_id, 2, token=token)


def test_procedure_text_does_not_depend_on_the_build_stamp(full):
    # The procedure ships inside the build it must not hash itself into: no placeholder for the stamp.
    assert "@@BUILD_STAMP@@" not in backup_procedure.PROCEDURE_TEMPLATE
    stamp = build_backup_planner.content_sha256()
    for backup in backup_procedure.BACKUPS:
        assert stamp not in backup_procedure.render_procedure(backup, backup_procedure.common_values())
    assert backup_planner  # imported for the subcommand under test


def test_the_run_values_the_procedure_names_are_defined_in_the_pasted_instructions(instructions):
    """Steps 8 and 9 live in the procedure and say ${PPLX_RUN_STATE_DIR} / ${DCA_ALERT_EMAIL_ADDRESS}. The operator's
    filler only fills the pasted prompt, so each value must be given there by name or the agent cannot resolve it."""
    for backup in backup_procedure.BACKUPS:
        text = instructions[backup.file]
        for name in ("PPLX_RUN_STATE_DIR", "DCA_ALERT_EMAIL_ADDRESS", "ROBINHOOD_AGENTIC_ACCOUNT_LAST4"):
            assert f"{name} (" in text and f": ${{{name}}}" in text, (backup.file, name)


def test_step_1_delegates_the_gate_decision_to_the_gates_command():
    for backup in backup_procedure.BACKUPS:
        part1 = backup_procedure.render_part(backup.task_id, 1)
        step1 = part1.split("STEP 2 - ")[0]
        assert f"gates --task {backup.task_id}" in step1
        for status in ("PROCEED", "skipped_market_closed", "skipped_outside_window", "failed_unexpected_error"):
            assert status in step1
        assert "Never pass --now" in step1


# --- Codex adversarial review of #1786 (2026-10-18), P2 findings --------------------------------------------------


def test_every_part_names_its_task_and_position_so_a_wrong_task_id_is_visible():
    """A prompt for one task must be able to notice it was served another task's procedure."""
    for backup in backup_procedure.BACKUPS:
        for part in (1, 2, 3):
            token = None if part == 1 else backup_procedure.part_token(backup.task_id, part - 1)
            text = backup_procedure.render_part(backup.task_id, part, token=token)
            assert text.startswith(f"PROCEDURE FOR TASK {backup.task_id} - PART {part} OF 3\n")
            assert f"END OF PART {part} OF 3 FOR TASK {backup.task_id}" in text
            assert f"END OF PART {part} OF 3" in text


def test_parts_must_be_requested_in_order_with_the_token_the_previous_part_printed():
    task = backup_procedure.BACKUPS[0].task_id
    part1 = backup_procedure.render_part(task, 1)
    token = backup_procedure.part_token(task, 1)
    assert f"--token {token}" in part1  # part 1 tells the agent what part 2 needs
    with pytest.raises(ValueError, match="token"):
        backup_procedure.render_part(task, 2)  # no token
    with pytest.raises(ValueError, match="token"):
        backup_procedure.render_part(task, 2, token="0" * 8)
    with pytest.raises(ValueError, match="token"):
        backup_procedure.render_part(task, 3, token=token)  # part 2's token is the one part 3 needs
    assert backup_procedure.render_part(task, 2, token=token)


def test_a_token_is_bound_to_its_task_and_part():
    first, second = backup_procedure.BACKUPS[0].task_id, backup_procedure.BACKUPS[1].task_id
    assert backup_procedure.part_token(first, 1) != backup_procedure.part_token(second, 1)
    assert backup_procedure.part_token(first, 1) != backup_procedure.part_token(first, 2)


def test_the_command_refuses_part_2_without_the_token_from_part_1():
    task = backup_procedure.BACKUPS[0].task_id
    for args in (["--part", "2"], ["--part", "3", "--token", backup_procedure.part_token(task, 1)],
                 ["--part", "2", "--token", "deadbeef"]):
        done = _run(["procedure", "--task", task, *args])
        assert done.returncode == 2, args
        assert json.loads(done.stdout)["status"] == "FAILED"


def test_instructions_tell_the_agent_to_pass_the_token_and_check_the_task_line(instructions):
    for backup in backup_procedure.BACKUPS:
        text = instructions[backup.file]
        assert "--part 2 --token" in text and "--part 3 --token" in text
        assert f"PROCEDURE FOR TASK {backup.task_id} - PART" in text
