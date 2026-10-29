"""The 9:35 live prompt must work without the account number written into it.

2026-10-28: the live 9:35 task carried the real account number in about ten places, because
``scripts/sync_live_prompts.py`` replaces ``${ROBINHOOD_AGENTIC_ACCOUNT_NUMBER}`` with the live value.
That also rewrote the SECRET RULE to say "write the configured Agentic account or <the real number>
instead", so the prompt itself told the run it could put the number in an alert. The repo copy keeps
the placeholder, but a native tool call (get_equity_orders at Gate 4, get_portfolio, place_equity_order)
is built directly and performs no shell expansion, and the prompt only said how to resolve the number
at Gate 7, after Gate 4 had already needed it. These tests pin the rule that closes that gap, so the
placeholder version can be pasted as is.
"""

from pathlib import Path

PROMPT = (
    Path(__file__).resolve().parent.parent
    / "claude/dca-executor-prompts/live-mirror/0935ET-935-am-dca-morning-buy.txt"
)
MARKER = "RESOLVED ACCOUNT FOR NATIVE CALLS"


def _text() -> str:
    return PROMPT.read_text(encoding="utf-8")


def test_prompt_tells_the_run_how_to_resolve_the_account_for_native_calls():
    assert MARKER in _text()


def test_account_is_resolved_before_the_first_native_call_that_needs_it():
    text = _text()
    assert MARKER in text
    # Gate 4's get_equity_orders is the first native call that takes the account.
    assert text.index(MARKER) < text.index("GATE 4 - SOLE-EXECUTOR INTERLOCK")


def test_rule_forbids_writing_the_resolved_value_anywhere_but_the_calls():
    text = _text()
    start = text.index(MARKER)
    rule = text[start : start + 2500]
    assert "the configured Agentic account" in rule
    for place in ("alert", "email", "artifact"):
        assert place in rule


def test_rule_resolves_through_dca_paths_not_a_hardcoded_value():
    text = _text()
    start = text.index(MARKER)
    rule = text[start : start + 2500]
    assert "import dca_paths" in rule
    assert "ROBINHOOD_AGENTIC_ACCOUNT_NUMBER" in rule
