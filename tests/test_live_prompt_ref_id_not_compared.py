"""No live executor prompt may require the returned ref_id to equal the one sent.

The broker answers ``place_equity_order`` with its own ``ref_id``, never the UUID that was sent
(``scripts/validate_native_placement.py``, 2026-10-12), so an equality check can never pass and halts the batch
after its first order. 2026-10-27 15:30 ET: the final run placed DXYZ ($1.43), then halted with
``BATCH_HALTED_PLACEMENT_RESPONSE_IDENTITY_MISMATCH`` ("place_equity_order echoed a different ref_id than the one
sent ... prompt requires halt on client reference mismatch") and never tried CHE, IBM, MCK and NFLX. The 09:35
prompt carries the same leftover wording; the five intraday prompts already say the ``ref_id`` is not compared.
"""

from pathlib import Path

import pytest

MIRRORS = Path(__file__).resolve().parents[1] / "claude/dca-executor-prompts/live-mirror"
PROMPTS = sorted(p.name for p in MIRRORS.glob("*.txt") if not p.name.startswith("."))
# The old rule, as the 09:35 and 15:30 prompts still word it.
OLD_RULE = ("must equal the deterministic ref_id sent", "require it back unchanged")
# The corrected rule, as the five intraday prompts word it: validate_native_placement is the identity check.
NEW_RULE = "the `ref_id` is NOT compared"


def _text(name: str) -> str:
    return (MIRRORS / name).read_text(encoding="utf-8")


def test_all_seven_slot_prompts_are_covered():
    assert len(PROMPTS) == 7


@pytest.mark.parametrize("name", PROMPTS)
def test_no_prompt_requires_the_returned_ref_id_to_match(name):
    text = _text(name)
    for phrase in OLD_RULE:
        assert phrase not in text, f"{name} still says {phrase!r}"


@pytest.mark.parametrize("name", PROMPTS)
def test_every_prompt_says_the_ref_id_is_not_compared(name):
    assert NEW_RULE in _text(name)
