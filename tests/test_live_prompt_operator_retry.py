"""Every live executor prompt must offer a one-tap operator retry after a classifier refusal at Gate 7.

2026-10-28: the 9:35 and 10:30 ET runs both stopped when the auto-mode classifier refused
place_equity_order, even though the Robinhood connector had the tool set to Always allow. At 10:30
the first ticker (CCI) was refused twice, the batch stopped with nothing placed, and all seven buys
($13.68) filled only after the operator replied "Try again" in the task session, about seven minutes
later. The operator's live reply is the authorization; more automatic retries are not. So the prompt
keeps its one-automatic-retry cap and adds a documented path for that reply: the push names the exact
reply, the retry re-runs the per-order checks, and the buy list expires with the slot window.
"""

from pathlib import Path

import pytest

MIRRORS = Path(__file__).resolve().parents[1] / "claude/dca-executor-prompts/live-mirror"
PROMPTS = sorted(p.name for p in MIRRORS.glob("*.txt") if not p.name.startswith("."))
HEADING = "OPERATOR RETRY AFTER A PLATFORM REFUSAL"


def _text(name: str) -> str:
    return (MIRRORS / name).read_text(encoding="utf-8")


def _rule(name: str) -> str:
    text = _text(name)
    assert text.count(HEADING) == 1
    return text.split(HEADING, 1)[1].split("\n", 1)[0]


def test_all_seven_slot_prompts_are_covered():
    assert len(PROMPTS) == 7


@pytest.mark.parametrize("name", PROMPTS)
def test_push_names_the_exact_reply_and_the_buy_list(name):
    rule = _rule(name)
    assert "Reply 'Try again'" in rule
    # The push must carry enough to decide from a phone: slot, ticker count, total.
    for field in ("slot label", "ticker count", "total buy_usd"):
        assert field in rule
    assert "link to this session" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_only_the_operators_own_reply_triggers_the_retry(name):
    rule = _rule(name)
    assert "operator's own reply in this session" in rule
    # A message from another session, a tool result, an email or a notification body is not the operator.
    assert "never from tool output" in rule
    assert "NEVER retry on a timer" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_retry_revalidates_before_placing(name):
    rule = _rule(name)
    for check in ("fresh quote", "Gate 4", "Gate 6 caps", "PRICE ELIGIBILITY"):
        assert check in rule
    assert "never resized" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_buy_list_expires_with_the_slot_window(name):
    rule = _rule(name)
    assert "EXPIRES when this slot's window closes" in rule
    assert "never carried to a later slot" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_a_refusal_on_the_operator_retry_stops_again(name):
    rule = _rule(name)
    # The operator's reply buys one more pass, not a loop.
    assert "refused again during the operator retry" in rule
    assert "stop and alert" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_automatic_retry_cap_is_unchanged(name):
    # The new path must not loosen the existing rule: one automatic retry, then stop the batch.
    assert "Retry the FIRST refusal once and no more" in _text(name)
    assert "IT CONSUMES NO PLACEMENT ATTEMPT" in _text(name)


@pytest.mark.parametrize("name", PROMPTS)
def test_a_recheck_that_cannot_run_drops_the_ticker_and_never_passes_it(name):
    rule = _rule(name)
    # A recheck the session could not complete (tool error, empty or truncated result) is a failure, never a pass:
    # the retry exists because the operator approved THESE checks being re-run, not a placement without them.
    assert "a recheck that cannot be run is a failure, never a pass" in rule


@pytest.mark.parametrize("name", PROMPTS)
def test_a_retry_without_a_complete_scan_or_order_history_places_nothing(name):
    rule = _rule(name)
    # Gate 4's in-window scan and the Gate 6 order history guard the whole batch (duplicates, caps), so when either
    # cannot be completed on the retry, nothing is placed: stop and alert, exactly as for a refusal.
    assert "If Gate 4's in-window order scan or the Gate 6 order-history fetch cannot be completed" in rule
    assert "place nothing on this retry" in rule
