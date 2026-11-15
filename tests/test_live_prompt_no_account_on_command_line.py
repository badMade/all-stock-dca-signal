"""No live prompt passes the account number on a command line (tests first).

The prompts used to run, for example, ``scripts.merge_order_pages ... --account ${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}``
or ``--account <the resolved account>``. Either way the real number is on the command line once the shell expands
it, and so in process listings and any transcript that records the command (AGENTS.md secrets policy). The scripts
now resolve it from the environment (``dca_config.resolve_order_account``), and the bridge protocol already exports
``ROBINHOOD_AGENTIC_ACCOUNT_NUMBER`` before every call that needs it, so the flag is dropped from every command.

Prose that merely MENTIONS the flag (for example the bridge note that an unexported variable expands to nothing) is
fine; what is forbidden is the flag followed by a value.
"""

import re
from pathlib import Path

import pytest

LIVE_MIRROR = Path(__file__).resolve().parent.parent / "claude/dca-executor-prompts/live-mirror"
SLOTS = ("0935", "1030", "1130", "1230", "1330", "1430", "1530")
# `--account` followed by a value: a ${...} expansion, a $VAR, a <placeholder>, a quote, or a digit.
FLAG_WITH_A_VALUE = re.compile(r"--account(?:\s+|=)[\$<\"'0-9]")


def _text(slot: str) -> str:
    (path,) = sorted(LIVE_MIRROR.glob(f"{slot}ET-*.txt"))
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("slot", SLOTS)
def test_no_command_passes_the_account_on_the_command_line(slot):
    hits = [m.group(0) for m in FLAG_WITH_A_VALUE.finditer(_text(slot))]
    assert not hits, f"{slot}: {len(hits)} command line(s) still pass --account with a value"


@pytest.mark.parametrize("slot", SLOTS)
def test_the_bridge_export_that_makes_the_environment_available_is_kept(slot):
    # Removing the flag is only safe because this export runs before every call that needs the account.
    text = _text(slot)
    assert "export ROBINHOOD_AGENTIC_ACCOUNT_NUMBER=" in text


@pytest.mark.parametrize("slot", SLOTS)
def test_the_scripts_called_with_the_account_are_still_called(slot):
    # Guard against "fixing" the test by deleting the commands: the order-history scripts remain in the prompt.
    text = _text(slot)
    assert "scripts.merge_order_pages" in text and "scripts.same_day_top_up_gate" in text
